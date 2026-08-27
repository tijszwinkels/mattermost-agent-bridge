"""Pure parsing for channel references (``--channel`` / ``--thread`` inputs).

A reference is one of:

* **id** — a 26-char ``[a-z0-9]{26}`` string (Mattermost channel/post ids);
  used verbatim, no server call needed.
* **slug** — a bare channel name (``"general"``), or a full channel URL
  ``https://host/<team>/channels/<slug>``; resolved to a channel id via
  ``GET /teams/name/{team}/channels/name/{slug}``.
* **permalink** — ``https://host/<team>/pl/<post_id>``; the post is fetched
  and its ``channel_id`` + thread root become the anchor.

Parsing is deliberately pure (no config, no network) so the URL shapes can
be unit-tested in isolation; the network half lives in :mod:`mm_bridge.cli`
and :mod:`mm_bridge.mm_client`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

__all__ = ["ChannelRef", "ChannelRefError", "parse_channel_ref"]

# Mattermost channel and post ids are base36(ish) 26-char lowercase strings.
_ID_RE = re.compile(r"^[a-z0-9]{26}$")


class ChannelRefError(ValueError):
    """A ``--channel``/``--thread`` value that is none of id / slug / URL."""


@dataclass(frozen=True)
class ChannelRef:
    """Parsed channel reference.

    ``kind`` drives the resolution path; the other fields are populated
    per kind (``slug``+``team`` for slug refs, ``post_id`` (+``team``) for
    permalinks, ``host`` for any URL so the client can refuse foreign hosts).
    """

    kind: Literal["id", "slug", "permalink"]
    value: str
    slug: str | None = None
    team: str | None = None
    post_id: str | None = None
    host: str | None = None


def _is_slug(s: str) -> bool:
    # Mattermost channel names are lowercase word/slug tokens; anything with
    # whitespace or path separators is *not* a bare slug.
    return bool(s) and " " not in s and "/" not in s


def _url_parts(value: str) -> tuple[str, list[str]] | None:
    """Return ``(host, path_segments)`` if *value* is an http(s) URL."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    segments = [s for s in parts.path.split("/") if s]
    return parts.hostname.lower(), segments


def parse_channel_ref(value: str) -> ChannelRef:
    """Parse a ``--channel`` value into a :class:`ChannelRef`.

    Raises :class:`ChannelRefError` (a ``ValueError``) if *value* matches
    none of the supported shapes — the CLI reports that as a usage error.
    """
    if _ID_RE.match(value):
        return ChannelRef(kind="id", value=value)

    url = _url_parts(value)
    if url is not None:
        host, segs = url
        if len(segs) == 3 and segs[1] == "channels" and _is_slug(segs[2]):
            return ChannelRef(
                kind="slug", value=value, team=segs[0], slug=segs[2], host=host,
            )
        if len(segs) == 3 and segs[1] == "pl" and segs[2]:
            return ChannelRef(
                kind="permalink",
                value=value,
                team=segs[0],
                post_id=segs[2],
                host=host,
            )
        raise ChannelRefError(
            f"not a supported channel URL — expected "
            f"'…/<team>/channels/<slug>' or '…/<team>/pl/<post_id>': {value!r}"
        )

    if _is_slug(value):
        return ChannelRef(kind="slug", value=value, slug=value)

    raise ChannelRefError(
        f"cannot parse as a channel id, slug, or URL: {value!r}"
    )
