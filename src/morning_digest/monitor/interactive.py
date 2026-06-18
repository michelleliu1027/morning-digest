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

import json
import os
import threading
import time
from dataclasses import asdict, fields
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import ledger
from .server import PORT, REGISTRY, serve, spawn_agent
from .tasks import Task, split_digest_and_tasks

load_dotenv()

BOT_TOKEN_ENV = "SLACK_MORNING_TASKS_DIGEST_BOT_TOKEN"
APP_TOKEN_ENV = "SLACK_MORNING_TASKS_DIGEST_APP_TOKEN"

DASHBOARD_URL = f"http://127.0.0.1:{PORT}"

# Hour (local) at/after which the daily digest is posted once. The listener is a
# persistent process, so a marker file records the last date it posted — this
# survives restarts (login, crash) so you get exactly one digest per day.
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "8"))
_STATE_DIR = Path(__file__).resolve().parents[3] / "digest_output"
_LAST_SENT_FILE = _STATE_DIR / ".last_digest_date"
# Posted tasks persisted to disk, keyed by task id. The in-memory _TASKS dict is
# lost when the listener restarts (KeepAlive/login/crash) or when a one-off
# process posts the digest — but the buttons in Slack live forever, so a click
# can arrive at a process whose memory never held that task. This file lets any
# process resolve a clicked task id back into a Task, so buttons never go dead.
_TASKS_FILE = _STATE_DIR / ".posted_tasks.json"
_TASKS_FILE_LOCK = threading.Lock()

# Demo task set used by `--demo`. These are placeholders to show the shape of a
# task; in real use tasks are generated from your digest. Edit them to point at
# your own repos/PRs, or just run a live digest instead.
DEMO_TASKS = [
    Task(
        id="demo-code",
        title="Add a schema test to PR #123",
        prompt="Add a schema/uniqueness test to satisfy the failing check. This "
               "extends PR #123 — add it to that PR's branch as a new commit rather "
               "than opening a separate PR.",
        cwd="~/your-repo",
        gate="code",
    ),
    Task(
        id="demo-draft",
        title="Review PR #124",
        prompt="Read the diff of PR #124 via gh, then draft review comments. "
               "Output the draft only — do not post it.",
        cwd="~/your-repo",
        gate="draft",
    ),
    Task(
        id="demo-readonly",
        title="Investigate the status of feature X",
        prompt="Summarize the current state of feature X by reading the relevant "
               "code and notes. Read-only.",
        cwd="~/your-repo",
        gate="readonly",
    ),
]

_TASKS: dict[str, Task] = {t.id: t for t in DEMO_TASKS}

# The posted task message, so we can chat_update it in place as tasks progress.
# Keyed by the message ts: {"channel": str, "blocks": list[dict]}.
_MSG: dict[str, dict] = {}
_MSG_LOCK = threading.Lock()

# Only the Task dataclass fields are persisted (all JSON-safe strings); anything
# extra in the file is ignored so an old file never breaks a newer Task shape.
_TASK_FIELDS = {f.name for f in fields(Task)}


def _persist_tasks(tasks: list[Task]) -> None:
    """Merge the given tasks into the on-disk store so any process can resolve them."""
    try:
        with _TASKS_FILE_LOCK:
            store: dict[str, dict] = {}
            try:
                store = json.loads(_TASKS_FILE.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                store = {}
            for t in tasks:
                store[t.id] = asdict(t)
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            _TASKS_FILE.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        print(f"[interactive] _persist_tasks failed: {exc}")


def _get_task(tid: str) -> Task | None:
    """Resolve a clicked task id: memory first, then the on-disk store.

    The disk fallback is what keeps Slack buttons alive across restarts and
    across the process that posted the digest exiting.
    """
    t = _TASKS.get(tid)
    if t is not None:
        return t
    try:
        store = json.loads(_TASKS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    spec = store.get(tid)
    if not isinstance(spec, dict):
        return None
    t = Task(**{k: v for k, v in spec.items() if k in _TASK_FIELDS})
    _TASKS[tid] = t  # warm the cache so the next click is in-memory
    return t


def _record_outcome(tid: str, state: str) -> None:
    """Persist a task's terminal outcome to the completion ledger (best-effort)."""
    t = _get_task(tid)
    if not t:
        return
    try:
        ledger.record(state=state, title=t.title, source_url=t.source_url, gate=t.gate)
    except Exception as exc:  # noqa: BLE001 - ledger is best-effort, never crash a click
        print(f"[interactive] ledger.record failed: {exc}")


def _task_blocks(tasks: list[Task]) -> list[dict]:
    """Build Slack Block Kit blocks: a header + one section+buttons per task."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "🌅 Today's tasks — tap ✅ to start an agent"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"👀 Watch agents live: {DASHBOARD_URL}"}]},
        {"type": "divider"},
    ]
    for i, t in enumerate(tasks, 1):
        body = f"*{i}️⃣ {t.title}*\n_{t.gate_label}_"
        if t.source_label:
            body += f"\n{t.source_label}"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        })
        blocks.append({
            "type": "actions",
            "block_id": f"task_{t.id}",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Do this"},
                 "style": "primary", "action_id": "do_task", "value": t.id},
                {"type": "button", "text": {"type": "plain_text", "text": "✔️ Already did it"},
                 "action_id": "finished_task", "value": t.id},
                {"type": "button", "text": {"type": "plain_text", "text": "⏭️ Skip"},
                 "action_id": "skip_task", "value": t.id},
            ],
        })
    return blocks


def _ensure_msg(body: dict) -> None:
    """Warm _MSG from a click payload when this process didn't post the message.

    Status edits rewrite blocks held in the in-memory _MSG dict, keyed by message
    ts. That dict is empty when the listener restarts or when a one-off process
    posted the digest — so a click would find no record and the row would never
    flip to "Running…". The action payload carries the message's current blocks,
    ts and channel, so we seed _MSG from it (only if missing — never clobber the
    live record that already tracks in-place edits).
    """
    msg = body.get("message", {}) or {}
    ts = msg.get("ts")
    blocks = msg.get("blocks")
    channel = (body.get("channel", {}) or {}).get("id")
    if not (ts and blocks and channel):
        return
    with _MSG_LOCK:
        _MSG.setdefault(ts, {"channel": channel, "blocks": blocks})


def _set_task_status(client, message_ts: str, task_id: str, status_md: str) -> None:
    """Rewrite one task row's buttons into a status line, in place via chat_update.

    The buttons live in an `actions` block with block_id `task_<id>`; we swap that
    block for a `context` block (e.g. "⏳ Running… · watch live") so a tapped task
    no longer shows Do this / Skip.
    """
    with _MSG_LOCK:
        rec = _MSG.get(message_ts)
        if not rec:
            return
        new_blocks = []
        for b in rec["blocks"]:
            if b.get("type") == "actions" and b.get("block_id") == f"task_{task_id}":
                new_blocks.append({
                    "type": "context", "block_id": f"task_{task_id}",
                    "elements": [{"type": "mrkdwn", "text": status_md}],
                })
            else:
                new_blocks.append(b)
        rec["blocks"] = new_blocks
        channel, blocks = rec["channel"], list(new_blocks)
    try:
        client.chat_update(channel=channel, ts=message_ts, blocks=blocks,
                           text="Today's tasks")
    except Exception as exc:  # noqa: BLE001 - status is cosmetic; never crash a click
        print(f"[interactive] chat_update failed: {exc}")


def build_app() -> App:
    app = App(token=os.environ[BOT_TOKEN_ENV])

    @app.action("skip_task")
    def handle_skip(ack, body, client):
        ack()
        _ensure_msg(body)
        tid = body["actions"][0]["value"]
        _record_outcome(tid, "skipped")
        _set_task_status(client, body["message"]["ts"], tid, "⏭️ *Skipped*")

    @app.action("finished_task")
    def handle_finished(ack, body, client):
        # The user did this themselves before any agent ran. Same suppression
        # window as a real 'done' — the ledger stops it resurfacing for a week.
        ack()
        _ensure_msg(body)
        tid = body["actions"][0]["value"]
        _record_outcome(tid, "finished")
        _set_task_status(client, body["message"]["ts"], tid, "✔️ *Already done by you*")

    @app.action("do_task")
    def handle_do(ack, body, client):
        ack()
        _ensure_msg(body)
        tid = body["actions"][0]["value"]
        task = _get_task(tid)
        channel = body["channel"]["id"]
        thread_ts = body["message"]["ts"]
        if not task:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=f"⚠️ Task not found: {tid}")
            return

        agent = spawn_agent(name=task.title, prompt=task.agent_prompt,
                            cwd=task.cwd, gate=task.gate,
                            source=task.source, source_url=task.source_url)
        _set_task_status(client, thread_ts, tid,
                         f"⏳ *Running…* · <{DASHBOARD_URL}|watch live>")
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"🚀 Started: *{task.title}*\nWatch it work → {DASHBOARD_URL}\n_({task.gate_label})_",
        )
        # Watch the agent and post the outcome back to this thread when it finishes.
        threading.Thread(
            target=_report_when_done,
            args=(client, channel, thread_ts, agent.id, task, tid),
            daemon=True,
        ).start()

    return app


def _report_when_done(client, channel: str, thread_ts: str, agent_id: str,
                      task: Task, tid: str | None = None) -> None:
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
            if task.gate == "draft":
                n = len(agent.drafts or [])
                if tid:
                    _set_task_status(client, thread_ts, tid,
                                     f"✋ *{n} draft comment(s)* — <{DASHBOARD_URL}|approve each>")
                client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f"✋ *{task.title}* — {n} draft comment(s) ready. Nothing is "
                         f"posted yet; approve each one in the dashboard to post it to "
                         f"the PR:\n{DASHBOARD_URL}",
                )
            else:
                target = (agent.diff or {}).get("target", "")
                if tid:
                    _set_task_status(client, thread_ts, tid,
                                     f"✋ *Diff ready* — <{DASHBOARD_URL}|review & commit>")
                client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f"✋ *{task.title}* — change is ready. Review the diff in the "
                         f"dashboard before committing:\n{DASHBOARD_URL}\n_Target: {target}_",
                )
            continue  # keep watching: she'll approve/reject in the dashboard
        if agent.status == "needs_input" and not announced_input:
            announced_input = True
            if tid:
                _set_task_status(client, thread_ts, tid,
                                 f"✋ *Needs input* — <{DASHBOARD_URL}|review & re-run>")
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f"✋ *{task.title}* — this did NOT finish cleanly: {agent.note}\n"
                     f"Open the dashboard to read its reasoning and re-run it with a "
                     f"correcting hint:\n{DASHBOARD_URL}",
            )
            continue  # she may re-run with a hint; keep watching for the real outcome
        if agent.status in ("done", "failed"):
            break

    if tid:
        elapsed = agent.public()["elapsed"]
        if agent.status == "done":
            _set_task_status(client, thread_ts, tid, f"✅ *Done* · {elapsed}s")
            # Record success so the next digest won't resurface it for a week.
            _record_outcome(tid, "done")
        else:
            _set_task_status(client, thread_ts, tid, f"❌ *Failed* · {elapsed}s")

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
    # Persist so a click can be resolved even if THIS process is gone by then
    # (a one-off post, or the listener restarting before you tap a button).
    _persist_tasks(tasks)
    blocks = _task_blocks(tasks)
    resp = app.client.chat_postMessage(
        channel=os.environ["MY_SLACK_USER_ID"],
        blocks=blocks,
        text="Today's tasks (tap a button to run)",
    )
    # Remember the message so each click can chat_update its task row in place.
    with _MSG_LOCK:
        _MSG[resp["ts"]] = {"channel": resp["channel"], "blocks": blocks}


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
    # include_sent=True even for the daily digest: the user's own replies
    # (tagged [ME]) are the evidence the model needs to tell an already-answered
    # @mention/DM from one that still owes a reply. Without them it can only see
    # the incoming ask and re-surfaces things the user already handled.
    slack_messages = fetch_messages(
        user_id, after=win.after, before=win.before,
        include_sent=True,
    )
    prompt = build_prompt(
        mode=win.mode, today=today.isoformat(), window=win.label,
        slack_messages=slack_messages, with_actions=True,
        already_handled=ledger.format_for_prompt(),
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


def _already_sent_today() -> bool:
    try:
        return _LAST_SENT_FILE.read_text().strip() == date.today().isoformat()
    except FileNotFoundError:
        return False


def _mark_sent_today() -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _LAST_SENT_FILE.write_text(date.today().isoformat())


def _daily_scheduler(app: App, mode: str | None) -> None:
    """Post the digest once per day at/after DIGEST_HOUR, guarded by a marker file.

    Runs forever in a background thread. Because the listener is persistent, this
    replaces v1's one-shot launchd run: if the Mac was asleep at 8am, the digest
    goes out when this catches up — and never twice in one day.
    """
    while True:
        now = datetime.now()
        if now.hour >= DIGEST_HOUR and not _already_sent_today():
            try:
                post_live_digest(app, mode=mode)
                _mark_sent_today()
                print(f"[interactive] daily digest posted at {now:%H:%M}.")
            except Exception as exc:  # noqa: BLE001 - keep the listener alive
                print(f"[interactive] digest failed: {exc}")
        time.sleep(300)  # check every 5 minutes


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Interactive digest + agent buttons.")
    parser.add_argument("--demo", action="store_true",
                        help="Post the hardcoded demo tasks instead of a live digest.")
    parser.add_argument("--mode", choices=["daily", "weekly"], default=None,
                        help="Force digest mode (default: auto by weekday).")
    parser.add_argument("--now", action="store_true",
                        help="Post a digest immediately on start (for testing), "
                             "in addition to the daily schedule.")
    parser.add_argument("--no-schedule", action="store_true",
                        help="Skip the daily scheduler; just listen for clicks "
                             "(use with --now or --demo for a one-off test).")
    args = parser.parse_args()

    for env in (BOT_TOKEN_ENV, APP_TOKEN_ENV, "MY_SLACK_USER_ID"):
        if not os.environ.get(env):
            raise SystemExit(f"Missing {env} in .env")

    # Run the dashboard server in-process so buttons + monitor share one registry.
    threading.Thread(target=serve, kwargs={"port": PORT}, daemon=True).start()
    time.sleep(0.5)

    app = build_app()

    # Optional immediate post (testing or a manual kick).
    if args.demo:
        post_tasks(app)
    elif args.now:
        post_live_digest(app, mode=args.mode)
        _mark_sent_today()

    # The persistent daily scheduler is what replaces v1's launchd one-shot.
    if not args.no_schedule:
        threading.Thread(target=_daily_scheduler, args=(app, args.mode),
                         daemon=True).start()
        print(f"[interactive] daily scheduler armed (posts at/after {DIGEST_HOUR}:00).")

    print(f"[interactive] dashboard: {DASHBOARD_URL}")
    print("[interactive] listening for button clicks (Ctrl-C to stop)…")
    SocketModeHandler(app, os.environ[APP_TOKEN_ENV]).start()


if __name__ == "__main__":
    main()
