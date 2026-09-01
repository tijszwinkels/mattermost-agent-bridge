"""Provider-failure classification (round F3).

Ruling R4: the classifier is DATA. One signature table, each row motivated by
a real string, and adding a provider is one row + one test. These tests are
the "+ one test" half of that contract.

The two incident strings below are VERBATIM, recovered by the lead from the two
pi session logs (`~/.pi/agent/sessions/…2026-08-29T13-51-14…_8122f578….jsonl`
= Wagtail/OpenRouter; `…2026-08-31T13-41-32…_7c984a73….jsonl` = Linnet/ollama)
and supplied at M1. They are also the exact shape pi writes to stderr — see
`design.md` § Which stream carries a provider error, where the same
`<status>: <body>` line is reproduced from a live pi run.

Three things about the real strings that reconstructions would have missed:

  - the provider name appears ONLY inside a URL (`openrouter.ai`, `ollama.com`),
    never as a bare word;
  - the status arrives as a leading `403:` / `429:` prefix, and `"code"` is 403
    in one and **null** in the other, so the prefix is the only reliable source;
  - incident 2 carries a username and a ref UUID, and incident 1 a 64-hex key
    id. Only the key id is a credential — see `RealIncidentRedactionTests`.
"""
from __future__ import annotations

import unittest

from mm_bridge.backend_errors import classify_failure

# ── The two recorded incidents (Meshenger V2 program, pi backend) ──────────

INCIDENT_OPENROUTER_403 = (
    '403: {"message":"Key limit exceeded (daily limit). Manage it using '
    'https://openrouter.ai/workspaces/default/keys/'
    '2c37c23c5f934dba18d3a24cabd51368be82d67d490774a121c0ae55abbbc4b6",'
    '"code":403}'
)
INCIDENT_OLLAMA_429 = (
    '429: {"message":"you (tijszwinkels) have reached your session usage limit, '
    'upgrade for higher limits: https://ollama.com/upgrade or add extra usage: '
    'https://ollama.com/settings (ref: 56b1e3fb-e6b4-45b6-a0ca-45cf641755cf)",'
    '"type":"api_error","param":null,"code":null}'
)


class IncidentTests(unittest.TestCase):
    """The failures this whole round exists for."""

    def test_openrouter_daily_limit_is_quota_exhausted(self):
        f = classify_failure(INCIDENT_OPENROUTER_403)
        self.assertEqual(f.kind, "quota_exhausted")
        self.assertEqual(f.provider, "openrouter")
        self.assertEqual(f.status, 403)

    def test_ollama_session_usage_limit_is_rate_limited(self):
        f = classify_failure(INCIDENT_OLLAMA_429)
        self.assertEqual(f.kind, "rate_limited")
        self.assertEqual(f.provider, "ollama")
        self.assertEqual(f.status, 429)

    def test_a_403_daily_limit_is_never_mistaken_for_auth(self):
        """Row order is load-bearing: quota is tested before auth."""
        self.assertEqual(classify_failure(INCIDENT_OPENROUTER_403).kind, "quota_exhausted")

    def test_status_survives_a_null_code_field(self):
        """Incident 2 has `"code":null`; the leading `429:` is the only source."""
        self.assertEqual(classify_failure(INCIDENT_OLLAMA_429).status, 429)

    def test_provider_is_found_inside_a_url(self):
        """Neither incident names its provider as a bare word."""
        self.assertEqual(classify_failure(INCIDENT_OPENROUTER_403).provider, "openrouter")
        self.assertEqual(classify_failure(INCIDENT_OLLAMA_429).provider, "ollama")


class RealIncidentRedactionTests(unittest.TestCase):
    """What must be masked, and what must deliberately survive.

    Lead ruling at M1: mask the key id, keep the UUID and the username — they
    are not secrets, and a ref UUID is exactly what an operator would quote to
    the provider's support. Recorded as a test so the decision is explicit
    rather than an accident of the regexes.
    """

    KEY_ID = "2c37c23c5f934dba18d3a24cabd51368be82d67d490774a121c0ae55abbbc4b6"

    def test_the_64_hex_key_id_is_masked(self):
        self.assertNotIn(self.KEY_ID, classify_failure(INCIDENT_OPENROUTER_403).detail)
        self.assertNotIn(self.KEY_ID[:24], classify_failure(INCIDENT_OPENROUTER_403).detail)

    def test_masking_the_key_id_does_not_cost_the_classification(self):
        f = classify_failure(INCIDENT_OPENROUTER_403)
        self.assertEqual(f.kind, "quota_exhausted")
        self.assertEqual(f.provider, "openrouter")
        self.assertEqual(f.status, 403)

    def test_the_ref_uuid_survives(self):
        detail = classify_failure(INCIDENT_OLLAMA_429).detail
        self.assertIn("56b1e3fb-e6b4-45b6-a0ca-45cf641755cf", detail)

    def test_the_username_survives(self):
        self.assertIn("tijszwinkels", classify_failure(INCIDENT_OLLAMA_429).detail)


class SignatureRowTests(unittest.TestCase):
    """One test per signature row (R4)."""

    def test_monthly_limit(self):
        self.assertEqual(
            classify_failure("monthly limit reached for this key").kind, "quota_exhausted")

    def test_quota_exceeded(self):
        self.assertEqual(
            classify_failure("Quota exceeded for this project").kind, "quota_exhausted")

    def test_insufficient_quota(self):
        self.assertEqual(
            classify_failure('{"code":"insufficient_quota"}').kind, "quota_exhausted")

    def test_out_of_credits(self):
        self.assertEqual(
            classify_failure("You are out of credits").kind, "quota_exhausted")

    def test_credit_balance(self):
        self.assertEqual(
            classify_failure("Your credit balance is too low").kind, "quota_exhausted")

    def test_rate_limit_wording(self):
        self.assertEqual(classify_failure("rate limit exceeded").kind, "rate_limited")

    def test_too_many_requests(self):
        self.assertEqual(classify_failure("429 Too Many Requests").kind, "rate_limited")

    def test_overloaded(self):
        self.assertEqual(
            classify_failure('{"type":"overloaded_error"}').kind, "rate_limited")

    def test_invalid_api_key(self):
        self.assertEqual(classify_failure("Invalid API key provided").kind, "auth")

    def test_no_auth_credentials(self):
        self.assertEqual(
            classify_failure("No auth credentials found").kind, "auth")

    def test_unauthorized(self):
        self.assertEqual(classify_failure("401 Unauthorized").kind, "auth")

    def test_authentication_error(self):
        self.assertEqual(
            classify_failure('{"type":"authentication_error"}').kind, "auth")

    def test_context_length(self):
        self.assertEqual(
            classify_failure("This model's maximum context length is 200000 tokens").kind,
            "context_overflow")

    def test_prompt_is_too_long(self):
        self.assertEqual(
            classify_failure("prompt is too long: 250000 tokens").kind,
            "context_overflow")

    def test_unknown_is_the_fallback(self):
        f = classify_failure("CLI exited with a non-zero status (1).")
        self.assertEqual(f.kind, "unknown")
        self.assertIsNone(f.provider)


class ProviderExtractionTests(unittest.TestCase):
    def test_openrouter(self):
        self.assertEqual(
            classify_failure("https://openrouter.ai/api/v1 said no").provider, "openrouter")

    def test_ollama(self):
        self.assertEqual(classify_failure("ollama server error").provider, "ollama")

    def test_anthropic(self):
        self.assertEqual(
            classify_failure("api.anthropic.com returned 500").provider, "anthropic")

    def test_openai(self):
        self.assertEqual(
            classify_failure("api.openai.com returned 500").provider, "openai")

    def test_google(self):
        self.assertEqual(
            classify_failure("google generativelanguage returned 500").provider, "google")

    def test_gemini_is_an_alias_for_google(self):
        self.assertEqual(classify_failure("gemini API error").provider, "google")

    def test_openrouter_is_not_read_as_openai(self):
        self.assertEqual(classify_failure("openrouter.ai error").provider, "openrouter")

    def test_no_provider_named_yields_none(self):
        self.assertIsNone(classify_failure("CLI exited with status 1").provider)


class StatusExtractionTests(unittest.TestCase):
    def test_http_prefixed(self):
        self.assertEqual(classify_failure("HTTP 403 from provider").status, 403)

    def test_arrow_shape_used_by_this_repo(self):
        detail = "agent-harness POST /v1/sessions -> 500: harness exploded"
        self.assertEqual(classify_failure(detail).status, 500)

    def test_json_code_field(self):
        self.assertEqual(classify_failure('{"code":429}').status, 429)

    def test_absent_status_is_none(self):
        self.assertIsNone(classify_failure("something went wrong").status)

    def test_leading_status_colon_prefix(self):
        """pi composes `<status>: <body>` — the shape both incidents arrive in."""
        self.assertEqual(classify_failure('429: {"message":"nope"}').status, 429)

    def test_leading_status_before_a_json_body(self):
        """The openai SDK composes `<status> <body>` with no colon."""
        self.assertEqual(classify_failure('403 {"error":{"code":403}}').status, 403)

    def test_degraded_no_body_form(self):
        """pi/openai when the body can't be folded in: `403 status code (no body)`."""
        self.assertEqual(classify_failure("403 status code (no body)").status, 403)

    def test_a_plain_number_is_not_read_as_a_status(self):
        """`500 tokens` must not become HTTP 500."""
        self.assertIsNone(classify_failure("used 500 tokens this turn").status)

    def test_a_leading_plain_number_is_not_read_as_a_status(self):
        """Even in leading position, a bare count is not a status code."""
        self.assertIsNone(classify_failure("500 tokens used this turn").status)


class StderrTailTests(unittest.TestCase):
    """The incident shape: payload says nothing, stderr says everything."""

    def test_tail_supplies_the_class_when_detail_is_a_bare_returncode(self):
        f = classify_failure(
            "CLI exited with a non-zero status (1).",
            stderr_tail=("starting pi", INCIDENT_OPENROUTER_403),
        )
        self.assertEqual(f.kind, "quota_exhausted")
        self.assertEqual(f.provider, "openrouter")
        self.assertEqual(f.status, 403)

    def test_evidence_is_the_single_matched_line_only(self):
        f = classify_failure(
            "CLI exited with a non-zero status (1).",
            stderr_tail=("noise one", INCIDENT_OLLAMA_429, "noise two"),
        )
        self.assertIn("session usage limit", f.evidence or "")
        self.assertNotIn("noise one", f.evidence or "")
        self.assertNotIn("noise two", f.evidence or "")

    def test_detail_wins_over_the_tail_when_it_already_classifies(self):
        f = classify_failure(INCIDENT_OPENROUTER_403, stderr_tail=(INCIDENT_OLLAMA_429,))
        self.assertEqual(f.kind, "quota_exhausted")
        self.assertIsNone(f.evidence, "no stderr quoting needed when the payload said it")

    def test_unclassifiable_tail_leaves_unknown_and_no_evidence(self):
        f = classify_failure(
            "CLI exited with a non-zero status (1).",
            stderr_tail=("just some chatter", "more chatter"),
        )
        self.assertEqual(f.kind, "unknown")
        self.assertIsNone(f.evidence)

    def test_evidence_is_redacted(self):
        """C1: a matched line carrying a key must not surface the key."""
        f = classify_failure(
            "CLI exited with a non-zero status (1).",
            stderr_tail=(
                "403 daily limit for key sk-or-v1-deadbeefcafebabe01234567 on openrouter",
            ),
        )
        self.assertEqual(f.kind, "quota_exhausted")
        self.assertNotIn("deadbeefcafebabe", f.evidence or "")

    def test_detail_is_redacted_too(self):
        f = classify_failure("401 Unauthorized for sk-ant-api03-abcdefgh12345678")
        self.assertEqual(f.kind, "auth")
        self.assertNotIn("abcdefgh12345678", f.detail)


if __name__ == "__main__":
    unittest.main()
