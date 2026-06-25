"""Pull Slack activity relevant to the user, for a date range.

Uses the Slack search.messages API, which requires a *user* token (xoxp-) with
the `search:read` scope — a bot token cannot search. We gather raw messages and
format them (grouped by channel) as plain text to hand to Claude as a source.

Three query streams:
  - `<@U…>`  : places the user was @mentioned (group channels, threads)
  - `to:me`  : DMs sent to the user
  - `from:me`: what the user said — the evidence for "what did I do / wrap up?"
The `from:me` stream is only included when include_sent=True (weekly review),
since the daily digest only cares about incoming asks they might have missed.
"""

import os
from datetime import date

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

USER_TOKEN_ENV = "SLACK_MORNING_TASKS_DIGEST_USER_TOKEN"
BOT_TOKEN_ENV = "SLACK_MORNING_TASKS_DIGEST_BOT_TOKEN"

# search.messages caps page size at 100; paginate up to this many pages per query.
_PER_PAGE = 100
_MAX_PAGES = 10  # up to ~1000 msgs/stream — covers a full week comfortably


def _client() -> WebClient | None:
    token = os.environ.get(USER_TOKEN_ENV)
    if not token:
        return None
    return WebClient(token=token)


def _bot_user_id() -> str | None:
    """The digest bot's own Slack user id, so we can drop its own posts.

    The bot DMs the user a "🚀 Started / ❌ Failed / 🌅 Today's tasks" message for
    every click. Those land in the same DM the next digest searches, so without
    this they get re-ingested as "activity" — a feedback loop that buries (and
    misleads the model about) the real request the task came from. Identifying
    the bot by user id lets us strip its posts no matter which channel they're in.
    """
    token = os.environ.get(BOT_TOKEN_ENV)
    if not token:
        return None
    try:
        return WebClient(token=token).auth_test().get("user_id")
    except SlackApiError:
        return None


def _search_all(client: WebClient, query: str) -> list[dict]:
    """Fetch every page (up to _MAX_PAGES) for a query."""
    out: list[dict] = []
    page = 1
    while page <= _MAX_PAGES:
        resp = client.search_messages(
            query=query, sort="timestamp", sort_dir="asc",
            count=_PER_PAGE, page=page,
        )
        msgs = resp.get("messages", {})
        out.extend(msgs.get("matches", []))
        paging = msgs.get("paging", {}) or {}
        if page >= paging.get("pages", 1):
            break
        page += 1
    return out


def _where(m: dict) -> str:
    ch = m.get("channel", {})
    name = ch.get("name") if isinstance(ch, dict) else None
    ch_id = ch.get("id", "") if isinstance(ch, dict) else ""
    if name:
        return f"#{name}"
    if str(ch_id).startswith("D"):
        return "DM"
    if str(ch_id).startswith("G") or str(ch_id).startswith("mpdm"):
        return "group-DM"
    return ch_id or "?"


def fetch_messages(
    user_id: str,
    after: date,
    before: date | None = None,
    include_sent: bool = False,
) -> str:
    """Return a plain-text, channel-grouped rundown of Slack activity in the window.

    Returns "" if no user token is configured (so the bot still runs without it).
    """
    client = _client()
    if client is None:
        return ""

    after_q = f"after:{after.isoformat()}"
    before_q = f" before:{before.isoformat()}" if before else ""
    win = f"{after_q}{before_q}".strip()

    streams = [
        ("@mention", f"<@{user_id}> {win}"),
        ("dm-to-me", f"to:me {win}"),
    ]
    if include_sent:
        streams.append(("i-said", f"from:me {win}"))

    bot_uid = _bot_user_id()

    # channel -> list of (ts, kind, sender, text, permalink)
    by_channel: dict[str, list[tuple]] = {}
    seen: set[str] = set()
    try:
        for kind, query in streams:
            for m in _search_all(client, query):
                # Drop the digest bot's own posts (🚀 Started / ❌ Failed / the
                # digest itself). They live in the same DM this search scans, so
                # re-ingesting them creates a feedback loop and misleads the model.
                if bot_uid and m.get("user") == bot_uid:
                    continue
                ch = m.get("channel", {})
                ch_id = ch.get("id", "") if isinstance(ch, dict) else ""
                ts = m.get("ts", "")
                key = f"{ch_id}:{ts}"
                if key in seen:
                    continue
                seen.add(key)
                where = _where(m)
                sender = "me" if kind == "i-said" else (m.get("username") or m.get("user", "someone"))
                text = (m.get("text", "") or "").replace("\n", " ").strip()
                by_channel.setdefault(where, []).append(
                    (ts, kind, sender, text, m.get("permalink", ""))
                )
    except SlackApiError as e:
        return f"(Slack search failed: {e.response.get('error', 'unknown')})"

    if not by_channel:
        return "(no Slack activity found in this window)"

    # Most-active channels first; messages within a channel chronological.
    lines: list[str] = []
    for where, items in sorted(by_channel.items(), key=lambda kv: -len(kv[1])):
        items.sort(key=lambda x: x[0])
        lines.append(f"\n*{where}* ({len(items)} msgs)")
        for _ts, kind, sender, text, link in items:
            tag = {"@mention": "@", "dm-to-me": "→", "i-said": "ME"}.get(kind, "")
            snippet = text[:300]
            lines.append(f"  [{tag}] {sender}: {snippet}" + (f"  {link}" if link else ""))
    return "\n".join(lines).strip()
