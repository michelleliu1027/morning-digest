"""Slack interactive layer: ✅/⏭️ buttons that spawn local agents.

A resident process (Socket Mode, no public URL needed) that:
  1. posts a task list to your Slack DM, each task with [✅ Do this] [⏭️ Skip],
  2. on ✅ → spawns a local `claude` agent (via the monitor server) and replies
     in-thread with a link to the live dashboard,
  3. when the agent finishes → posts the outcome back to the same thread.

Run it:
    python -m morning_digest.monitor.interactive

Requires (in .env):
    SLACK_MORNING_TASKS_DIGEST_BOT_TOKEN   (xoxb-, chat:write)
    SLACK_MORNING_TASKS_DIGEST_APP_TOKEN   (xapp-, connections:write + Socket Mode on)
    MY_SLACK_USER_ID
"""

from __future__ import annotations

import os
import threading
import time

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .server import PORT, REGISTRY, serve, spawn_agent
from .tasks import Task, split_digest_and_tasks

load_dotenv()

BOT_TOKEN_ENV = "SLACK_MORNING_TASKS_DIGEST_BOT_TOKEN"
APP_TOKEN_ENV = "SLACK_MORNING_TASKS_DIGEST_APP_TOKEN"

DASHBOARD_URL = f"http://127.0.0.1:{PORT}"

# Demo task set. Later these are generated from the digest; the shape is what matters.
DEMO_TASKS = [
    Task(
        id="cac-schema",
        title="Add schema test to dagster-etl#1098",
        prompt="Add a campaign_cac_daily.yml schema file with a grain uniqueness "
               "test to satisfy Bugbot. This extends PR #1098 — add it to that PR's "
               "branch as a new commit rather than opening a separate PR.",
        cwd="~/Documents/GitHub/dagster-etl",
        gate="code",
    ),
    Task(
        id="review-1095",
        title="Review dagster-etl#1095 (PL funnel)",
        prompt="Read the diff of dagster-etl PR #1095 via gh, then draft review "
               "comments. Output the draft only — do not post it.",
        cwd="~/Documents/GitHub/dagster-etl",
        gate="draft",
    ),
    Task(
        id="datalocker",
        title="Investigate Data Locker status",
        prompt="Summarize the current state of the Data Locker / AppsFlyer ingestion "
               "work by reading the relevant code and Notion notes. Read-only.",
        cwd="~/Documents/GitHub/data-monorepo",
        gate="readonly",
    ),
]

_TASKS: dict[str, Task] = {t.id: t for t in DEMO_TASKS}


def _task_blocks(tasks: list[Task]) -> list[dict]:
    """Build Slack Block Kit blocks: a header + one section+buttons per task."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "🌅 Today's tasks — tap ✅ to start an agent"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"👀 Watch agents live: {DASHBOARD_URL}"}]},
        {"type": "divider"},
    ]
    for i, t in enumerate(tasks, 1):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{i}️⃣ {t.title}*\n_{t.gate_label}_"},
        })
        blocks.append({
            "type": "actions",
            "block_id": f"task_{t.id}",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Do this"},
                 "style": "primary", "action_id": "do_task", "value": t.id},
                {"type": "button", "text": {"type": "plain_text", "text": "⏭️ Skip"},
                 "action_id": "skip_task", "value": t.id},
            ],
        })
    return blocks


def build_app() -> App:
    app = App(token=os.environ[BOT_TOKEN_ENV])

    @app.action("skip_task")
    def handle_skip(ack, body, client):
        ack()
        tid = body["actions"][0]["value"]
        task = _TASKS.get(tid)
        client.chat_postMessage(
            channel=body["channel"]["id"],
            thread_ts=body["message"]["ts"],
            text=f"⏭️ Skipped: {task.title if task else tid}",
        )

    @app.action("do_task")
    def handle_do(ack, body, client):
        ack()
        tid = body["actions"][0]["value"]
        task = _TASKS.get(tid)
        channel = body["channel"]["id"]
        thread_ts = body["message"]["ts"]
        if not task:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=f"⚠️ Task not found: {tid}")
            return

        agent = spawn_agent(name=task.title, prompt=task.agent_prompt,
                            cwd=task.cwd, gate=task.gate)
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"🚀 Started: *{task.title}*\nWatch it work → {DASHBOARD_URL}\n_({task.gate_label})_",
        )
        # Watch the agent and post the outcome back to this thread when it finishes.
        threading.Thread(
            target=_report_when_done,
            args=(client, channel, thread_ts, agent.id, task),
            daemon=True,
        ).start()

    return app


def _report_when_done(client, channel: str, thread_ts: str, agent_id: str, task: Task) -> None:
    # Terminal states differ by gate: a code task first parks in awaiting_approval
    # (the diff is ready for review) before it can reach done. It may instead land
    # in needs_input — it didn't actually finish and wants a human correction.
    announced_diff = False
    announced_input = False
    while True:
        time.sleep(2)
        agent = REGISTRY.get(agent_id)
        if agent is None:
            return
        if agent.status == "running":  # a re-run restarted it — re-arm the pings
            announced_diff = announced_input = False
        if agent.status == "awaiting_approval" and not announced_diff:
            announced_diff = True
            target = (agent.diff or {}).get("target", "")
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f"✋ *{task.title}* — change is ready. Review the diff in the "
                     f"dashboard before committing:\n{DASHBOARD_URL}\n_Target: {target}_",
            )
            continue  # keep watching: she'll approve/reject in the dashboard
        if agent.status == "needs_input" and not announced_input:
            announced_input = True
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f"✋ *{task.title}* — this did NOT finish cleanly: {agent.note}\n"
                     f"Open the dashboard to read its reasoning and re-run it with a "
                     f"correcting hint:\n{DASHBOARD_URL}",
            )
            continue  # she may re-run with a hint; keep watching for the real outcome
        if agent.status in ("done", "failed"):
            break

    final = agent.latest() or {}
    summary = final.get("text", "")
    ok = agent.status == "done"
    head = "✅ Done" if ok else "❌ Failed"
    note = {
        "code": "→ Change committed per your approval (not merged).",
        "draft": "→ Draft is in the dashboard; send it once you've okayed it.",
        "readonly": "→ Read-only analysis; results in the dashboard.",
    }.get(task.gate, "")
    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f"{head}: *{task.title}* ({agent.public()['elapsed']}s)\n{summary}\n{note}",
    )


def post_tasks(app: App, tasks: list[Task] | None = None) -> None:
    """Push the task list with buttons to the user's DM."""
    tasks = tasks or DEMO_TASKS
    for t in tasks:
        _TASKS[t.id] = t
    app.client.chat_postMessage(
        channel=os.environ["MY_SLACK_USER_ID"],
        blocks=_task_blocks(tasks),
        text="Today's tasks (tap a button to run)",
    )


def post_live_digest(app: App, mode: str | None = None) -> None:
    """Build the real digest (with actionable tasks), post it, then the buttons.

    This is the bridge from v1 (text digest) to v2 (tappable tasks): one Claude
    run produces both the human digest and the machine-readable task list.
    """
    # Imported lazily so the demo path doesn't require the full digest deps.
    from datetime import date

    from ..prompt import build_prompt
    from ..slack_format import to_slack_blocks, to_slack_mrkdwn
    from ..window import compute_window
    from .. import __main__ as runner

    today = date.today()
    win = compute_window(today, force_mode=mode)
    user_id = os.environ["MY_SLACK_USER_ID"]
    from ..slack_source import fetch_messages
    slack_messages = fetch_messages(
        user_id, after=win.after, before=win.before,
        include_sent=(win.mode == "weekly"),
    )
    prompt = build_prompt(
        mode=win.mode, today=today.isoformat(), window=win.label,
        slack_messages=slack_messages, with_actions=True,
    )
    raw = runner.run_claude(prompt)
    digest_text, tasks = split_digest_and_tasks(raw)

    app.client.chat_postMessage(
        channel=user_id, blocks=to_slack_blocks(digest_text),
        text=to_slack_mrkdwn(digest_text), unfurl_links=False,
    )
    if tasks:
        post_tasks(app, tasks)
    else:
        print("[interactive] digest had no actionable tasks; no buttons posted.")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Interactive digest + agent buttons.")
    parser.add_argument("--demo", action="store_true",
                        help="Post the hardcoded demo tasks instead of a live digest.")
    parser.add_argument("--mode", choices=["daily", "weekly"], default=None,
                        help="Force digest mode (default: auto by weekday).")
    args = parser.parse_args()

    for env in (BOT_TOKEN_ENV, APP_TOKEN_ENV, "MY_SLACK_USER_ID"):
        if not os.environ.get(env):
            raise SystemExit(f"Missing {env} in .env")

    # Run the dashboard server in-process so buttons + monitor share one registry.
    threading.Thread(target=serve, kwargs={"port": PORT}, daemon=True).start()
    time.sleep(0.5)

    app = build_app()
    if args.demo:
        post_tasks(app)
    else:
        post_live_digest(app, mode=args.mode)
    print(f"[interactive] posted to Slack. Dashboard: {DASHBOARD_URL}")
    print("[interactive] listening for button clicks (Ctrl-C to stop)…")
    SocketModeHandler(app, os.environ[APP_TOKEN_ENV]).start()


if __name__ == "__main__":
    main()
