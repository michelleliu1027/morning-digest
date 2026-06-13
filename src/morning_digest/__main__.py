"""Morning digest: have Claude gather Notion + GitHub, then DM the summary to Slack.

Run: python -m morning_digest          (gather + send to Slack)
     python -m morning_digest --dry-run (gather + print to terminal, no Slack)
"""

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient

from .prompt import DIGEST_PROMPT

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]


def build_digest() -> str:
    """Invoke the claude CLI headlessly to gather sources and produce the digest."""
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    model = os.environ.get("CLAUDE_MODEL", "opus")

    cmd = [
        claude_bin,
        "-p",
        DIGEST_PROMPT,
        "--model",
        model,
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Bash(gh *) mcp__notion__notion-search mcp__notion__notion-fetch",
    ]

    # Run from the user's home so the Notion MCP (a user-scoped HTTP server) and
    # gh CLI auth resolve exactly as they do in an interactive session.
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


def send_to_slack(text: str) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    user_id = os.environ.get("MY_SLACK_USER_ID")
    if not token or not user_id:
        raise SystemExit(
            "Missing SLACK_BOT_TOKEN or MY_SLACK_USER_ID. "
            "Copy .env.example to .env and fill them in."
        )
    client = WebClient(token=token)
    # Posting to a user ID opens (or reuses) the DM with that user.
    client.chat_postMessage(channel=user_id, text=text, unfurl_links=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and send the morning digest.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest to the terminal instead of sending to Slack.",
    )
    args = parser.parse_args()

    print(f"[morning-digest] building digest for {date.today()}...", file=sys.stderr)
    digest = build_digest()

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
