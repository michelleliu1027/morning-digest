"""Morning digest: have Claude gather Notion + GitHub + Slack, then DM the summary.

Run: python -m morning_digest             (auto mode by weekday; send to Slack)
     python -m morning_digest --dry-run    (gather + print to terminal, no Slack)
     python -m morning_digest --mode weekly (force the Monday weekly review)
"""

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient

from .prompt import build_prompt
from .slack_format import to_slack_blocks, to_slack_mrkdwn
from .slack_source import fetch_messages
from .window import compute_window

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_claude(prompt: str) -> str:
    """Invoke the claude CLI headlessly with the digest toolset; return stdout.

    Runs from the user's home so the Notion MCP (a user-scoped HTTP server) and
    gh CLI auth resolve exactly as they do in an interactive session.
    """
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    model = os.environ.get("CLAUDE_MODEL", "opus")
    cmd = [
        claude_bin,
        "-p",
        prompt,
        "--model",
        model,
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Bash(gh *) mcp__notion__notion-search mcp__notion__notion-fetch",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(Path.home()),
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude exited {result.returncode}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def build_digest(mode: str | None = None) -> str:
    """Gather sources and produce the digest text (no actionable-task block)."""
    today = date.today()
    win = compute_window(today, force_mode=mode)

    user_id = os.environ.get("MY_SLACK_USER_ID", "")
    slack_messages = (
        fetch_messages(
            user_id,
            after=win.after,
            before=win.before,
            include_sent=(win.mode == "weekly"),
        )
        if user_id
        else ""
    )

    prompt = build_prompt(
        mode=win.mode,
        today=today.isoformat(),
        window=win.label,
        slack_messages=slack_messages,
    )
    return run_claude(prompt)


def send_to_slack(text: str) -> None:
    token = os.environ.get("SLACK_MORNING_TASKS_DIGEST_BOT_TOKEN")
    user_id = os.environ.get("MY_SLACK_USER_ID")
    if not token or not user_id:
        raise SystemExit(
            "Missing SLACK_MORNING_TASKS_DIGEST_BOT_TOKEN or MY_SLACK_USER_ID. "
            "Copy .env.example to .env and fill them in."
        )
    client = WebClient(token=token)
    # Posting to a user ID opens (or reuses) the DM with that user.
    # Blocks give us a real header, dividers, and bullet glyphs; `text` is the
    # notification/preview fallback for clients that can't render blocks.
    client.chat_postMessage(
        channel=user_id,
        blocks=to_slack_blocks(text),
        text=to_slack_mrkdwn(text),
        unfurl_links=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and send the morning digest.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest to the terminal instead of sending to Slack.",
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly"],
        default=None,
        help="Force a mode. Default: auto (weekly on Monday, daily otherwise).",
    )
    args = parser.parse_args()

    print(f"[morning-digest] building digest for {date.today()}...", file=sys.stderr)
    digest = build_digest(mode=args.mode)

    if not digest:
        print("[morning-digest] claude returned nothing; aborting.", file=sys.stderr)
        raise SystemExit(1)

    if args.dry_run:
        print("\n" + "=" * 60 + "\n" + digest + "\n" + "=" * 60)
        return

    send_to_slack(digest)
    print("[morning-digest] sent to Slack DM.", file=sys.stderr)


if __name__ == "__main__":
    main()
