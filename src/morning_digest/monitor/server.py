"""Resident local monitor: spawn `claude` agents and watch them live in a browser.

Run it:
    python -m morning_digest.monitor          # starts server on http://127.0.0.1:8787

Then either:
    - open the dashboard in a browser to watch agents work, or
    - POST a task to spawn an agent:
        curl -XPOST 127.0.0.1:8787/spawn \\
             -d '{"name":"demo","prompt":"list python files","cwd":"."}'

State lives in memory in this one process; the dashboard streams updates via SSE.
Pure stdlib — no extra dependencies.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .events import ProgressEvent, parse_event
from .gitops import (
    capture_diff, commit_and_push, commit_files, discard, discard_files,
    fetch_pr_review_comments, post_pr_comment, split_diff_by_file,
)

HOST = "127.0.0.1"
PORT = 8787

# Read-only Slack thread reader (morning_digest.slack_read), invoked by the
# listener's own interpreter so it resolves regardless of the agent's cwd. This
# is the ONLY Slack surface an agent may touch: it can READ the thread a task
# came from (to pull context the digest dropped, e.g. IDs a teammate pasted) but
# holds no token and cannot post. The command is allowlisted verbatim below.
SLACK_READ_CMD = f"{sys.executable} -m morning_digest.slack_read"
_SLACK_READ_TOOL = f"Bash({SLACK_READ_CMD}*)"

# Read-only `gh` subcommands an analysis/draft agent may use. Deliberately a
# per-subcommand allowlist (NOT the `gh *` wildcard) so the agent CANNOT post a
# comment/review or merge — `gh pr comment`, `gh pr review`, `gh pr merge` and
# the raw `gh api` escape hatch are simply not granted. The draft is data the
# user approves; only the server posts it, after a click.
_READONLY_GH = (
    "Bash(gh pr view*) Bash(gh pr diff*) Bash(gh pr list*) Bash(gh pr checks*) "
    "Bash(gh pr checkout*) Bash(gh search*) Bash(gh issue view*) Bash(gh issue list*) "
    "Bash(gh repo view*)"
)

# Tools a read-only / analysis agent may use without prompting.
DEFAULT_ALLOWED_TOOLS = (
    f"TodoWrite Read Grep {_READONLY_GH} {_SLACK_READ_TOOL} "
    "Bash(ls *) Bash(find *) Bash(cat *) Bash(grep *) Bash(git diff*) Bash(git log*) "
    "mcp__notion__notion-search mcp__notion__notion-fetch"
)

# Draft agents (gate="draft") get the same read-only surface: they read a PR and
# WRITE A DRAFT, but never post it. Same allowlist — the write happens only via
# the server's post_pr_comment after the user approves each comment.
DRAFT_ALLOWED_TOOLS = DEFAULT_ALLOWED_TOOLS

# Tools the code WRITER may use. Deliberately EXCLUDES `git commit` / `git push`:
# the agent edits files and picks the right branch, but committing only happens
# after the user approves the diff in the dashboard. The gate is enforced here at
# the tool level, not merely by the prompt.
WRITER_ALLOWED_TOOLS = (
    f"TodoWrite Edit Write Read Grep {_SLACK_READ_TOOL} "
    "Bash(git status*) Bash(git diff*) Bash(git log*) Bash(git branch*) "
    "Bash(git checkout*) Bash(git switch*) Bash(git fetch*) Bash(git stash*) "
    "Bash(gh pr list*) Bash(gh pr view*) Bash(gh pr checkout*) "
    "Bash(ls *) Bash(find *) Bash(cat *) Bash(grep *)"
)


@dataclass
class Agent:
    id: str
    name: str
    prompt: str
    cwd: str
    # starting | running | done | failed | awaiting_approval | committing | needs_input
    status: str = "starting"
    gate: str = "readonly"            # readonly | draft | code
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    events: list[dict] = field(default_factory=list)  # serialized ProgressEvents
    proc: subprocess.Popen | None = None
    diff: dict | None = None          # DiffSnapshot (branch/diff/files/existing_pr) when awaiting approval
    session_id: str | None = None     # claude session id, for --resume on a re-run
    note: str = ""                    # why the agent needs human input (shown in dashboard)
    model: str = "opus"               # model to reuse on a --resume re-run
    allowed_tools: str = ""           # toolset to reuse on a --resume re-run
    drafts: list[dict] | None = None  # draft PR comments awaiting per-item approval
    pr_feedback: str = ""             # all PR review comments fetched server-side
    source: str = ""                  # task provenance from the digest ("CodeRabbit on #123")
    source_url: str = ""              # link to that source, if any

    def latest(self) -> dict | None:
        return self.events[-1] if self.events else None

    def final_output(self) -> str:
        """The agent's last substantial 'say' — the draft / analysis to read in full."""
        for ev in reversed(self.events):
            if ev.get("kind") == "say" and ev.get("full"):
                return ev["full"]
        return ""

    def todos(self) -> list[dict]:
        """The agent's most recent TodoWrite plan (Manus-style live checklist)."""
        for ev in reversed(self.events):
            if ev.get("kind") == "todo":
                return ev.get("meta", {}).get("todos", [])
        return []

    def public(self) -> dict:
        """JSON-safe snapshot for the dashboard (omits the live Popen handle)."""
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "cwd": self.cwd,
            "status": self.status,
            "gate": self.gate,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed": round((self.ended_at or time.time()) - self.started_at, 1),
            "events": self.events,
            "latest": self.latest(),
            "final_output": self.final_output(),
            "todos": self.todos(),
            "diff": self.diff,
            "note": self.note,
            "drafts": self.drafts,
            "source": self.source,
            "source_url": self.source_url,
            # The dashboard chat box only makes sense once the agent has stopped
            # AND has a session to resume; surface that as one flag for the UI.
            "can_chat": bool(self.session_id) and self.status in _CHATTABLE_STATES,
        }


class Registry:
    """Thread-safe store of all agents + a simple pub/sub for SSE subscribers."""

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._lock = threading.Lock()
        self._subscribers: list["Queue"] = []

    def add(self, agent: Agent) -> None:
        with self._lock:
            self._agents[agent.id] = agent
        self._broadcast()

    def get(self, agent_id: str) -> Agent | None:
        with self._lock:
            return self._agents.get(agent_id)

    def snapshot(self) -> list[dict]:
        with self._lock:
            agents = list(self._agents.values())
        agents.sort(key=lambda a: a.started_at, reverse=True)
        return [a.public() for a in agents]

    def push_event(self, agent: Agent, ev: ProgressEvent) -> None:
        rec = {
            "kind": ev.kind, "text": ev.text, "icon": ev.icon,
            "detail": ev.detail, "full": ev.full, "meta": ev.meta,
            "t": round(time.time() - agent.started_at, 1),
        }
        with self._lock:
            agent.events.append(rec)
        self._broadcast()

    def set_status(self, agent: Agent, status: str) -> None:
        with self._lock:
            agent.status = status
            if status in ("done", "failed", "needs_input"):
                agent.ended_at = time.time()
        self._broadcast()

    def set_diff(self, agent: Agent, diff: dict | None) -> None:
        with self._lock:
            agent.diff = diff
        self._broadcast()

    def set_note(self, agent: Agent, note: str) -> None:
        with self._lock:
            agent.note = note
        self._broadcast()

    def set_drafts(self, agent: Agent, drafts: list[dict] | None) -> None:
        with self._lock:
            agent.drafts = drafts
        self._broadcast()

    # --- SSE pub/sub ---
    def subscribe(self) -> "Queue":
        from queue import Queue
        q: Queue = Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "Queue") -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self) -> None:
        payload = self.snapshot()
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            q.put(payload)


REGISTRY = Registry()


_PR_NUM_RE = re.compile(r"#(\d{1,6})\b")


def _fetch_pr_feedback(prompt: str, cwd: str) -> str:
    """The full review feedback for the first PR named in the task, or ''.

    Resolves the first `#<n>` in the task and fetches every inline review comment
    + review summary via gitops (read-only gh). Returns '' if no PR named or none
    found — never raises (a gh hiccup must not block a spawn).

    Searches only the BARE task text, not the prepended preambles — CODE_PREAMBLE
    contains a literal "#123" example that would otherwise match for every task.
    """
    m = _PR_NUM_RE.search(_bare_task(prompt))
    if not m:
        return ""
    try:
        return fetch_pr_review_comments(cwd, int(m.group(1)))
    except Exception:  # noqa: BLE001
        return ""


def _with_pr_feedback(prompt: str, feedback: str) -> str:
    """Prepend fetched PR feedback to the prompt so the agent works the whole PR."""
    if not feedback:
        return prompt
    return (
        "--- ALL OPEN REVIEW FEEDBACK ON THIS PR (fetched for you) ---\n"
        "These are every reviewer/bot comment currently on the PR. Address EACH "
        "distinct issue, or state explicitly which you are skipping and why. Do "
        "not stop after the first one. Quote the relevant ones verbatim into your "
        f"<<<SOURCE>>> block.\n\n{feedback}\n"
        "--- END REVIEW FEEDBACK ---\n\n"
    ) + prompt


def _with_slack_source(prompt: str, source_url: str) -> str:
    """If the task came from a Slack thread, tell the agent to read it first.

    The digest distils a thread into a one-line task and drops the rest, so the
    evidence the agent needs (IDs/links a teammate pasted, the precise ask) is
    only in the thread. The agent can't reach Slack except through the read-only
    `slack_read` tool, so point it at the exact permalink and require it to pull
    the thread before acting.
    """
    if "/archives/" not in (source_url or ""):
        return prompt  # not a Slack permalink (PR/Notion/none) — nothing to add
    return (
        "=== SLACK SOURCE (read it FIRST for full context) ===\n"
        "This task was distilled from a Slack thread; the one-line task above may "
        "be missing details that live in the thread (IDs, links, the exact ask). "
        "Before you do anything else, run this read-only command and use what it "
        "returns as primary context:\n"
        f"  {SLACK_READ_CMD} '{source_url}'\n"
        "If it returns an error or nothing useful, say so briefly and proceed with "
        "what you have.\n"
        "=== END SLACK SOURCE ===\n\n"
    ) + prompt


def spawn_agent(name: str, prompt: str, cwd: str = ".", model: str = "opus",
                gate: str = "readonly", allowed_tools: str | None = None,
                source: str = "", source_url: str = "") -> Agent:
    """Start a local `claude -p` run and stream its progress into the registry.

    For ``gate="code"`` the agent gets the WRITER toolset (no commit/push) and,
    on a clean exit, the server captures the working-tree diff and parks the
    agent in ``awaiting_approval`` — committing waits for the user's click.
    """
    if allowed_tools is None:
        allowed_tools = {
            "code": WRITER_ALLOWED_TOOLS,
            "draft": DRAFT_ALLOWED_TOOLS,
        }.get(gate, DEFAULT_ALLOWED_TOOLS)
    resolved_cwd = str(Path(cwd).expanduser())
    # For a code/draft task that names a PR, fetch ALL its review comments
    # server-side (deterministic gh, not the sandboxed agent) and inject them, so
    # the agent works the whole PR's feedback — not just the one issue the digest
    # mentioned. The agent has no `gh api`, so this is the only way it sees them.
    feedback = _fetch_pr_feedback(prompt, resolved_cwd) if gate in ("code", "draft") else ""
    # Build the FULL prompt the agent runs: task + any PR feedback + a pointer to
    # the Slack thread it came from. agent.prompt is the source of truth that gets
    # run (not the bare `prompt` arg) so injected context actually reaches the CLI.
    full_prompt = _with_slack_source(_with_pr_feedback(prompt, feedback), source_url)
    agent = Agent(id=uuid.uuid4().hex[:8], name=name,
                  prompt=full_prompt, cwd=resolved_cwd, gate=gate)
    agent.allowed_tools = allowed_tools
    agent.model = model
    agent.pr_feedback = feedback
    agent.source = source
    agent.source_url = source_url
    REGISTRY.add(agent)
    threading.Thread(target=_run_agent, args=(agent, agent.prompt), daemon=True).start()
    return agent


def _run_agent(agent: Agent, prompt: str, resume_session: str | None = None) -> None:
    """Stream one `claude -p` run into the registry, then decide the terminal state.

    Used for both the first spawn and a `--resume` re-run with a correcting hint.
    """
    # Pass the prompt on stdin, not as an argv. `claude -p <prompt>` treats a
    # prompt that starts with `-`/`--` (e.g. an injected "--- SLACK SOURCE ---"
    # header) as an unknown CLI option and dies at startup. stdin sidesteps argv
    # parsing entirely, so no prompt content can ever masquerade as a flag.
    cmd = [
        "claude", "-p",
        "--model", agent.model,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits",
        "--allowedTools", agent.allowed_tools,
    ]
    if resume_session:
        cmd += ["--resume", resume_session]
    try:
        proc = subprocess.Popen(
            cmd, cwd=agent.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        proc.stdin.write(prompt)
        proc.stdin.close()
    except FileNotFoundError:
        REGISTRY.push_event(agent, ProgressEvent("done", "claude CLI not found on PATH", "❌"))
        REGISTRY.set_status(agent, "failed")
        return

    # Drain stderr in a thread so a full pipe can't deadlock the stdout loop, and
    # so an early `claude` exit (dies at startup with no stdout) still leaves a
    # reason to show instead of a blank pane.
    stderr_tail: list[str] = []

    def _drain_stderr() -> None:
        for ln in proc.stderr:
            stderr_tail.append(ln)
            del stderr_tail[:-50]  # keep only the last 50 lines

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    agent.proc = proc
    REGISTRY.set_status(agent, "running")
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        for ev in parse_event(evt):
            if ev.kind == "start" and ev.meta.get("session_id"):
                agent.session_id = ev.meta["session_id"]
            REGISTRY.push_event(agent, ev)
    rc = proc.wait()
    err_thread.join(timeout=2)
    if rc != 0:
        why = "".join(stderr_tail).strip() or f"claude exited with code {rc} (no output — likely a transient startup failure; try again)"
        REGISTRY.push_event(agent, ProgressEvent("done", why, "❌"))
        REGISTRY.set_status(agent, "failed")
        return
    if agent.gate == "code":
        _finish_code_agent(agent)
    elif agent.gate == "draft":
        _finish_draft_agent(agent)
    else:
        REGISTRY.set_status(agent, "done")


# Phrases that mean the agent stopped to ask / wasn't sure / didn't actually do the
# work. A code agent that ends like this must NOT be marked done — it goes to
# needs_input so the human can correct it and re-run with a hint.
_UNCERTAINTY_MARKERS = (
    "let me know", "would you like", "do you want", "should i", "which option",
    "option 1", "option 2", "options:", "i'm not sure", "i am not sure", "unsure",
    "please confirm", "could you clarify", "two options", "either way",
    "your call", "up to you", "i recommend", "waiting for", "before i proceed",
)


def _looks_uncertain(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _UNCERTAINTY_MARKERS)


def _finish_code_agent(agent: Agent) -> None:
    """Decide the terminal state of a finished code agent.

    The dumb failure we are guarding against: the agent makes NO change, declares
    "work is done", and the run is marked done — when really it picked the wrong
    branch or wasn't sure. So: no diff OR an uncertain-sounding ending => needs_input
    (a human checkpoint), never a silent done.
    """
    try:
        snap = capture_diff(agent.cwd)
    except Exception as exc:  # noqa: BLE001 - surface any git failure to the UI
        REGISTRY.push_event(agent, ProgressEvent("done", f"diff capture failed: {exc}", "❌"))
        REGISTRY.set_status(agent, "failed")
        return

    final = agent.final_output()
    has_diff = bool(snap.diff.strip())

    if not has_diff:
        REGISTRY.set_note(
            agent,
            "Agent ended WITHOUT making any code change. It may have picked the "
            "wrong branch or assumed the work was already done. Review its reasoning "
            "below, then re-run with a hint (e.g. tell it the correct PR branch).")
        REGISTRY.push_event(agent, ProgressEvent(
            "done", "⚠️ no change made — needs your input before this counts as done", "✋",
            full=final or "(no final message)"))
        REGISTRY.set_status(agent, "needs_input")
        return

    if _looks_uncertain(final):
        REGISTRY.set_note(
            agent,
            "Agent made a change but ended UNSURE (it asked a question or listed "
            "options). Read its diff and message, then approve, discard, or re-run "
            "with a hint.")
        # still capture the diff so she can see what it did do
        _attach_diff(agent, snap)
        REGISTRY.push_event(agent, ProgressEvent(
            "done", "✋ agent is unsure — needs your decision", "✋", full=final))
        REGISTRY.set_status(agent, "needs_input")
        return

    _park_for_approval(agent, snap)


_SOURCE_RE = re.compile(r"<<<SOURCE>>>(.*?)<<<END SOURCE>>>", re.DOTALL)
_ISSUES_RE = re.compile(r"<<<ISSUES>>>(.*?)<<<END ISSUES>>>", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_TASK_MARKER = "--- TASK ---\n"


def _bare_task(prompt: str) -> str:
    """The human task text, stripped of any prepended gate/brain preamble."""
    return prompt.rsplit(_TASK_MARKER, 1)[-1].strip()


def _parse_source(text: str) -> str:
    """Pull the original request (Bugbot comment / review note) the agent quoted.

    The code preamble asks the agent to emit a `<<<SOURCE>>> … <<<END SOURCE>>>`
    block so the dashboard can show WHY the change was needed beside the diff.
    """
    m = _SOURCE_RE.search(text or "")
    return m.group(1).strip() if m else ""


def _parse_issues(text: str) -> list[dict]:
    """Pull the agent's issue→file mapping from its `<<<ISSUES>>>` JSON block.

    Each item links one reviewer finding to the files changed for it, so the
    dashboard can render a card per issue with a jump-link to its diff. Returns
    [] on a missing/malformed block — the diff still renders without the cards.
    """
    m = _ISSUES_RE.search(text or "")
    if not m:
        return []
    arr = _JSON_ARRAY_RE.search(m.group(1))
    if not arr:
        return []
    try:
        specs = json.loads(arr.group(0))
    except json.JSONDecodeError:
        return []
    issues: list[dict] = []
    for s in specs if isinstance(specs, list) else []:
        if not isinstance(s, dict) or not s.get("title"):
            continue
        files = s.get("files") or []
        issues.append({
            "title": str(s["title"])[:120],
            "severity": str(s.get("severity", ""))[:12],
            "did": str(s.get("did", ""))[:300],
            "files": [str(f) for f in files if isinstance(f, str)],
        })
    return issues


def _attach_diff(agent: Agent, snap) -> None:
    """Store a captured diff snapshot on the agent for the dashboard to render."""
    target = (f"existing PR #{snap.existing_pr} (branch `{snap.branch}`)"
              if snap.existing_pr else f"new branch `{snap.branch}` → would open a Draft PR")
    by_file = split_diff_by_file(snap.diff)
    # Per-file approval rows: each changed file is committed or discarded on its
    # own, so the user can ship one reviewer's fix without the others.
    file_rows = [
        {"path": p, "diff": by_file.get(p, ""), "status": "pending"}  # pending|committed|discarded
        for p in snap.files
    ]
    REGISTRY.set_diff(agent, {
        "branch": snap.branch,
        "diff": snap.diff,
        "files": snap.files,
        "file_rows": file_rows,
        "existing_pr": snap.existing_pr,
        "target": target,
        "task": _bare_task(agent.prompt),
        # Prefer the real PR feedback the server fetched (guaranteed complete) over
        # the agent's own re-quote; fall back to the agent's <<<SOURCE>>> block.
        "source": agent.pr_feedback or _parse_source(agent.final_output()),
        # The agent's issue→file mapping: lets the dashboard show a card per
        # reviewer finding, each linked to the file(s) it changed.
        "issues": _parse_issues(agent.final_output()),
    })


def _park_for_approval(agent: Agent, snap) -> None:
    """Attach the diff and wait for the user to approve committing."""
    _attach_diff(agent, snap)
    target = agent.diff["target"]
    REGISTRY.push_event(agent, ProgressEvent(
        "say", f"✋ change ready for your review → {target}. {len(snap.files)} file(s).",
        "✋", full=f"Target: {target}\nFiles:\n" + "\n".join(snap.files)))
    REGISTRY.set_status(agent, "awaiting_approval")


def approve_commit(agent_id: str, commit_msg: str | None = None) -> dict:
    """User approved the diff → commit + push (existing branch) or open Draft PR (new)."""
    agent = REGISTRY.get(agent_id)
    if agent is None:
        return {"ok": False, "error": "agent not found"}
    if agent.status not in ("awaiting_approval", "needs_input") or not agent.diff:
        return {"ok": False, "error": f"agent is {agent.status}, nothing to commit"}

    REGISTRY.set_status(agent, "committing")
    msg = commit_msg or f"{agent.name}\n\nCo-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
    res = commit_and_push(
        cwd=agent.cwd, branch=agent.diff["branch"],
        commit_msg=msg, existing_pr=agent.diff["existing_pr"],
    )
    REGISTRY.push_event(agent, ProgressEvent(
        "done", res.message, "✅" if res.ok else "❌", full=res.message))
    REGISTRY.set_status(agent, "done" if res.ok else "awaiting_approval")
    return {"ok": res.ok, "message": res.message, "pr_url": res.pr_url}


def stop_agent(agent_id: str) -> dict:
    """Kill a running agent's process (the one realistic way to interrupt a headless run)."""
    agent = REGISTRY.get(agent_id)
    if agent is None:
        return {"ok": False, "error": "agent not found"}
    if agent.proc and agent.status in ("running", "starting"):
        agent.proc.terminate()
        REGISTRY.push_event(agent, ProgressEvent("done", "stopped by user", "⏹"))
        REGISTRY.set_status(agent, "failed")
        return {"ok": True, "message": "stopped"}
    return {"ok": False, "error": f"agent is {agent.status}, not running"}


def reject_diff(agent_id: str) -> dict:
    """User rejected the diff → stash the work (recoverable) and mark done."""
    agent = REGISTRY.get(agent_id)
    if agent is None:
        return {"ok": False, "error": "agent not found"}
    if agent.status not in ("awaiting_approval", "needs_input"):
        return {"ok": False, "error": f"agent is {agent.status}"}
    if not agent.diff:
        REGISTRY.set_status(agent, "done")
        return {"ok": True, "message": "nothing to discard"}
    res = discard(agent.cwd, label=f"morning-digest:{agent.name}")
    REGISTRY.push_event(agent, ProgressEvent("done", res.message, "🗑️", full=res.message))
    REGISTRY.set_diff(agent, None)
    REGISTRY.set_status(agent, "done")
    return {"ok": res.ok, "message": res.message}


def _file_row(agent: Agent, path: str) -> dict | None:
    for r in (agent.diff or {}).get("file_rows", []):
        if r["path"] == path:
            return r
    return None


def approve_file(agent_id: str, path: str, commit_msg: str | None = None) -> dict:
    """Commit + push ONE approved file, leaving the others for separate review."""
    agent = REGISTRY.get(agent_id)
    if agent is None or not agent.diff:
        return {"ok": False, "error": "no diff to commit"}
    if agent.status not in ("awaiting_approval", "needs_input", "committing"):
        return {"ok": False, "error": f"agent is {agent.status}, nothing to commit"}
    row = _file_row(agent, path)
    if row is None:
        return {"ok": False, "error": "file not in diff"}
    if row["status"] != "pending":
        return {"ok": False, "error": f"file already {row['status']}"}

    REGISTRY.set_status(agent, "committing")
    msg = commit_msg or f"{agent.name} ({path})\n\nCo-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
    res = commit_files(cwd=agent.cwd, branch=agent.diff["branch"], files=[path],
                       commit_msg=msg, existing_pr=agent.diff["existing_pr"])
    row["status"] = "committed" if res.ok else "pending"
    REGISTRY.push_event(agent, ProgressEvent(
        "done" if res.ok else "say", res.message, "✅" if res.ok else "❌", full=res.message))
    _settle_file_approval(agent)
    return {"ok": res.ok, "message": res.message, "pr_url": res.pr_url}


def discard_file(agent_id: str, path: str) -> dict:
    """Stash ONE file's change (recoverable), leaving the others."""
    agent = REGISTRY.get(agent_id)
    if agent is None or not agent.diff:
        return {"ok": False, "error": "no diff"}
    row = _file_row(agent, path)
    if row is None:
        return {"ok": False, "error": "file not in diff"}
    if row["status"] != "pending":
        return {"ok": False, "error": f"file already {row['status']}"}
    res = discard_files(agent.cwd, [path], label=f"morning-digest:{agent.name}:{path}")
    if res.ok:
        row["status"] = "discarded"
    REGISTRY.push_event(agent, ProgressEvent(
        "done" if res.ok else "say", res.message, "🗑️" if res.ok else "❌", full=res.message))
    _settle_file_approval(agent)
    return {"ok": res.ok, "message": res.message}


def _settle_file_approval(agent: Agent) -> None:
    """Mark the agent done once every file is committed or discarded; else re-arm."""
    rows = (agent.diff or {}).get("file_rows", [])
    if rows and all(r["status"] != "pending" for r in rows):
        REGISTRY.set_status(agent, "done")
    else:
        # Files remain: come back out of the transient 'committing' state.
        if agent.status == "committing":
            REGISTRY.set_status(agent, "awaiting_approval")
        REGISTRY.set_diff(agent, agent.diff)  # broadcast updated per-file statuses


_COMMENTS_MARKER = "<<<COMMENTS>>>"
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_draft_comments(text: str) -> list[dict]:
    """Pull the agent's `<<<COMMENTS>>>` JSON array of {pr, body} draft comments.

    Each becomes a row in the dashboard the user approves one at a time. Bad or
    missing JSON yields no drafts (the full text is still shown as Output/Draft).
    """
    if _COMMENTS_MARKER not in text:
        return []
    _, _, tail = text.partition(_COMMENTS_MARKER)
    m = _JSON_ARRAY_RE.search(tail)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    drafts = []
    for i, it in enumerate(items if isinstance(items, list) else []):
        if not isinstance(it, dict) or not it.get("body"):
            continue
        pr = it.get("pr")
        drafts.append({
            "idx": i,
            "pr": int(pr) if str(pr).isdigit() else None,
            "body": str(it["body"]),
            "status": "pending",   # pending | posted | discarded
        })
    return drafts


def _finish_draft_agent(agent: Agent) -> None:
    """A draft agent finished: park its draft comments for per-item approval.

    Nothing is posted. If the agent produced structured draft comments, the
    dashboard shows each with [Post to PR]/[Discard]; if not, it just shows the
    full draft text. Either way the agent had no tools to post on its own.
    """
    drafts = _parse_draft_comments(agent.final_output())
    if drafts:
        REGISTRY.set_drafts(agent, drafts)
        REGISTRY.push_event(agent, ProgressEvent(
            "say", f"✋ {len(drafts)} draft comment(s) ready — approve each to post.", "✋"))
        REGISTRY.set_status(agent, "awaiting_approval")
    else:
        # No structured comments; the draft text is in Output/Draft. Done (nothing to post).
        REGISTRY.set_status(agent, "done")


def post_draft_comment(agent_id: str, idx: int) -> dict:
    """Post ONE approved draft comment to its PR (only after the user clicks)."""
    agent = REGISTRY.get(agent_id)
    if agent is None or not agent.drafts:
        return {"ok": False, "error": "no drafts to post"}
    draft = next((d for d in agent.drafts if d["idx"] == idx), None)
    if draft is None:
        return {"ok": False, "error": "draft not found"}
    if draft["status"] != "pending":
        return {"ok": False, "error": f"draft already {draft['status']}"}
    if not draft["pr"]:
        return {"ok": False, "error": "draft has no PR number"}
    res = post_pr_comment(agent.cwd, draft["pr"], draft["body"])
    draft["status"] = "posted" if res.ok else "pending"
    REGISTRY.push_event(agent, ProgressEvent(
        "done" if res.ok else "say", res.message, "✅" if res.ok else "❌"))
    _settle_draft_agent(agent)
    return {"ok": res.ok, "message": res.message}


def discard_draft_comment(agent_id: str, idx: int) -> dict:
    """Mark one draft comment discarded (never posted)."""
    agent = REGISTRY.get(agent_id)
    if agent is None or not agent.drafts:
        return {"ok": False, "error": "no drafts"}
    draft = next((d for d in agent.drafts if d["idx"] == idx), None)
    if draft is None:
        return {"ok": False, "error": "draft not found"}
    draft["status"] = "discarded"
    REGISTRY.set_drafts(agent, agent.drafts)
    _settle_draft_agent(agent)
    return {"ok": True, "message": "discarded"}


def _settle_draft_agent(agent: Agent) -> None:
    """Mark the draft agent done once every draft is posted or discarded."""
    if agent.drafts and all(d["status"] != "pending" for d in agent.drafts):
        REGISTRY.set_status(agent, "done")
    else:
        REGISTRY.set_drafts(agent, agent.drafts)  # broadcast updated statuses


def rerun_with_hint(agent_id: str, hint: str) -> dict:
    """Resume a needs_input agent's session with a correcting hint and run again.

    Headless `claude -p` can't be steered mid-task, so the correction lands as a
    NEW turn on the same session via `--resume`: the agent keeps all its prior
    context (what it already looked at) and gets the human's nudge on top.
    """
    agent = REGISTRY.get(agent_id)
    if agent is None:
        return {"ok": False, "error": "agent not found"}
    if agent.status != "needs_input":
        return {"ok": False, "error": f"agent is {agent.status}, not waiting for input"}
    if not agent.session_id:
        return {"ok": False, "error": "no session id to resume"}
    hint = (hint or "").strip()
    if not hint:
        return {"ok": False, "error": "empty hint"}

    REGISTRY.set_note(agent, "")
    REGISTRY.set_diff(agent, None)
    REGISTRY.push_event(agent, ProgressEvent("say", f"↻ re-run with your hint: {hint}", "↻", full=hint))
    threading.Thread(
        target=_run_agent, args=(agent, hint),
        kwargs={"resume_session": agent.session_id}, daemon=True,
    ).start()
    return {"ok": True, "message": "re-running with hint"}


# States a chat turn may resume from: anything that has stopped. (running would
# collide with the live process; committing is a transient gate action.)
_CHATTABLE_STATES = {"done", "failed", "needs_input", "awaiting_approval"}


def chat_with_agent(agent_id: str, message: str) -> dict:
    """Continue a finished agent as a follow-up chat turn on the SAME session.

    This is the dashboard's "talk to Claude" box. Like rerun_with_hint it resumes
    the claude session via --resume so the agent keeps everything it already saw
    (the thread it read, the diff it made), but unlike rerun it works from any
    stopped state — so you can ask a done analysis a follow-up, or tell a code
    agent to also fix one more thing. The task's original gate/toolset is reused,
    so a code task that edits again simply re-enters the diff-approval gate.
    """
    agent = REGISTRY.get(agent_id)
    if agent is None:
        return {"ok": False, "error": "agent not found"}
    if agent.status == "running" or agent.status == "starting":
        return {"ok": False, "error": "agent is still running — wait for it to finish"}
    if agent.status not in _CHATTABLE_STATES:
        return {"ok": False, "error": f"can't chat while agent is {agent.status}"}
    if not agent.session_id:
        return {"ok": False, "error": "no session id to resume"}
    message = (message or "").strip()
    if not message:
        return {"ok": False, "error": "empty message"}

    # Clear any prior gate state: this turn produces a fresh outcome (new diff,
    # new analysis) that will re-park the agent if it's a code/draft task.
    REGISTRY.set_note(agent, "")
    REGISTRY.set_diff(agent, None)
    REGISTRY.set_drafts(agent, None)
    REGISTRY.push_event(agent, ProgressEvent("say", f"💬 you: {message}", "💬", full=message))
    threading.Thread(
        target=_run_agent, args=(agent, message),
        kwargs={"resume_session": agent.session_id}, daemon=True,
    ).start()
    return {"ok": True, "message": "continuing the conversation"}


# ---------------------------------------------------------------------------
# HTTP layer: dashboard page + JSON API + SSE stream
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Morning Digest · Agents</title>
<style>
  :root { color-scheme: dark; --bg:#0f1115; --panel:#161922; --line:#262b36; --muted:#8b93a7; --dim:#5b6478; }
  * { box-sizing:border-box; }
  body { font: 14px/1.5 -apple-system, system-ui, sans-serif; margin:0; background:var(--bg); color:#e6e6e6; height:100vh; display:flex; flex-direction:column; overflow:hidden; }
  header { padding:12px 18px; background:var(--panel); border-bottom:1px solid var(--line); display:flex; align-items:center; gap:10px; flex:0 0 auto; }
  header h1 { font-size:15px; margin:0; font-weight:600; }
  header small { color:var(--muted); }
  .layout { flex:1; display:flex; min-height:0; }
  /* draggable gutters between panes */
  .gutter { flex:0 0 6px; cursor:col-resize; background:var(--line); position:relative; }
  .gutter:hover, .gutter.drag { background:#3a6; }
  .gutter::after { content:""; position:absolute; inset:0 -3px; }
  /* left rail: list of agents */
  .rail { width:230px; flex:0 0 auto; background:var(--panel); overflow:auto; }
  .rail .it { padding:10px 12px; border-bottom:1px solid var(--line); cursor:pointer; }
  .rail .it:hover { background:#1c2129; }
  .rail .it.sel { background:#1b2030; border-left:3px solid #5fa8e3; }
  .rail .it .nm { font-size:13px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .rail .it .sub { font-size:11px; color:var(--muted); margin-top:2px; display:flex; justify-content:space-between; }
  .badge { font-size:10px; padding:1px 7px; border-radius:99px; font-weight:600; }
  .running { background:#1d3b2a; color:#5fe39a; } .starting{ background:#3a341d; color:#e3cc5f;}
  .done { background:#1d2a3b; color:#5fa8e3; } .failed{ background:#3b1d1d; color:#e35f5f;}
  .awaiting_approval { background:#3a2a14; color:#f0a85f; } .committing{ background:#2a2a3b; color:#9f9fe3;}
  .needs_input { background:#3b2a3a; color:#e39ad8; }
  /* center: narration feed */
  .feed-pane { flex:1 1 0; min-width:0; display:flex; flex-direction:column; }
  .pane-h { padding:10px 14px; border-bottom:1px solid var(--line); font-size:12px; color:var(--muted); display:flex; justify-content:space-between; align-items:center; flex:0 0 auto; }
  .feed { flex:1; overflow:auto; padding:6px 0; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
  .row { padding:3px 14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .row.has-full { cursor:pointer; }
  .row.has-full:hover { background:#1c2129; }
  .row.open { white-space:pre-wrap; word-break:break-word; background:#0c0e12; border-left:2px solid #3a6; padding:8px 14px; }
  .row .t { color:var(--dim); margin-right:8px; }
  .row .caret { color:var(--dim); margin-right:4px; font-size:10px; }
  .row.tool { color:#9fd0ff; } .row.say{ color:#e6e6e6;} .row.result{ color:var(--muted);} .row.done{ color:#5fe39a; font-weight:600;} .row.todo{ color:#c9a8ff;}
  /* right: artifact pane (todo + diff/draft/output) */
  .art { flex:0 0 46%; min-width:280px; overflow:auto; background:#12141a; }
  .art .sec { border-bottom:1px solid var(--line); }
  .art .sec h3 { font-size:11px; text-transform:uppercase; letter-spacing:.06em; margin:0; padding:9px 14px; color:var(--muted); background:#14171f; position:sticky; top:0; display:flex; justify-content:space-between; align-items:center; }
  .art .sec h3 .diffmode { display:flex; gap:4px; text-transform:none; letter-spacing:0; }
  .art .sec h3 .diffmode button { font:10px system-ui; padding:2px 8px; border-radius:5px; border:1px solid var(--line); background:#0f1115; color:var(--muted); cursor:pointer; }
  .art .sec h3 .diffmode button.on { background:#1d2a3b; color:#9fd0ff; border-color:#2d5a8a; }
  /* "why this change": task + original source side by side */
  .why { display:flex; gap:0; }
  .why .col { flex:1 1 0; min-width:0; padding:10px 14px; }
  .why .col + .col { border-left:1px solid var(--line); }
  .why .col .lbl { font-size:10px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim); margin-bottom:5px; }
  .why .col .bd { font-size:12px; color:#cdd3df; white-space:pre-wrap; word-break:break-word; }
  .why .col.src .bd { color:#e3cc9a; }
  /* Issue cards: one per reviewer finding, each linking to the file it changed. */
  .issues { padding:8px 14px 12px; display:flex; flex-direction:column; gap:8px; }
  .icard { border:1px solid var(--line); border-radius:7px; padding:9px 11px; background:#10141c; }
  .icard .ih { display:flex; align-items:baseline; gap:8px; margin-bottom:4px; }
  .icard .inum { color:var(--dim); font-size:11px; flex:0 0 auto; }
  .icard .ititle { font-size:13px; color:#e6ebf5; font-weight:600; }
  .icard .sev { font-size:9.5px; text-transform:uppercase; letter-spacing:.05em; padding:1px 6px; border-radius:4px; flex:0 0 auto; }
  .icard .sev.high { background:#3a1620; color:#ff9aa6; } .icard .sev.medium { background:#3a2c14; color:#f0c46a; }
  .icard .sev.low { background:#16243a; color:#9fd0ff; } .icard .sev.none { background:#1a1e27; color:var(--muted); }
  .icard .idid { font-size:12px; color:#c5ccda; margin-bottom:5px; }
  .icard .ifiles { display:flex; flex-wrap:wrap; gap:6px; }
  .icard .ifile { font:11px ui-monospace,SFMono-Regular,Menlo,monospace; color:#7fc0ff; background:#0d1117; border:1px solid var(--line); border-radius:4px; padding:2px 7px; cursor:pointer; }
  .icard .ifile:hover { border-color:#2d5a8a; background:#15202e; }
  .icard .ifile.none { color:var(--muted); cursor:default; } .icard .ifile.none:hover { border-color:var(--line); background:#0d1117; }
  .frow.flash { animation:flash 1.2s ease-out; }
  @keyframes flash { 0%{ background:#1d2a3b; } 100%{ background:transparent; } }
  details.rawwhy { border-top:1px solid var(--line); }
  details.rawwhy > summary { cursor:pointer; padding:7px 14px; font-size:11px; color:var(--muted); list-style:none; }
  details.rawwhy > summary::-webkit-details-marker { display:none; }
  details.rawwhy > summary:hover { color:#9fb6d4; }
  details.rawwhy[open] > summary { border-bottom:1px solid var(--line); }
  .srcbanner { padding:8px 14px; font-size:12px; color:#9fb6d4; background:#11161f; border-bottom:1px solid var(--line); }
  .srcbanner a { color:#7fc0ff; }
  .todo { list-style:none; margin:0; padding:6px 0; }
  .todo li { padding:4px 14px; font-size:13px; display:flex; gap:8px; align-items:baseline; }
  .todo li .mk { width:14px; flex:0 0 auto; }
  .todo li.completed { color:var(--dim); text-decoration:line-through; }
  .todo li.in_progress { color:#e3cc5f; font-weight:600; }
  .art pre { margin:0; padding:10px 14px; white-space:pre-wrap; word-break:break-word; font-size:12px; color:#d7dbe3; }
  .art pre.diff { white-space:pre; overflow:auto; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11.5px; line-height:1.45; }
  pre.diff .add { color:#5fe39a; } pre.diff .del{ color:#e38f8f;} pre.diff .hdr{ color:#9fd0ff;} pre.diff .at{ color:#c08fe3;}
  /* side-by-side diff */
  table.sxs { width:100%; border-collapse:collapse; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11.5px; line-height:1.45; table-layout:fixed; }
  table.sxs td { vertical-align:top; padding:0 8px; white-space:pre-wrap; word-break:break-word; width:50%; border-left:1px solid var(--line); }
  table.sxs td:first-child { border-left:0; }
  table.sxs tr.hdr td { color:#9fd0ff; background:#11151c; }
  table.sxs tr.at td { color:#c08fe3; background:#15121c; }
  table.sxs td.del { background:#241619; color:#e38f8f; }
  table.sxs td.add { background:#14241a; color:#5fe39a; }
  table.sxs td.ctx { color:#9aa3b5; }
  table.sxs td.pad { background:#0d0f13; }
  .gate-acts { display:flex; gap:8px; padding:12px 14px; background:#1a160f; position:sticky; bottom:0; border-top:1px solid var(--line); }
  .gate-acts button { font:13px system-ui; padding:8px 16px; border-radius:7px; border:0; cursor:pointer; font-weight:600; }
  button.ok { background:#1d6b3a; color:#cfffe0; } button.ok:hover{ background:#23824a;}
  button.no { background:#3b1d1d; color:#ffd5d5; } button.no:hover{ background:#5a2a2a;}
  button.stop { background:#4a2a14; color:#ffd9b0; } button:disabled{ opacity:.5; cursor:default; }
  .target { padding:9px 14px; color:#f0a85f; font-size:12px; background:#1a160f; }
  .empty { color:var(--dim); padding:40px; text-align:center; }
  .note { padding:11px 14px; color:#e39ad8; font-size:12.5px; background:#1f1320; border-left:3px solid #a85fc0; }
  .hint { padding:12px 14px; background:#1a1320; border-top:1px solid var(--line); }
  .hint textarea { width:100%; min-height:54px; resize:vertical; background:#0f1115; color:#e6e6e6; border:1px solid var(--line); border-radius:7px; padding:8px; font:13px system-ui; }
  .hint button { margin-top:8px; }
  button.rerun { background:#3a2a52; color:#e9d5ff; } button.rerun:hover{ background:#4a356a;}
  /* chat box: talk to the agent (resumes its session) */
  .chat { padding:12px 14px; background:#11151c; border-top:1px solid var(--line); }
  .chat textarea { width:100%; min-height:46px; resize:vertical; background:#0f1115; color:#e6e6e6; border:1px solid var(--line); border-radius:7px; padding:8px; font:13px system-ui; }
  .chat .crow { display:flex; gap:8px; align-items:center; margin-top:8px; }
  .chat button { font:13px system-ui; padding:8px 16px; border-radius:7px; border:0; cursor:pointer; font-weight:600; background:#1d3b2a; color:#5fe39a; }
  .chat button:hover { background:#244a35; }
  .chat button:disabled { opacity:.5; cursor:default; }
  .chat .hint-txt { font-size:11px; color:var(--dim); }
  .draft { padding:10px 14px; border-bottom:1px solid var(--line); }
  .draft .dh { font-size:11px; color:var(--muted); margin-bottom:5px; }
  .draft .dstatus { text-transform:uppercase; letter-spacing:.04em; }
  .draft.st-posted { opacity:.6; } .draft.st-posted .dstatus{ color:#5fe39a;}
  .draft.st-discarded { opacity:.45; } .draft.st-discarded .dstatus{ color:#e38f8f;}
  .draft pre { margin:0; padding:8px 10px; background:#0c0e12; border-radius:6px; white-space:pre-wrap; word-break:break-word; font-size:12px; }
  /* per-file approval rows */
  .frow { border-bottom:1px solid var(--line); padding:0 0 8px; }
  .frow .fh { display:flex; justify-content:space-between; align-items:center; padding:8px 14px; background:#14171f; position:sticky; top:30px; }
  .frow .fp { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; color:#cdd3df; word-break:break-all; }
  .frow .fstatus { font-size:10px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); padding-left:10px; flex:0 0 auto; }
  .frow.st-committed { opacity:.55; } .frow.st-committed .fstatus{ color:#5fe39a;}
  .frow.st-discarded { opacity:.4; } .frow.st-discarded .fstatus{ color:#e38f8f;}
  .frow .gate-acts { padding-left:14px; padding-right:14px; }
</style></head>
<body>
<header><h1>Morning Digest · Agents</h1><small id="status">connecting…</small></header>
<div class="layout" id="layout">
  <div class="rail" id="rail"></div>
  <div class="gutter" data-gutter="rail"></div>
  <div class="feed-pane" id="feedPane">
    <div class="pane-h"><span id="feedTitle">No agent selected</span><span id="feedMeta"></span></div>
    <div class="feed" id="feed"><div class="empty">No agents yet. Trigger one from Slack to watch it work here.</div></div>
  </div>
  <div class="gutter" data-gutter="art"></div>
  <div class="art" id="art"></div>
</div>
<script>
const openRows = new Set();
let LAST = [], SEL = null;
let DIFFMODE = localStorage.getItem('diffmode') || 'split';  // 'split' | 'unified'

function esc(s){ return (s||'').replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function colorDiff(d){
  return esc(d).split('\\n').map(l=>{
    if(l.startsWith('+++')||l.startsWith('---')||l.startsWith('diff ')||l.startsWith('index ')) return `<span class="hdr">${l}</span>`;
    if(l.startsWith('@@')) return `<span class="at">${l}</span>`;
    if(l.startsWith('+')) return `<span class="add">${l}</span>`;
    if(l.startsWith('-')) return `<span class="del">${l}</span>`;
    return l;
  }).join('\\n');
}
// Render a unified diff as a two-column (old | new) table. Deletions sit on the
// left, additions on the right; aligned pairs share a row, context spans both.
function sideBySide(d){
  let rows='';
  const flush=(dels,adds)=>{
    const n=Math.max(dels.length,adds.length);
    for(let i=0;i<n;i++){
      const l=dels[i], r=adds[i];
      const lc=l===undefined?'pad':'del', rc=r===undefined?'pad':'add';
      rows+=`<tr><td class="${lc}">${l===undefined?'':esc(l)||'&nbsp;'}</td><td class="${rc}">${r===undefined?'':esc(r)||'&nbsp;'}</td></tr>`;
    }
  };
  let dels=[], adds=[];
  (d||'').split('\\n').forEach(line=>{
    if(line.startsWith('+++')||line.startsWith('---')||line.startsWith('diff ')||line.startsWith('index ')){
      flush(dels,adds); dels=[]; adds=[];
      rows+=`<tr class="hdr"><td colspan="2">${esc(line)}</td></tr>`;
    } else if(line.startsWith('@@')){
      flush(dels,adds); dels=[]; adds=[];
      rows+=`<tr class="at"><td colspan="2">${esc(line)}</td></tr>`;
    } else if(line.startsWith('-')){
      dels.push(line.slice(1));
    } else if(line.startsWith('+')){
      adds.push(line.slice(1));
    } else {
      flush(dels,adds); dels=[]; adds=[];
      const c=esc(line.startsWith(' ')?line.slice(1):line)||'&nbsp;';
      rows+=`<tr><td class="ctx">${c}</td><td class="ctx">${c}</td></tr>`;
    }
  });
  flush(dels,adds);
  return `<table class="sxs">${rows}</table>`;
}
function renderDiff(d){
  return DIFFMODE==='split' ? sideBySide(d) : `<pre class="diff">${colorDiff(d)}</pre>`;
}
function diffToggle(){
  const on=m=>m===DIFFMODE?' on':'';
  return `<span class="diffmode">
    <button data-diffmode="split" class="${on('split').trim()}">side-by-side</button>
    <button data-diffmode="unified" class="${on('unified').trim()}">unified</button></span>`;
}
// Provenance banner: where this whole task came from (from the digest). Shows
// for ANY agent, even before/without a diff, so the user can always trace it.
function sourceBanner(a){
  if(!a.source) return '';
  const txt = a.source_url
    ? `<a href="${esc(a.source_url)}" target="_blank">${esc(a.source)}</a>`
    : esc(a.source);
  return `<div class="srcbanner">📎 From: ${txt}</div>`;
}
// A stable DOM id for a file's diff row, so an issue card can scroll to it.
function fileAnchor(path){ return 'f-' + (path||'').replace(/[^a-zA-Z0-9]/g,'-'); }
function whyBlock(diff){
  if(!diff || (!diff.task && !diff.source && !(diff.issues||[]).length)) return '';
  const issues = diff.issues || [];
  let body = '';
  if(issues.length){
    // Primary view: one card per reviewer finding, each linked to its file diff.
    body += `<div class="issues">` + issues.map((it,i)=>{
      const sev = (it.severity||'').toLowerCase();
      const sevCls = sev.startsWith('high')?'high':sev.startsWith('med')?'medium':sev.startsWith('low')?'low':'none';
      const sevTag = it.severity ? `<span class="sev ${sevCls}">${esc(it.severity)}</span>` : '';
      const files = (it.files||[]).length
        ? (it.files||[]).map(f=>`<span class="ifile" data-jumpfile="${esc(fileAnchor(f))}" title="${esc(f)}">${esc(f.split('/').pop())} ↦</span>`).join('')
        : `<span class="ifile none">no file change</span>`;
      return `<div class="icard">
        <div class="ih"><span class="inum">${i+1}.</span><span class="ititle">${esc(it.title)}</span>${sevTag}</div>
        ${it.did?`<div class="idid">→ ${esc(it.did)}</div>`:''}
        <div class="ifiles">${files}</div></div>`;
    }).join('') + `</div>`;
  }
  // The full task + cleaned reviewer feedback, kept available but tucked away so
  // the cards (not the raw dump) are what the eye lands on first.
  const col=(cls,lbl,txt)=> txt ? `<div class="col ${cls}"><div class="lbl">${lbl}</div><div class="bd">${esc(txt)}</div></div>` : '';
  const raw = col('task','Task', diff.task) + col('src','Original request (all PR review feedback)', diff.source);
  if(raw){
    const openByDefault = !issues.length;  // no cards → show the raw text directly
    body += `<details class="rawwhy"${openByDefault?' open':''}><summary>${issues.length?'Show full task + reviewer feedback':'Task + reviewer feedback'}</summary><div class="why">${raw}</div></details>`;
  }
  return `<div class="sec"><h3>Why this change</h3>${body}</div>`;
}
function cur(){ return LAST.find(a=>a.id===SEL) || LAST[0] || null; }

function renderRail(){
  document.getElementById('rail').innerHTML = LAST.map(a=>{
    const sel = a.id===(cur()||{}).id ? ' sel':'';
    const td = a.todos||[]; const done = td.filter(t=>t.status==='completed').length;
    const plan = td.length ? `${done}/${td.length} steps` : '';
    return `<div class="it${sel}" data-pick="${a.id}">
      <div class="nm">${esc(a.name)}</div>
      <div class="sub"><span class="badge ${a.status}">${a.status}</span><span>${plan||a.elapsed+'s'}</span></div>
    </div>`;
  }).join('') || '<div class="empty" style="padding:20px">No agents</div>';
}

function renderFeed(){
  const a = cur();
  const ft = document.getElementById('feedTitle'), fm = document.getElementById('feedMeta'), feed = document.getElementById('feed');
  if(!a){ ft.textContent='No agent selected'; fm.textContent=''; feed.innerHTML='<div class="empty">No agents yet. Trigger one from Slack to watch it work here.</div>'; return; }
  ft.textContent = a.name; fm.innerHTML = `<span class="badge ${a.status}">${a.status}</span> · ${a.elapsed}s`;
  feed.innerHTML = a.events.filter(e=>e.kind!=='todo').map((e,i)=>{
    const key=a.id+':'+i;
    const hasFull = e.full && e.full.length>(e.text||'').length;
    const open = openRows.has(key);
    const body = open ? esc(e.full) : esc(e.text);
    const caret = hasFull ? `<span class="caret">${open?'▼':'▶'}</span>`:'';
    return `<div class="row ${e.kind}${hasFull?' has-full':''}${open?' open':''}" data-key="${key}"><span class="t">${e.t}s</span>${caret}${e.icon||''} ${body}</div>`;
  }).join('') || '<div class="row">…</div>';
}

function renderArt(){
  const a = cur(); const art = document.getElementById('art');
  if(!a){ art.innerHTML=''; return; }
  let html = '';
  html += sourceBanner(a);
  const td = a.todos||[];
  if(td.length){
    html += `<div class="sec"><h3>Plan</h3><ul class="todo">` + td.map(t=>{
      const mk = t.status==='completed'?'✓':(t.status==='in_progress'?'▸':'○');
      return `<li class="${t.status}"><span class="mk">${mk}</span>${esc(t.content||t.activeForm||'')}</li>`;
    }).join('') + `</ul></div>`;
  }
  if(a.status==='needs_input'){
    html += whyBlock(a.diff);
    html += `<div class="sec"><h3>✋ Needs your input${a.diff?diffToggle():''}</h3>`;
    if(a.note) html += `<div class="note">${esc(a.note)}</div>`;
    if(a.diff){
      html += `<div class="target">${esc(a.diff.target)}</div>`;
      html += renderDiff(a.diff.diff);
    }
    html += `<div class="hint">
      <textarea id="hintBox" placeholder="Tell it what to fix, e.g. you're on the wrong branch — first run: gh pr checkout 1098"></textarea>
      <div class="gate-acts" style="padding:0;background:none;border:0;">
        <button class="rerun" data-rerun="${a.id}">↻ Re-run with this hint</button>`;
    if(a.diff){
      html += `<button class="ok" data-approve="${a.id}">✅ Commit anyway</button>
               <button class="no" data-reject="${a.id}">🗑️ Discard</button>`;
    }
    html += `</div></div></div>`;
  }
  if(a.diff && (a.status==='awaiting_approval'||a.status==='committing')){
    html += whyBlock(a.diff);
    const rows = a.diff.file_rows||[];
    const busy = a.status==='committing';
    const pending = rows.filter(r=>r.status==='pending').length;
    html += `<div class="sec"><h3>Proposed change — approve each file${diffToggle()}</h3>`;
    html += `<div class="target">✋ ${esc(a.diff.target)} · ${pending} of ${rows.length} file(s) pending</div>`;
    if(rows.length){
      rows.forEach(r=>{
        html += `<div class="frow st-${r.status}" id="${fileAnchor(r.path)}">
          <div class="fh"><span class="fp">${esc(r.path)}</span><span class="fstatus">${r.status}</span></div>`;
        html += renderDiff(r.diff);
        if(r.status==='pending'){
          html += `<div class="gate-acts" style="position:static;padding:8px 0 4px;background:none;border:0;">
            <button class="ok" data-approvef="${a.id}" data-path="${esc(r.path)}" ${busy?'disabled':''}>✅ Commit this file</button>
            <button class="no" data-discardf="${a.id}" data-path="${esc(r.path)}" ${busy?'disabled':''}>🗑️ Discard this file</button></div>`;
        }
        html += `</div>`;
      });
      // Bulk fallback: commit / discard everything still pending in one go.
      if(pending>0){
        html += `<div class="gate-acts">`;
        html += busy ? `<span style="color:var(--muted)">⏳ committing…</span>`
                     : `<button class="ok" data-approve="${a.id}">✅ Commit all ${pending} remaining</button>
                        <button class="no" data-reject="${a.id}">🗑️ Discard all remaining</button>`;
        html += `</div>`;
      }
    } else {
      // No per-file split available — fall back to the whole diff + single approve.
      html += renderDiff(a.diff.diff);
      html += busy ? `<div class="gate-acts"><span style="color:var(--muted)">⏳ committing…</span></div>`
                   : `<div class="gate-acts"><button class="ok" data-approve="${a.id}">✅ Commit this change</button>
                      <button class="no" data-reject="${a.id}">🗑️ Discard</button></div>`;
    }
    html += `</div>`;
  }
  if(a.drafts && a.drafts.length){
    html += `<div class="sec"><h3>Draft PR comments — approve each to post</h3>`;
    a.drafts.forEach(d=>{
      const prtxt = d.pr ? `PR #${d.pr}` : '(no PR number)';
      html += `<div class="draft st-${d.status}">
        <div class="dh">${prtxt} · <span class="dstatus">${d.status}</span></div>
        <pre>${esc(d.body)}</pre>`;
      if(d.status==='pending'){
        html += `<div class="gate-acts" style="position:static;padding:8px 0 4px;background:none;border:0;">
          <button class="ok" data-post="${a.id}" data-idx="${d.idx}" ${d.pr?'':'disabled'}>✅ Post to PR</button>
          <button class="no" data-dropc="${a.id}" data-idx="${d.idx}">🗑️ Discard</button></div>`;
      }
      html += `</div>`;
    });
    html += `</div>`;
  }
  if(a.final_output){
    html += `<div class="sec"><h3>Output / Draft</h3><pre>${esc(a.final_output)}</pre></div>`;
  }
  if(a.status==='running'){
    html += `<div class="gate-acts"><button class="stop" data-stop="${a.id}">⏹ Stop agent</button></div>`;
  }
  // Talk to the agent: a follow-up turn resumes its session (it keeps everything
  // it already saw). Shown once it has stopped and has a session to resume.
  if(a.can_chat){
    const hint = a.gate==='code' ? 'It keeps its context; edits re-enter the diff gate.'
               : a.gate==='draft' ? 'It keeps its context; new drafts go to approval.'
               : 'It keeps its context from this run.';
    html += `<div class="chat">
      <textarea id="chatBox" placeholder="Ask a follow-up or tell it what to do next…"></textarea>
      <div class="crow">
        <button data-chat="${a.id}">💬 Send to Claude</button>
        <span class="hint-txt">${hint}</span>
      </div></div>`;
  }
  art.innerHTML = html || '<div class="empty" style="padding:30px">Nothing to show yet.</div>';
}

function render(agents){ LAST=agents; if(!SEL && agents.length) SEL=agents[0].id; renderRail(); renderFeed(); renderArt(); }

document.body.addEventListener('click', ev=>{
  const pick = ev.target.closest('[data-pick]');
  if(pick){ SEL=pick.dataset.pick; renderRail(); renderFeed(); renderArt(); return; }
  const row = ev.target.closest('.row.has-full');
  if(row){ const k=row.dataset.key; openRows.has(k)?openRows.delete(k):openRows.add(k); renderFeed(); return; }
  const ap = ev.target.closest('[data-approve]');
  if(ap){ ap.disabled=true; ap.textContent='committing…'; post('/approve',{id:ap.dataset.approve}); return; }
  const rj = ev.target.closest('[data-reject]');
  if(rj){ rj.disabled=true; post('/reject',{id:rj.dataset.reject}); return; }
  const st = ev.target.closest('[data-stop]');
  if(st){ st.disabled=true; post('/stop',{id:st.dataset.stop}); return; }
  const rr = ev.target.closest('[data-rerun]');
  if(rr){
    const box=document.getElementById('hintBox');
    const hint=(box&&box.value||'').trim();
    if(!hint){ alert('Type a hint first'); return; }
    rr.disabled=true; rr.textContent='re-running…';
    post('/rerun',{id:rr.dataset.rerun, hint}); return;
  }
  const ch = ev.target.closest('[data-chat]');
  if(ch){
    const box=document.getElementById('chatBox');
    const message=(box&&box.value||'').trim();
    if(!message){ alert('Type a message first'); return; }
    ch.disabled=true; ch.textContent='sending…';
    post('/chat',{id:ch.dataset.chat, message}); return;
  }
  const dm = ev.target.closest('[data-diffmode]');
  if(dm){ DIFFMODE=dm.dataset.diffmode; localStorage.setItem('diffmode',DIFFMODE); renderArt(); return; }
  const jf = ev.target.closest('[data-jumpfile]');
  if(jf){ const el=document.getElementById(jf.dataset.jumpfile);
    if(el){ el.scrollIntoView({behavior:'smooth',block:'start'}); el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); } return; }
  const af = ev.target.closest('[data-approvef]');
  if(af){ af.disabled=true; af.textContent='committing…';
    post('/approve-file',{id:af.dataset.approvef, path:af.dataset.path}); return; }
  const df = ev.target.closest('[data-discardf]');
  if(df){ df.disabled=true;
    post('/discard-file',{id:df.dataset.discardf, path:df.dataset.path}); return; }
  const pc = ev.target.closest('[data-post]');
  if(pc){ pc.disabled=true; pc.textContent='posting…';
    post('/post-comment',{id:pc.dataset.post, idx:Number(pc.dataset.idx)}); return; }
  const dc = ev.target.closest('[data-dropc]');
  if(dc){ dc.disabled=true;
    post('/discard-comment',{id:dc.dataset.dropc, idx:Number(dc.dataset.idx)}); }
});
function post(path,body){
  fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(r=>r.json()).then(res=>{ if(!res.ok) alert(res.error||res.message||'failed'); });
}
// Draggable panel dividers. The rail (left) and art (right) panes have explicit
// widths; the feed pane in the middle flexes to fill whatever is left. Widths
// persist in localStorage so a layout you like survives a reload.
(function(){
  const rail=document.getElementById('rail'), art=document.getElementById('art'),
        layout=document.getElementById('layout');
  const saved={rail:localStorage.getItem('w_rail'), art:localStorage.getItem('w_art')};
  if(saved.rail) rail.style.width=saved.rail+'px';
  if(saved.art) art.style.flexBasis=saved.art+'px';
  let drag=null;
  document.querySelectorAll('.gutter').forEach(g=>{
    g.addEventListener('mousedown', e=>{
      drag={which:g.dataset.gutter, x:e.clientX,
            rail:rail.getBoundingClientRect().width, art:art.getBoundingClientRect().width};
      g.classList.add('drag'); document.body.style.userSelect='none'; e.preventDefault();
    });
  });
  window.addEventListener('mousemove', e=>{
    if(!drag) return;
    const dx=e.clientX-drag.x;
    if(drag.which==='rail'){
      const w=Math.max(150,Math.min(480,drag.rail+dx));
      rail.style.width=w+'px'; localStorage.setItem('w_rail',Math.round(w));
    } else {
      const w=Math.max(280,Math.min(layout.clientWidth-360,drag.art-dx));
      art.style.flexBasis=w+'px'; localStorage.setItem('w_art',Math.round(w));
    }
  });
  window.addEventListener('mouseup', ()=>{
    if(!drag) return;
    document.querySelectorAll('.gutter').forEach(g=>g.classList.remove('drag'));
    document.body.style.userSelect=''; drag=null;
  });
})();
const statusEl=document.getElementById('status');
const es=new EventSource('/events');
es.onopen=()=>statusEl.textContent='live';
es.onerror=()=>statusEl.textContent='reconnecting…';
es.onmessage=e=>render(JSON.parse(e.data));
fetch('/agents').then(r=>r.json()).then(render);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
        elif self.path == "/agents":
            self._send(200, json.dumps(REGISTRY.snapshot()).encode())
        elif self.path == "/events":
            self._stream_events()
        else:
            self._send(404, b'{"error":"not found"}')

    def _stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = REGISTRY.subscribe()
        try:
            self.wfile.write(b"data: " + json.dumps(REGISTRY.snapshot()).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                payload = q.get()
                self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            REGISTRY.unsubscribe(q)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}

        if self.path == "/spawn":
            prompt = data.get("prompt", "").strip()
            if not prompt:
                self._send(400, b'{"error":"missing prompt"}')
                return
            agent = spawn_agent(
                name=data.get("name", "agent"),
                prompt=prompt,
                cwd=data.get("cwd", "."),
                model=data.get("model", "opus"),
                gate=data.get("gate", "readonly"),
                allowed_tools=data.get("allowed_tools"),
            )
            self._send(200, json.dumps({"id": agent.id, "name": agent.name}).encode())
        elif self.path == "/approve":
            res = approve_commit(data.get("id", ""), data.get("commit_msg"))
            self._send(200 if res.get("ok") else 400, json.dumps(res).encode())
        elif self.path == "/reject":
            res = reject_diff(data.get("id", ""))
            self._send(200 if res.get("ok") else 400, json.dumps(res).encode())
        elif self.path == "/stop":
            res = stop_agent(data.get("id", ""))
            self._send(200 if res.get("ok") else 400, json.dumps(res).encode())
        elif self.path == "/rerun":
            res = rerun_with_hint(data.get("id", ""), data.get("hint", ""))
            self._send(200 if res.get("ok") else 400, json.dumps(res).encode())
        elif self.path == "/chat":
            res = chat_with_agent(data.get("id", ""), data.get("message", ""))
            self._send(200 if res.get("ok") else 400, json.dumps(res).encode())
        elif self.path == "/approve-file":
            res = approve_file(data.get("id", ""), data.get("path", ""), data.get("commit_msg"))
            self._send(200 if res.get("ok") else 400, json.dumps(res).encode())
        elif self.path == "/discard-file":
            res = discard_file(data.get("id", ""), data.get("path", ""))
            self._send(200 if res.get("ok") else 400, json.dumps(res).encode())
        elif self.path == "/post-comment":
            res = post_draft_comment(data.get("id", ""), int(data.get("idx", -1)))
            self._send(200 if res.get("ok") else 400, json.dumps(res).encode())
        elif self.path == "/discard-comment":
            res = discard_draft_comment(data.get("id", ""), int(data.get("idx", -1)))
            self._send(200 if res.get("ok") else 400, json.dumps(res).encode())
        else:
            self._send(404, b'{"error":"not found"}')


def serve(host: str = HOST, port: int = PORT) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[monitor] dashboard at http://{host}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[monitor] stopped.")
