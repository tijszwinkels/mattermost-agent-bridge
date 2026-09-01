"""Channel Purpose config parser.

Parses a Mattermost channel's "Purpose" text into a backend/model/flags config.
Never raises — unknown tokens become warnings the bridge surfaces as a channel
message.

Spec: specs/20260417-mattermost-bridge-v2/requirements.md §3
      specs/20260417-mattermost-bridge-v2/design.md §2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

KNOWN_BACKENDS: frozenset[str] = frozenset({"claude", "codex", "pi", "opencode"})

# Purpose backend tokens the user may write that canonicalise to one of
# ``KNOWN_BACKENDS``. Lets operators paste the harness-wire name
# (``claude-code``) into Channel Purpose without it being treated as an
# unknown token. Aliases are matched case-insensitively.
_BACKEND_ALIASES: dict[str, str] = {
    "claude-code": "claude",
    "claudecode": "claude",
    "claude code": "claude",
}

# A standalone line equal to this string splits the Channel Purpose into a
# config section (parsed by `parse()`) and a trailing section reserved for
# informational content such as the resume-command block written by
# `resume_header`. Anything after the first standalone separator line is
# ignored by `parse()`, so adding/refreshing a resume block never mutates
# the parsed config — and the bridge can update either section
# independently as long as it round-trips through `split_config_section`
# and `join_sections`.
SECTION_SEPARATOR = "---"

MENTION_ONLY_TOKEN = "mention-only"
NOAUTORESPOND_TOKEN = "noautorespond"  # synonym for mention-only
AUTORESPOND_TOKEN = "autorespond"       # explicit mention_only=False
# Forgiving spelling variants — users naturally type "autoresponse" /
# "noautoresponse" (with trailing `e`). Accept both as synonyms.
NOAUTORESPOND_ALIASES: frozenset[str] = frozenset({
    NOAUTORESPOND_TOKEN, "noautoresponse", MENTION_ONLY_TOKEN,
})
AUTORESPOND_ALIASES: frozenset[str] = frozenset({
    AUTORESPOND_TOKEN, "autoresponse",
})
CWD_PREFIX = "cwd="


@dataclass
class PurposeConfig:
    backend: str
    model: str | None
    mention_only: bool = False
    cwd: str | None = None
    warnings: list[str] = field(default_factory=list)


def canonical_backend(name: str | None) -> str | None:
    """Canonicalise a backend name: lowercase + resolve wire aliases.

    ``"claude-code"`` / ``"claudecode"`` → ``"claude"``. Returns the input
    unchanged when it's falsy (None / empty). Unknown names pass through
    lowercased so callers can still use them (free-text tolerance).

    WHY callers need this: agent-harness reports its own wire names
    (``claude-code``), while Channel Purpose, `.backend` and every
    operator-facing message speak the short form (``claude``). Anything
    reading a backend off a harness record — `_backend_for_error`,
    `_resume_meta_for` — must come through here so the channel never sees a
    name the user can't type back.
    """
    if not name:
        return name
    lc = name.lower()
    return _BACKEND_ALIASES.get(lc, lc)


def split_config_section(text: str) -> tuple[str, str]:
    """Return ``(config, rest)`` split on the first standalone separator line.

    A "standalone" separator is a line that equals ``SECTION_SEPARATOR``
    after stripping surrounding whitespace. Both halves are separator-free.
    Either may be empty. A line that contains ``---`` as part of a longer
    string (e.g. ``"--- not really ---"``) does NOT trigger a split.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == SECTION_SEPARATOR:
            head = "\n".join(lines[:i]).rstrip("\n")
            tail = "\n".join(lines[i + 1:]).strip("\n")
            return head, tail
    return text, ""


def join_sections(config: str, rest: str) -> str:
    """Glue a config section and a trailing section with a separator line.

    Each side is stripped of surrounding whitespace. If either side is empty
    the separator is omitted (so empty input round-trips cleanly through
    ``split_config_section``). Layout uses blank lines around the separator
    for readability in Mattermost's Purpose panel.
    """
    c = config.strip()
    r = rest.strip()
    if not r:
        return c
    if not c:
        return f"{SECTION_SEPARATOR}\n{r}"
    return f"{c}\n\n{SECTION_SEPARATOR}\n\n{r}"


def _tokenize(purpose: str) -> list[str]:
    """Split on `,`, strip whitespace, drop empty tokens.

    Case is preserved — callers lowercase at comparison time. We can't
    lowercase eagerly because `cwd=` values carry case-sensitive paths.
    """
    return [tok.strip() for tok in purpose.split(",") if tok.strip()]


def _parse_cwd_token(token: str) -> tuple[str | None, str | None]:
    """If `token` is a `cwd=…` assignment, return (value_or_None, warning_or_None).

    Returns (None, None) when the token isn't a cwd assignment at all.
    Returns (path, None) when a valid absolute path is supplied.
    Returns (None, warning) when the assignment is malformed.

    Tolerates whitespace around the `=` (`cwd = /path`). The `cwd` key is
    case-insensitive but the path value preserves case.
    """
    lhs, sep, raw_value = token.partition("=")
    if not sep or lhs.strip().lower() != "cwd":
        return None, None
    value = raw_value.strip()
    if not value:
        return None, "Could not parse Channel Purpose `cwd=` token — value is empty."
    if not value.startswith("/"):
        return None, (
            f"Channel Purpose `cwd=` value `{value}` must be an absolute path."
        )
    return value, None


def _models_for(
    backend: str, available_models_for: Callable[[str], list[str]]
) -> list[str]:
    """Fetch models for a backend, tolerating callable failures.

    We treat a failing/raising models callable as "no models known" so that
    parsing never raises — the caller can still use defaults.
    """
    try:
        models = available_models_for(backend) or []
    except Exception:
        return []
    return [m.lower() for m in models]


def parse(
    purpose: str,
    default_backend: str,
    default_model: str | None,
    available_models_for: Callable[[str], list[str]],
    *,
    default_autorespond: bool = True,
    strict_catalog: bool = False,
) -> PurposeConfig:
    """Parse a channel purpose string into a PurposeConfig.

    Arguments:
        purpose: raw Purpose field from Mattermost.
        default_backend: backend to use when purpose is empty or unparseable.
        default_model: model to use when only the backend is specified.
        available_models_for: callable returning the list of model names for
            a given backend. Called at most once per backend during parsing.
        default_autorespond: when True, channels without an explicit
            mention-only / noautorespond / autorespond token default to
            responding to every message. When False, they default to
            mention-only (explicit `autorespond` needed to turn it off).
        strict_catalog: when True, disable the "empty model catalog →
            accept any token as a model name" fallback, so ordinary words
            like "Hi!" aren't interpreted as unknown-but-acceptable model
            names just because the harness doesn't enumerate models. NOTE:
            the bridge no longer parses chat-message content as config (the
            first-message / pre-session selectors were removed 2026-07), so
            this option currently has no production caller; it's retained as
            a parser capability and exercised directly by test_purpose.py.

    Never raises. Unknown tokens are collected into PurposeConfig.warnings.
    """
    default_mention_only = not default_autorespond

    # Ignore the resume-block trailing section — operators must be able to
    # extend Purpose with documentation/links/code blocks without those
    # generating spurious warnings or fighting the config parser.
    config_section, _trailing = split_config_section(purpose)
    raw_tokens = _tokenize(config_section)

    if not raw_tokens:
        return PurposeConfig(
            backend=default_backend,
            model=default_model,
            mention_only=default_mention_only,
            cwd=None,
            warnings=[],
        )

    warnings: list[str] = []

    # Step 2a: extract cwd= and autorespond/noautorespond/mention-only tokens
    # up-front so they work positionally anywhere. Paths are case-sensitive so
    # we keep raw tokens until this point.
    cwd: str | None = None
    mention_only_override: bool | None = None
    remaining: list[str] = []
    for tok in raw_tokens:
        value, warn = _parse_cwd_token(tok)
        if warn:
            warnings.append(warn)
            continue
        if value is not None:
            if cwd is not None and cwd != value:
                warnings.append(
                    f"Multiple `cwd=` tokens in Channel Purpose — ignoring `{cwd}`, using `{value}`."
                )
            cwd = value
            continue
        tok_lc = tok.lower()
        if tok_lc in NOAUTORESPOND_ALIASES:
            mention_only_override = True
            continue
        if tok_lc in AUTORESPOND_ALIASES:
            mention_only_override = False
            continue
        remaining.append(tok)

    mention_only_effective = (
        mention_only_override if mention_only_override is not None
        else default_mention_only
    )

    if not remaining:
        return PurposeConfig(
            backend=default_backend,
            model=default_model,
            mention_only=mention_only_effective,
            cwd=cwd,
            warnings=warnings,
        )

    first, *rest = remaining
    first_lc = first.lower()
    first_canon = _BACKEND_ALIASES.get(first_lc, first_lc)

    # Step 3: resolve the first token to a backend (and maybe a model).
    backend: str
    model: str | None
    if first_canon in KNOWN_BACKENDS:
        backend = first_canon
        model = None
    else:
        # If the first token is a model name under the default backend,
        # interpret it as "use default backend + this model". When the
        # default backend has no enumerated catalog (US-5.3: live harness
        # returns ``data: []`` for known backends), still accept the token
        # as a model so operators can use models the harness hasn't
        # enumerated.
        default_models = _models_for(default_backend, available_models_for)
        accept_unknown = not default_models and not strict_catalog
        if first_lc in default_models or accept_unknown:
            backend = default_backend
            model = first_lc
        else:
            warnings.append(
                f"Could not parse Channel Purpose token `{first}` — using defaults."
            )
            backend = default_backend
            model = None

    # Cache the model list for the *chosen* backend (may differ from default).
    backend_models = _models_for(backend, available_models_for)

    # Step 4: walk remaining tokens (mention-only / autorespond / cwd were
    # already extracted in Step 2a). With an empty catalog (US-5.3) any
    # otherwise-unrecognised token is taken as a model name verbatim so the
    # bridge can pass it to ``POST /v1/sessions`` unchanged — unless the
    # caller asked for strict_catalog (see the parameter docstring).
    catalog_empty = not backend_models and not strict_catalog
    for token in rest:
        token_lc = token.lower()
        if token_lc in backend_models or (catalog_empty and model is None):
            if model is not None and model != token_lc:
                warnings.append(
                    f"Multiple model tokens in Channel Purpose — ignoring `{model}`, using `{token_lc}`."
                )
            model = token_lc
            continue

        warnings.append(
            f"Could not parse Channel Purpose token `{token}`."
        )

    # Fill in default model if nothing else resolved one.
    # (The test suite treats `default_model` as a backend-agnostic fallback;
    # the bridge layer is responsible for turning names into backend-specific
    # indices at session-create time.)
    if model is None:
        model = default_model

    return PurposeConfig(
        backend=backend,
        model=model,
        mention_only=mention_only_effective,
        cwd=cwd,
        warnings=warnings,
    )


def to_purpose_string(cfg: PurposeConfig, *, default_autorespond: bool) -> str:
    """Serialize a PurposeConfig back into canonical Channel Purpose form.

    Emits tokens in a stable order: backend, model, (mention-only|autorespond),
    cwd. Always emits the mention/autorespond flag explicitly so the Channel
    Purpose documents the effective setting regardless of config defaults.

    The `default_autorespond` argument is accepted for symmetry with `parse()`
    but currently doesn't change the output; kept for future-proofing in case
    we want to elide redundant flags later.

    The result is round-trippable: parse(to_purpose_string(cfg)) == cfg
    (modulo warnings).
    """
    del default_autorespond  # explicit: flag is always emitted
    parts: list[str] = [cfg.backend]
    if cfg.model:
        parts.append(cfg.model)

    parts.append(MENTION_ONLY_TOKEN if cfg.mention_only else AUTORESPOND_TOKEN)

    if cfg.cwd:
        parts.append(f"{CWD_PREFIX}{cfg.cwd}")

    return ", ".join(parts)
