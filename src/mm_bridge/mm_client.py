"""Mattermost REST + WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
from mattermostautodriver import Driver

logger = logging.getLogger(__name__)


# WebSocket events the bridge dispatches on.
_HANDLED_EVENTS = {
    "posted", "user_added", "user_removed", "channel_updated", "channel_created",
}

# Upper bound on tracked own-post IDs. Only the most-recent IDs are kept;
# older entries age out of both the deque and the lookup set.
_OWN_POST_HISTORY_MAX = 1024


class MattermostClient:
    """Mattermost client: sync Driver for REST, raw aiohttp for WebSocket."""

    def __init__(
        self,
        url: str,
        port: int,
        scheme: str,
        token: str,
        team_name: str,
    ):
        self._driver = Driver({
            "url": url,
            "port": port,
            "scheme": scheme,
            "token": token,
            "verify": False,
        })
        self._url = url
        self._port = port
        self._scheme = scheme
        self._token = token
        self._team_name = team_name
        self._team_id: str = ""
        self._bot_user_id: str = ""
        self._bot_username: str = ""
        # Post IDs this process authored via post()/post_message()/
        # update_post(), used to suppress the WS echo Mattermost sends
        # back for our own posts. Without this, the dispatcher would
        # either see its own posts as user input, or (the old bug) blanket-
        # filter the shared bot identity and drop legitimate posts from
        # sibling mm-bridge sessions, other tools using MM_BOT_TOKEN, or
        # humans posting as @claude.
        self._own_post_ids: deque[str] = deque(maxlen=_OWN_POST_HISTORY_MAX)
        self._own_post_id_set: set[str] = set()

    # ----- setup -----

    def login(self) -> None:
        self._driver.login()
        me = self._driver.users.get_user("me")
        self._bot_user_id = me["id"]
        self._bot_username = me["username"]
        team = self._driver.teams.get_team_by_name(self._team_name)
        self._team_id = team["id"]
        logger.info("Logged in as %s (team=%s)", me["username"], self._team_name)

    @property
    def bot_user_id(self) -> str:
        return self._bot_user_id

    @property
    def bot_username(self) -> str:
        return self._bot_username

    @property
    def team_id(self) -> str:
        return self._team_id

    def is_own_post(self, post_id: str) -> bool:
        """Return True when ``post_id`` was authored by this process."""
        return bool(post_id) and post_id in self._own_post_id_set

    def _record_own_post(self, post_id: str | None) -> None:
        if not post_id or post_id in self._own_post_id_set:
            return
        if len(self._own_post_ids) == self._own_post_ids.maxlen:
            evicted = self._own_post_ids[0]
            self._own_post_id_set.discard(evicted)
        self._own_post_ids.append(post_id)
        self._own_post_id_set.add(post_id)

    # ----- posts -----

    def post_message(self, channel_id: str, message: str) -> dict:
        resp = self._driver.posts.create_post(options={
            "channel_id": channel_id,
            "message": message,
        })
        self._record_own_post(resp.get("id") if isinstance(resp, dict) else None)
        return resp

    def post(
        self,
        channel_id: str,
        message: str,
        *,
        file_ids: list[str] | None = None,
        root_id: str | None = None,
        props: dict | None = None,
    ) -> dict:
        options: dict = {"channel_id": channel_id, "message": message}
        if file_ids:
            options["file_ids"] = file_ids
        if root_id:
            options["root_id"] = root_id
        if props is not None:
            options["props"] = props
        resp = self._driver.posts.create_post(options=options)
        self._record_own_post(resp.get("id") if isinstance(resp, dict) else None)
        return resp

    def update_post(self, post_id: str, message: str) -> dict:
        resp = self._driver.posts.update_post(post_id, options={
            "id": post_id,
            "message": message,
        })
        # Re-record in case the post id aged out of the deque between
        # create and edit — MM will echo the edited post back over WS.
        self._record_own_post(post_id)
        return resp

    def delete_post(self, post_id: str, *, permanent: bool = False) -> None:
        """Delete a post. `permanent=True` drops the DB row (no soft-delete,
        no edit-history row retained) — requires `ServiceSettings.
        EnableAPIPostDeletion=true` server-side. If that flag is off, MM
        returns 501; callers should catch and fall back.
        """
        if permanent:
            self._driver.client.delete(
                f"/api/v4/posts/{post_id}", params={"permanent": "true"},
            )
            return
        self._driver.posts.delete_post(post_id)

    def get_posts(self, channel_id: str, limit: int) -> list[dict]:
        """Most-recent N posts, returned oldest-first."""
        resp = self._driver.posts.get_posts_for_channel(
            channel_id, params={"per_page": limit},
        )
        order = resp.get("order", [])
        posts = resp.get("posts", {})
        return [posts[pid] for pid in reversed(order) if pid in posts]

    def get_post(self, post_id: str) -> dict:
        return self._driver.posts.get_post(post_id)

    def get_posts_since(
        self, channel_id: str, since_ms: int, per_page: int = 200,
    ) -> list[dict]:
        """Posts in `channel_id` created after `since_ms`, oldest first.

        Mattermost's `since` endpoint returns all posts since the given
        epoch in a single page when the window is small; we still loop
        with `?page` as a defensive measure and cap at a hard safety
        ceiling so a misuse doesn't pull the whole channel.
        """
        safety_cap = 2000
        collected: dict[str, dict] = {}
        order: list[str] = []
        page = 0
        while len(collected) < safety_cap:
            resp = self._driver.posts.get_posts_for_channel(
                channel_id,
                params={
                    "since": since_ms, "per_page": per_page, "page": page,
                },
            )
            resp_order = resp.get("order", [])
            resp_posts = resp.get("posts", {})
            if not resp_order:
                break
            added = 0
            for pid in resp_order:
                if pid in collected:
                    continue
                collected[pid] = resp_posts[pid]
                order.append(pid)
                added += 1
            if added == 0:
                break
            if len(resp_order) < per_page:
                break
            page += 1
        posts = [collected[pid] for pid in order if pid in collected]
        posts.sort(key=lambda p: p.get("create_at", 0))
        return posts

    def get_thread_posts(self, root_id: str) -> list[dict]:
        """All posts in the thread rooted at `root_id`, oldest first."""
        resp = self._driver.posts.get_post_thread(root_id)
        posts = resp.get("posts", {})
        order = resp.get("order", []) or list(posts.keys())
        ordered = [posts[pid] for pid in order if pid in posts]
        ordered.sort(key=lambda p: p.get("create_at", 0))
        return ordered

    def get_file_info(self, file_id: str) -> dict:
        """Return the metadata record for a single uploaded file."""
        return self._driver.files.get_file_info(file_id)

    # ----- channels -----

    def create_channel(
        self, name: str, display_name: str, purpose: str = ""
    ) -> dict:
        return self._driver.channels.create_channel(options={
            "team_id": self._team_id,
            "name": name,
            "display_name": display_name,
            "purpose": purpose,
            "type": "O",
        })

    def set_channel_header(self, channel_id: str, header: str) -> None:
        self._driver.channels.patch_channel(channel_id, options={"header": header})

    def set_channel_purpose(self, channel_id: str, purpose: str) -> None:
        self._driver.channels.patch_channel(channel_id, options={"purpose": purpose})

    def rename_channel(self, channel_id: str, display_name: str) -> None:
        self._driver.channels.patch_channel(channel_id, options={
            "display_name": display_name,
        })

    def get_channel(self, channel_id: str) -> dict:
        return self._driver.channels.get_channel(channel_id)

    def get_channel_by_name(self, team_name: str, channel_name: str) -> dict:
        """Resolve a channel slug to its record (id, name, ...).

        Uses GET /teams/name/{team}/channels/name/{channel}, which works for
        private channels the bot is a member of (unlike the public listing).
        Raises on 404 (unknown name, not a member, or archived).
        """
        return self._driver.channels.get_channel_by_name_for_team_name(
            team_name, channel_name,
        )

    def get_channel_members(self, channel_id: str) -> list[dict]:
        """Return membership records (user_id + roles) for `channel_id`."""
        return self._driver.channels.get_channel_members(channel_id)

    def remove_self_from_channel(self, channel_id: str) -> None:
        """Remove the bot from a channel."""
        self._driver.client.delete(
            f"/api/v4/channels/{channel_id}/members/{self._bot_user_id}"
        )

    def get_bot_channel_ids(self) -> set[str]:
        """Return the set of channel IDs the bot currently belongs to in its team."""
        channels = self.list_bot_channels()
        return {c["id"] for c in channels}

    def list_bot_channels(self) -> list[dict]:
        """Return the full channel records for the bot's team memberships."""
        return self._driver.channels.get_channels_for_team_for_user(
            self._bot_user_id, self._team_id,
        )

    # Page size for paginated team-channel listing. 200 is the Mattermost
    # API's documented maximum (`per_page`), so it keeps round-trips
    # bounded on teams up to a few thousand channels.
    _PUBLIC_CHANNELS_PAGE_SIZE = 200
    # Hard cap on the number of pages we'll fetch before giving up — a
    # safety net against a misbehaving server returning full pages
    # indefinitely. At 200 channels/page this still allows up to 10k
    # channels per team before the cap kicks in.
    _PUBLIC_CHANNELS_MAX_PAGES = 50

    def list_public_team_channels(self) -> list[dict]:
        """List all public channels in the bot's team.

        Pages through ``GET /teams/{team_id}/channels`` until a short or
        empty page signals the end of the list. Without explicit
        pagination, the Mattermost API defaults to ``per_page=60``,
        which silently truncates the result on teams with >60 public
        channels — the symptom was that newly-created public channels
        beyond the first page were never auto-joined by the reconciler.
        """
        out: list[dict] = []
        per_page = self._PUBLIC_CHANNELS_PAGE_SIZE
        for page in range(self._PUBLIC_CHANNELS_MAX_PAGES):
            chunk = self._driver.channels.get_public_channels_for_team(
                self._team_id,
                params={"page": page, "per_page": per_page},
            ) or []
            out.extend(chunk)
            if len(chunk) < per_page:
                break
        return out

    def join_channel(self, channel_id: str) -> None:
        """Add the bot to `channel_id`."""
        self._driver.channels.add_channel_member(channel_id, options={
            "user_id": self._bot_user_id,
        })

    def join_all_public_team_channels(self) -> list[str]:
        """Join every public team channel the bot isn't already in.

        Returns the list of channel IDs newly joined. Per-channel failures
        are logged but don't abort the batch.
        """
        already = self.get_bot_channel_ids()
        joined: list[str] = []
        for ch in self.list_public_team_channels():
            cid = ch["id"]
            if cid in already:
                continue
            try:
                self.join_channel(cid)
                joined.append(cid)
                logger.info(
                    "Auto-joined channel: %s (%s)",
                    ch.get("display_name") or cid, cid,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to auto-join channel %s (%s): %s",
                    ch.get("display_name") or cid, cid, exc,
                )
        return joined

    # ----- users -----

    def get_user(self, user_id: str) -> dict:
        return self._driver.users.get_user(user_id)

    def get_user_by_username(self, username: str) -> dict:
        return self._driver.users.get_user_by_username(username)

    def invite_user(self, channel_id: str, user_id: str) -> dict:
        """Add a user to a channel (thin wrapper around add_channel_member)."""
        return self._driver.channels.add_channel_member(channel_id, options={
            "user_id": user_id,
        })

    def publish_user_typing(
        self, channel_id: str, parent_id: str | None = None
    ) -> None:
        """Publish a 'user is typing' event from the bot to the channel."""
        options: dict = {"channel_id": channel_id}
        if parent_id:
            options["parent_id"] = parent_id
        self._driver.users.publish_user_typing(
            self._bot_user_id, options=options
        )

    # ----- files -----

    def upload_file(self, channel_id: str, path: Path) -> str:
        """Upload a file to a channel; return its file_id."""
        with path.open("rb") as fh:
            resp = self._driver.files.upload_file(
                files={"files": (path.name, fh)},
                data={"channel_id": channel_id},
            )
        infos = resp.get("file_infos") or []
        if not infos:
            raise RuntimeError(f"upload_file returned no file_infos: {resp!r}")
        return infos[0]["id"]

    def download_file(self, file_id: str) -> bytes:
        """Fetch the raw bytes of an uploaded file from Mattermost."""
        # Bypass self._driver.files.get_file(), which routes through
        # Client.get() and auto-parses any application/json response into
        # a dict — corrupting JSON file downloads. make_request() returns
        # the raw httpx Response regardless of content-type.
        resp = self._driver.client.make_request(
            "get", f"/api/v4/files/{file_id}"
        )
        return resp.content

    def get_max_file_size(self) -> int:
        """Read MaxFileSize from the MM server config; 50MB fallback."""
        try:
            cfg = self._driver.system.get_client_configuration(params={"format": "old"})
            return int(cfg.get("MaxFileSize", 50 * 1024 * 1024))
        except Exception:
            logger.debug("get_max_file_size failed, using 50MB fallback", exc_info=True)
            return 50 * 1024 * 1024

    # ----- WebSocket -----

    async def listen_websocket(self, handlers: dict[str, Callable[..., Awaitable[None]]]) -> None:
        """Listen on the MM WS; dispatch events to named handlers.

        Expected keys in `handlers` (all optional):
          - "posted"          → fn(post: dict)
          - "user_added"      → fn(channel_id: str, user_id: str)
          - "user_removed"    → fn(channel_id: str, user_id: str)
          - "channel_updated" → fn(channel: dict)
          - "channel_created" → fn(channel_id: str)

        `posted` events whose post id matches one this process just
        authored are filtered before dispatch (the echo suppression).
        Posts by *other* actors that happen to share the bot identity
        (sibling mm-bridge sessions, scripts using MM_BOT_TOKEN, humans
        posting as the bot) still reach the handler.
        """
        ws_scheme = "wss" if self._scheme == "https" else "ws"
        ws_url = f"{ws_scheme}://{self._url}:{self._port}/api/v4/websocket"

        while True:
            try:
                await self._ws_connect(ws_url, handlers)
            except (aiohttp.ClientError, ConnectionError) as exc:
                logger.warning("MM WebSocket lost (%s), reconnecting in 2s...", exc)
                await asyncio.sleep(2)
            except Exception:
                logger.exception("MM WebSocket error, reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _ws_connect(
        self,
        url: str,
        handlers: dict[str, Callable[..., Awaitable[None]]],
    ) -> None:
        async with aiohttp.ClientSession() as session:
            # heartbeat=30: aiohttp pings every 30s of silence and waits
            # heartbeat/2 for the pong; a missed pong surfaces as
            # WSMsgType.ERROR, the loop below breaks, and the outer
            # reconnect loop in listen_websocket() recovers — so a
            # half-open TCP socket can't strand us in ``async for msg``
            # (worst-case detection ≈ 45s).
            async with session.ws_connect(url, heartbeat=30) as ws:
                await ws.send_json({
                    "seq": 1,
                    "action": "authentication_challenge",
                    "data": {"token": self._token},
                })
                logger.info("MM WebSocket connected and authenticated")

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            event = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        await self._dispatch_event(event, handlers)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break

    async def _dispatch_event(
        self,
        event: dict,
        handlers: dict[str, Callable[..., Awaitable[None]]],
    ) -> None:
        event_type = event.get("event")
        if event_type not in _HANDLED_EVENTS:
            return

        data = event.get("data", {}) or {}
        broadcast = event.get("broadcast", {}) or {}

        try:
            if event_type == "posted":
                handler = handlers.get("posted")
                if not handler:
                    return
                post_json = data.get("post")
                if not post_json:
                    return
                try:
                    post = json.loads(post_json)
                except json.JSONDecodeError:
                    return
                # Race-safety: id tracking only works because our REST
                # post calls (`post`, `post_message`, `update_post`) are
                # synchronous and block this event loop — the id is
                # recorded before control returns to the WS read loop,
                # so the subsequent echo frame always sees it. If those
                # calls are ever wrapped in `asyncio.to_thread` or made
                # natively async, add a content-hash fallback (recent
                # channel_id + message hash window) to close the race.
                if self.is_own_post(post.get("id") or ""):
                    return
                await handler(post)

            elif event_type == "user_added":
                handler = handlers.get("user_added")
                if not handler:
                    return
                user_id = data.get("user_id")
                channel_id = broadcast.get("channel_id") or data.get("channel_id")
                if user_id and channel_id:
                    await handler(channel_id, user_id)

            elif event_type == "user_removed":
                handler = handlers.get("user_removed")
                if not handler:
                    return
                user_id = data.get("user_id")
                channel_id = broadcast.get("channel_id") or data.get("channel_id")
                if user_id and channel_id:
                    await handler(channel_id, user_id)

            elif event_type == "channel_updated":
                handler = handlers.get("channel_updated")
                if not handler:
                    return
                channel_json = data.get("channel")
                if not channel_json:
                    return
                try:
                    channel = json.loads(channel_json)
                except (json.JSONDecodeError, TypeError):
                    channel = channel_json if isinstance(channel_json, dict) else None
                if channel:
                    await handler(channel)

            elif event_type == "channel_created":
                handler = handlers.get("channel_created")
                if not handler:
                    return
                channel_id = (
                    data.get("channel_id")
                    or broadcast.get("channel_id")
                )
                if channel_id:
                    await handler(channel_id)
        except Exception:
            logger.exception("handler for %s failed", event_type)
