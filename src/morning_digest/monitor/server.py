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
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .events import ProgressEvent, parse_event
from .gitops import capture_diff, commit_and_push, discard

HOST = "127.0.0.1"
PORT = 8787

# Tools a read-only / analysis agent may use without prompting.
DEFAULT_ALLOWED_TOOLS = "Bash(gh *) Bash(ls *) Bash(find *) mcp__notion__notion-search mcp__notion__notion-fetch"

# Tools the code WRITER may use. Deliberately EXCLUDES `git commit` / `git push`:
# the agent edits files and picks the right branch, but committing only happens
# after the user approves the diff in the dashboard. The gate is enforced here at
# the tool level, not merely by the prompt.
WRITER_ALLOWED_TOOLS = (
    "Edit Write Read Grep "
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
    # starting | running | done | failed | awaiting_approval | committing
    status: str = "starting"
    gate: str = "readonly"            # readonly | draft | code
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    events: list[dict] = field(default_factory=list)  # serialized ProgressEvents
    proc: subprocess.Popen | None = None
    diff: dict | None = None          # DiffSnapshot (branch/diff/files/existing_pr) when awaiting approval

    def latest(self) -> dict | None:
        return self.events[-1] if self.events else None

    def final_output(self) -> str:
        """The agent's last substantial 'say' — the draft / analysis to read in full."""
        for ev in reversed(self.events):
            if ev.get("kind") == "say" and ev.get("full"):
                return ev["full"]
        return ""

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
            "diff": self.diff,
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
            if status in ("done", "failed"):
                agent.ended_at = time.time()
        self._broadcast()

    def set_diff(self, agent: Agent, diff: dict | None) -> None:
        with self._lock:
            agent.diff = diff
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


def spawn_agent(name: str, prompt: str, cwd: str = ".", model: str = "opus",
                gate: str = "readonly", allowed_tools: str | None = None) -> Agent:
    """Start a local `claude -p` run and stream its progress into the registry.

    For ``gate="code"`` the agent gets the WRITER toolset (no commit/push) and,
    on a clean exit, the server captures the working-tree diff and parks the
    agent in ``awaiting_approval`` — committing waits for the user's click.
    """
    if allowed_tools is None:
        allowed_tools = WRITER_ALLOWED_TOOLS if gate == "code" else DEFAULT_ALLOWED_TOOLS
    agent = Agent(id=uuid.uuid4().hex[:8], name=name, prompt=prompt,
                  cwd=str(Path(cwd).expanduser()), gate=gate)
    REGISTRY.add(agent)

    def run() -> None:
        cmd = [
            "claude", "-p", prompt,
            "--model", model,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits",
            "--allowedTools", allowed_tools,
        ]
        try:
            proc = subprocess.Popen(
                cmd, cwd=agent.cwd, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except FileNotFoundError:
            REGISTRY.push_event(agent, ProgressEvent("done", "claude CLI not found on PATH", "❌"))
            REGISTRY.set_status(agent, "failed")
            return

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
                REGISTRY.push_event(agent, ev)
        rc = proc.wait()
        if rc != 0:
            REGISTRY.set_status(agent, "failed")
            return
        if agent.gate == "code":
            _park_for_approval(agent)
        else:
            REGISTRY.set_status(agent, "done")

    threading.Thread(target=run, daemon=True).start()
    return agent


def _park_for_approval(agent: Agent) -> None:
    """Capture the uncommitted diff and wait for the user to approve committing."""
    try:
        snap = capture_diff(agent.cwd)
    except Exception as exc:  # noqa: BLE001 - surface any git failure to the UI
        REGISTRY.push_event(agent, ProgressEvent("done", f"diff capture failed: {exc}", "❌"))
        REGISTRY.set_status(agent, "failed")
        return

    if not snap.diff.strip():
        REGISTRY.push_event(agent, ProgressEvent("done", "no code changes were made", "ℹ️"))
        REGISTRY.set_status(agent, "done")
        return

    target = (f"existing PR #{snap.existing_pr} (branch `{snap.branch}`)"
              if snap.existing_pr else f"new branch `{snap.branch}` → would open a Draft PR")
    REGISTRY.set_diff(agent, {
        "branch": snap.branch,
        "diff": snap.diff,
        "files": snap.files,
        "existing_pr": snap.existing_pr,
        "target": target,
    })
    REGISTRY.push_event(agent, ProgressEvent(
        "say", f"✋ change ready for your review → {target}. {len(snap.files)} file(s).",
        "✋", full=f"Target: {target}\nFiles:\n" + "\n".join(snap.files)))
    REGISTRY.set_status(agent, "awaiting_approval")


def approve_commit(agent_id: str, commit_msg: str | None = None) -> dict:
    """User approved the diff → commit + push (existing branch) or open Draft PR (new)."""
    agent = REGISTRY.get(agent_id)
    if agent is None:
        return {"ok": False, "error": "agent not found"}
    if agent.status != "awaiting_approval" or not agent.diff:
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


def reject_diff(agent_id: str) -> dict:
    """User rejected the diff → stash the work (recoverable) and mark done."""
    agent = REGISTRY.get(agent_id)
    if agent is None:
        return {"ok": False, "error": "agent not found"}
    if agent.status != "awaiting_approval":
        return {"ok": False, "error": f"agent is {agent.status}"}
    res = discard(agent.cwd, label=f"morning-digest:{agent.name}")
    REGISTRY.push_event(agent, ProgressEvent("done", res.message, "🗑️", full=res.message))
    REGISTRY.set_diff(agent, None)
    REGISTRY.set_status(agent, "done")
    return {"ok": res.ok, "message": res.message}


# ---------------------------------------------------------------------------
# HTTP layer: dashboard page + JSON API + SSE stream
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Morning Digest · Agents</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 0; background:#0f1115; color:#e6e6e6; }
  header { padding: 14px 18px; background:#161922; border-bottom:1px solid #262b36; position:sticky; top:0; }
  header h1 { font-size:15px; margin:0; font-weight:600; }
  header small { color:#8b93a7; }
  .wrap { display:flex; gap:14px; padding:14px; flex-wrap:wrap; }
  .card { background:#161922; border:1px solid #262b36; border-radius:10px; width:min(420px,100%); overflow:hidden; }
  .card h2 { font-size:14px; margin:0; padding:10px 12px; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #262b36; }
  .badge { font-size:11px; padding:2px 8px; border-radius:99px; font-weight:600; }
  .running { background:#1d3b2a; color:#5fe39a; } .starting{ background:#3a341d; color:#e3cc5f;}
  .done { background:#1d2a3b; color:#5fa8e3; } .failed{ background:#3b1d1d; color:#e35f5f;}
  .awaiting_approval { background:#3a2a14; color:#f0a85f; } .committing{ background:#2a2a3b; color:#9f9fe3;}
  .prompt { padding:8px 12px; color:#8b93a7; font-size:12px; border-bottom:1px solid #262b36; }
  .feed { max-height:340px; overflow:auto; padding:6px 0; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
  .row { padding:3px 12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .row.has-full { cursor:pointer; }
  .row.has-full:hover { background:#1c2129; }
  .row.open { white-space:pre-wrap; word-break:break-word; background:#0c0e12; border-left:2px solid #3a6; padding:8px 12px; }
  .row .t { color:#5b6478; margin-right:8px; }
  .row .caret { color:#5b6478; margin-right:4px; font-size:10px; }
  .row.tool { color:#9fd0ff; } .row.say{ color:#e6e6e6;} .row.result{ color:#8b93a7;} .row.done{ color:#5fe39a; font-weight:600;}
  .output { border-top:1px solid #262b36; }
  .output h3 { font-size:12px; margin:0; padding:8px 12px; color:#5fe39a; display:flex; justify-content:space-between; cursor:pointer; }
  .output pre { margin:0; padding:0 12px 12px; white-space:pre-wrap; word-break:break-word; font-size:12px; color:#d7dbe3; max-height:420px; overflow:auto; }
  .empty { color:#5b6478; padding:30px; text-align:center; }
  .gate { border-top:1px solid #262b36; background:#1a160f; }
  .gate .target { padding:8px 12px; color:#f0a85f; font-size:12px; }
  .gate pre.diff { margin:0; padding:0 12px 10px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11.5px; line-height:1.45; white-space:pre; overflow:auto; max-height:460px; }
  .gate pre.diff .add { color:#5fe39a; } .gate pre.diff .del{ color:#e38f8f;} .gate pre.diff .hdr{ color:#9fd0ff;} .gate pre.diff .at{ color:#c08fe3;}
  .gate .acts { display:flex; gap:8px; padding:10px 12px; border-top:1px solid #262b36; }
  .gate button { font:13px system-ui; padding:7px 14px; border-radius:7px; border:0; cursor:pointer; font-weight:600; }
  .gate button.ok { background:#1d6b3a; color:#cfffe0; } .gate button.ok:hover{ background:#23824a;}
  .gate button.no { background:#3b1d1d; color:#ffd5d5; } .gate button.no:hover{ background:#5a2a2a;}
  .gate button:disabled { opacity:.5; cursor:default; }
</style></head>
<body>
<header><h1>Morning Digest · Agents <small id="status">connecting…</small></h1></header>
<div class="wrap" id="wrap"><div class="empty">No agents yet. Spawn one to watch it work here.</div></div>
<script>
const wrap = document.getElementById('wrap');
const statusEl = document.getElementById('status');
const openRows = new Set();   // remember which rows the user expanded (by agent+index)
const openOut = new Set();     // remember which output panels are open
function render(agents){
  if(!agents.length){ wrap.innerHTML='<div class="empty">No agents yet. Spawn one to watch it work here.</div>'; return; }
  wrap.innerHTML = agents.map(a => {
    const rows = a.events.map((e,i)=>{
      const key = a.id+':'+i;
      const hasFull = e.full && e.full.length > (e.text||'').length;
      const open = openRows.has(key);
      const cls = `row ${e.kind}${hasFull?' has-full':''}${open?' open':''}`;
      const body = open ? esc(e.full) : esc(e.text);
      const caret = hasFull ? `<span class="caret">${open?'▼':'▶'}</span>` : '';
      return `<div class="${cls}" data-key="${key}"><span class="t">${e.t}s</span>${caret}${e.icon||''} ${body}</div>`;
    }).join('') || '<div class="row">…</div>';
    let out = '';
    if(a.final_output){
      const oOpen = openOut.has(a.id) || a.status!=='running';
      out = `<div class="output"><h3 data-out="${a.id}">📄 Output / Draft (click to expand)<span>${oOpen?'▼':'▶'}</span></h3>${oOpen?`<pre>${esc(a.final_output)}</pre>`:''}</div>`;
    }
    let gate = '';
    if(a.diff && a.status==='awaiting_approval'){
      gate = `<div class="gate">
        <div class="target">✋ Awaiting your approval → ${esc(a.diff.target)}</div>
        <pre class="diff">${colorDiff(a.diff.diff)}</pre>
        <div class="acts">
          <button class="ok" data-approve="${a.id}">✅ Commit this change</button>
          <button class="no" data-reject="${a.id}">🗑️ Discard</button>
        </div></div>`;
    } else if(a.status==='committing'){
      gate = `<div class="gate"><div class="target">⏳ committing…</div></div>`;
    }
    return `<div class="card">
      <h2>${esc(a.name)} <span class="badge ${a.status}">${a.status} · ${a.elapsed}s</span></h2>
      <div class="prompt">${esc(a.prompt)}</div>
      <div class="feed">${rows}</div>${out}${gate}
    </div>`;
  }).join('');
}
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
wrap.addEventListener('click', ev => {
  const row = ev.target.closest('.row.has-full');
  if(row){ const k=row.dataset.key; openRows.has(k)?openRows.delete(k):openRows.add(k); render(LAST); return; }
  const out = ev.target.closest('[data-out]');
  if(out){ const id=out.dataset.out; openOut.has(id)?openOut.delete(id):openOut.add(id); render(LAST); return; }
  const ap = ev.target.closest('[data-approve]');
  if(ap){ ap.disabled=true; ap.textContent='committing…'; post('/approve', {id:ap.dataset.approve}); return; }
  const rj = ev.target.closest('[data-reject]');
  if(rj){ rj.disabled=true; post('/reject', {id:rj.dataset.reject}); }
});
function post(path, body){
  fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(r=>r.json()).then(res=>{ if(!res.ok) alert(res.error||res.message||'failed'); });
}
let LAST = [];
const es = new EventSource('/events');
es.onopen = ()=> statusEl.textContent='live';
es.onerror = ()=> statusEl.textContent='reconnecting…';
es.onmessage = e => { LAST = JSON.parse(e.data); render(LAST); };
fetch('/agents').then(r=>r.json()).then(d=>{ LAST=d; render(d); });
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
        else:
            self._send(404, b'{"error":"not found"}')


def serve(host: str = HOST, port: int = PORT) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[monitor] dashboard at http://{host}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[monitor] stopped.")
