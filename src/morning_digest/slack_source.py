"""Pull @mentions + DMs addressed to Michelle from Slack, for a date range.

Uses the Slack search.messages API, which requires a *user* token (xoxp-) with
the `search:read` scope — a bot token cannot search. We gather the raw messages
and format them as plain text to hand to Claude as an extra digest source.
"""

import os
from datetime import date

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

USER_TOKEN_ENV = "SLACK_MORNING_TASKS_DIGEST_USER_TOKEN"


def _client() -> WebClient | None:
    token = os.environ.get(USER_TOKEN_ENV)
    if not token:
        return None
    return WebClient(token=token)


def fetch_messages(user_id: str, after: date, before: date | None = None) -> str:
    """Return a plain-text rundown of @mentions + DMs in [after, before].

    `after` and `before` are inclusive-ish calendar dates; Slack's search uses
    `after:`/`before:` which are exclusive of the named day, so we widen by one
    day on each side and let the formatting note the intended window.
    Returns "" if no user token is configured (so v1 still works without it).
    """
    client = _client()
    if client is None:
        return ""

    # Slack after:/before: are exclusive of the given date.
    after_q = f"after:{after.isoformat()}"
    before_q = f" before:{before.isoformat()}" if before else ""

    matches: list[dict] = []
    seen: set[str] = set()
    for base in [f"<@{user_id}>", "to:me"]:
        query = f"{base} {after_q}{before_q}".strip()
        try:
            result = client.search_messages(
                query=query, sort="timestamp", sort_dir="asc", count=100
            )
        except SlackApiError as e:
            return f"(Slack search failed: {e.response.get('error', 'unknown')})"
        for m in result.get("messages", {}).get("matches", []):
            ch = m.get("channel", {})
            ch_id = ch.get("id", "") if isinstance(ch, dict) else ""
            key = f"{ch_id}:{m.get('ts','')}"
            if key in seen:
                continue
            seen.add(key)
            matches.append(m)

    if not matches:
        return "(no @mentions or DMs found in this window)"

    lines = []
    for m in matches:
        ch = m.get("channel", {})
        ch_name = ch.get("name") if isinstance(ch, dict) else None
        ch_id = ch.get("id", "") if isinstance(ch, dict) else ""
        where = f"#{ch_name}" if ch_name else (f"DM" if ch_id.startswith("D") else ch_id)
        sender = m.get("username") or m.get("user", "someone")
        text = (m.get("text", "") or "").replace("\n", " ").strip()
        permalink = m.get("permalink", "")
        lines.append(f"- [{where}] {sender}: {text}" + (f"  {permalink}" if permalink else ""))

    return "\n".join(lines)
