"""Secret redaction for channel-facing error text.

Lead condition C1 (round F3): the bridge may quote at most a single matched
stderr line into the channel, and that line must never carry a credential.
CLI stderr routinely echoes request URLs and Authorization headers, and a real
key has leaked through a bridge session once already — so redaction is a
load-bearing guarantee, not a nicety, and gets its own test file.
"""
from __future__ import annotations

import unittest

from mm_bridge.redaction import redact_secrets


class ApiKeyTests(unittest.TestCase):
    def test_sk_prefixed_key_is_masked(self):
        out = redact_secrets("401 from https://api.openai.com (sk-proj-AbCd1234EfGh5678)")
        self.assertNotIn("AbCd1234EfGh5678", out)
        self.assertIn("401", out)

    def test_openrouter_style_key_is_masked(self):
        out = redact_secrets("Authorization failed for sk-or-v1-deadbeefcafebabe0123456789")
        self.assertNotIn("deadbeefcafebabe", out)

    def test_bearer_token_is_masked(self):
        out = redact_secrets("headers: {'Authorization': 'Bearer abc123def456ghi789'}")
        self.assertNotIn("abc123def456ghi789", out)

    def test_key_value_pair_is_masked(self):
        out = redact_secrets("GET /v1/chat?api_key=Zm9vYmFyYmF6cXV4MTIzNA")
        self.assertNotIn("Zm9vYmFyYmF6cXV4MTIzNA", out)

    def test_token_assignment_is_masked(self):
        out = redact_secrets("token = 9f8e7d6c5b4a39281706abcdef")
        self.assertNotIn("9f8e7d6c5b4a39281706abcdef", out)

    def test_long_hex_run_is_masked(self):
        out = redact_secrets("session 0123456789abcdef0123456789abcdef0123 failed")
        self.assertNotIn("0123456789abcdef0123456789abcdef0123", out)


class PreservationTests(unittest.TestCase):
    """Redaction must not shred the signal we classify on."""

    def test_ordinary_error_text_is_untouched(self):
        text = "Error: 403 Forbidden - Key limit exceeded: daily limit reached"
        self.assertEqual(redact_secrets(text), text)

    def test_status_codes_and_words_survive(self):
        out = redact_secrets("openrouter 429 Too Many Requests (Bearer sk-or-v1-abcdefgh12345678)")
        self.assertIn("openrouter", out)
        self.assertIn("429", out)
        self.assertIn("Too Many Requests", out)
        self.assertNotIn("abcdefgh12345678", out)

    def test_empty_input(self):
        self.assertEqual(redact_secrets(""), "")


if __name__ == "__main__":
    unittest.main()
