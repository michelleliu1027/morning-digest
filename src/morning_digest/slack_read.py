"""Read ONE Slack thread, read-only, for an agent that needs its full context.

The digest gives an agent a one-line task ("Verify BrownBoots device IDs …") but
the evidence — the IDs a teammate pasted, the exact ask — lives in the Slack
thread the task came from. The sandboxed agent has no Slack access, so this CLI
is the bridge: given the task's source permalink, it prints the whole thread as
plain text the agent can reason over.

It is strictly read-only (only `conversations.replies` / `conversations.history`,
both GET) and is the ONLY Slack surface a spawned agent is allowed to touch. The
token comes from the same env the listener loaded (.env), so the agent never
holds a token itself — it just shells out to this command.

Usage:
    python -m morning_digest.slack_read <slack-permalink>
    python -m morning_digest.slack_read <channel_id> <thread_ts>
"""

from __future__ import annotations

import os
import re
import sys

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# A user token (xoxp-) reads any thread the user can see; fall back to the bot
# token (xoxb-, only works in channels the bot is in). User token preferred.
_USER_TOKEN_ENV = "SLACK_MORNING_TASKS_DIGEST_USER_TOKEN"
_BOT_TOKEN_ENV = "SLACK_MORNING_TASKS_DIGEST_BOT_TOKEN"

# https://workspace.slack.com/archives/C0123ABCD/p1700000000123456[?thread_ts=…]
_PERMALINK_RE = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")
_THREAD_TS_RE = re.compile(r"[?&]thread_ts=([0-9.]+)")


def _client() -> WebClient | None:
    token = os.environ.get(_USER_TOKEN_ENV) or os.environ.get(_BOT_TOKEN_ENV)
    return WebClient(token=token) if token else None


def _parse_permalink(arg: str) -> tuple[str, str] | None:
    """Resolve a Slack permalink to (channel_id, thread_ts).

    The p-number encodes the message ts (10 digits . 6 digits). If the link
    carries a `thread_ts` query param it points at a reply, so that is the real
    root of the thread; otherwise the message itself is the root.
    """
    m = _PERMALINK_RE.search(arg)
    if not m:
        return None
    channel, secs, micros = m.group(1), m.group(2), m.group(3)
    tm = _THREAD_TS_RE.search(arg)
    thread_ts = tm.group(1) if tm else f"{secs}.{micros}"
    return channel, thread_ts


# How many surrounding channel messages to pull when a permalink points at a
# standalone message rather than a real (nested-reply) thread. The ask + the
# evidence often sit in separate top-level messages a few lines apart, so we grab
# a window on each side to reconstruct the actual back-and-forth.
_CONTEXT_BEFORE = 15
_CONTEXT_AFTER = 15


def _missing_scope_msg(channel: str) -> str:
    need = {"D": "im:history", "G": "mpim:history"}.get(channel[:1], "channels:history")
    return (f"(Slack read failed: missing_scope — add `{need}` "
            f"(and groups:history for private channels) to the token's "
            f"OAuth scopes and reinstall the app)")


def _fetch_context(client, channel: str, ts: str) -> list[dict]:
    """Pull a window of channel messages around `ts` (the message + its neighbours).

    A Slack permalink frequently points at ONE message in a fast-moving channel
    where the real conversation is several sibling messages, not nested replies.
    `conversations_replies` only returns the nested replies (often just the one
    message), so we reconstruct context from the channel timeline: N messages
    before `ts` (latest=ts) and N after (oldest=ts), merged chronologically.
    """
    before = client.conversations_history(
        channel=channel, latest=ts, inclusive=True, limit=_CONTEXT_BEFORE + 1,
    ).get("messages", [])
    after = client.conversations_history(
        channel=channel, oldest=ts, inclusive=False, limit=_CONTEXT_AFTER,
    ).get("messages", [])
    merged: dict[str, dict] = {}
    for m in before + after:
        merged[m.get("ts", "")] = m
    return sorted(merged.values(), key=lambda m: float(m.get("ts") or 0))


def fetch_thread(channel: str, thread_ts: str) -> str:
    """Return the whole thread as plain text, or an error line (never raises).

    If the permalink is a real thread (has nested replies), return those. If it
    is a standalone message, fall back to a window of surrounding channel
    messages so the agent sees the full back-and-forth, not just one line.
    """
    client = _client()
    if client is None:
        return f"(no Slack token configured: set {_USER_TOKEN_ENV} or {_BOT_TOKEN_ENV})"
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=200)
    except SlackApiError as e:
        err = e.response.get("error", "unknown")
        if err == "missing_scope":
            # The token can search but lacks history read. Tell the caller exactly
            # which scope to add (depends on the conversation type) and reinstall.
            return _missing_scope_msg(channel)
        # A bare message (not a thread) has no replies; read the surrounding window.
        if err in ("thread_not_found", "message_not_found"):
            try:
                return _format(channel, _fetch_context(client, channel, thread_ts))
            except SlackApiError as e2:
                if e2.response.get("error") == "missing_scope":
                    return _missing_scope_msg(channel)
                return f"(Slack read failed: {e2.response.get('error', 'unknown')})"
        return f"(Slack read failed: {err})"

    msgs = resp.get("messages", [])
    # A lone root message means the permalink wasn't a real thread — the actual
    # conversation is sibling messages in the channel, so widen to a context window.
    if len(msgs) <= 1:
        try:
            ctx = _fetch_context(client, channel, thread_ts)
            if len(ctx) > len(msgs):
                return _format(channel, ctx)
        except SlackApiError:
            pass  # fall back to whatever replies gave us
    return _format(channel, msgs)


def _format(channel: str, msgs: list[dict]) -> str:
    if not msgs:
        return "(empty thread)"
    lines = [f"SLACK CONVERSATION ({channel}) — {len(msgs)} message(s), oldest first:"]
    for m in msgs:
        who = m.get("user") or m.get("username") or m.get("bot_id") or "someone"
        text = (m.get("text") or "").strip()
        lines.append(f"\n[{who}]\n{text}")
    return "\n".join(lines).strip()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: slack_read <permalink> | slack_read <channel_id> <thread_ts>",
              file=sys.stderr)
        return 2
    if len(argv) == 1:
        parsed = _parse_permalink(argv[0])
        if not parsed:
            print(f"(could not parse a channel + ts from: {argv[0]})", file=sys.stderr)
            return 2
        channel, thread_ts = parsed
    else:
        channel, thread_ts = argv[0], argv[1]
    print(fetch_thread(channel, thread_ts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
