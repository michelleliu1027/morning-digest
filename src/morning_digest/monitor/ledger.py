"""A small completion ledger so the digest stops resurfacing handled tasks.

The digest is otherwise stateless: every run regenerates tasks from Notion +
GitHub + Slack with no memory of what the user already acted on. This ledger is
that memory — an append-only JSONL of terminal task outcomes that the next run
loads and injects into the prompt as a "you already handled these" list, so the
model deduplicates naturally.

Three terminal states, each with its own suppression window:

  - "done"     : an agent ran the task and committed/posted the result.
  - "finished" : the user did it themselves BEFORE the agent gave feedback
                 (the "✔️ already did this" button) — same intent as done.
  - "skipped"  : the user dismissed it for now.

Suppression rule (decided with the user):
  done / finished → suppress for DONE_WINDOW_DAYS (the source rarely re-clears
                    on its own, e.g. a Notion task or an answered DM).
  skipped         → suppress only for the rest of TODAY ("no time today" is not
                    "never do it"); it returns tomorrow if its source persists.

Keying: prefer source_url (a PR URL / Slack permalink / Notion page — stable
across runs). Fall back to a normalized title. The newest record per key wins,
so a task skipped yesterday then done today reads as done.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

_STATE_DIR = Path(__file__).resolve().parents[3] / "digest_output"
_LEDGER_FILE = _STATE_DIR / ".done_ledger.jsonl"

DONE_WINDOW_DAYS = 7
_TERMINAL_STATES = {"done", "finished", "skipped"}


def _normalize_title(title: str) -> str:
    """Lowercased, whitespace/punctuation-squashed title for fallback keying."""
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _key(source_url: str, title: str) -> str:
    """Stable dedup key: the source URL if present, else the normalized title."""
    su = (source_url or "").strip()
    return su if su else "title:" + _normalize_title(title)


def record(state: str, title: str, source_url: str = "", gate: str = "") -> None:
    """Append one terminal outcome. No-op for non-terminal/unknown states."""
    if state not in _TERMINAL_STATES:
        return
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "date": date.today().isoformat(),
        "state": state,
        "title": title or "",
        "source_url": source_url or "",
        "gate": gate or "",
        "key": _key(source_url, title),
    }
    with _LEDGER_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_all() -> list[dict]:
    try:
        text = _LEDGER_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _is_active(entry: dict, today: date) -> bool:
    """Is this record still within its suppression window as of `today`?"""
    try:
        d = date.fromisoformat(entry.get("date", ""))
    except ValueError:
        return False
    state = entry.get("state")
    if state == "skipped":
        return d == today  # only suppress for the rest of today
    if state in ("done", "finished"):
        return (today - d) < timedelta(days=DONE_WINDOW_DAYS)
    return False


def handled_recently(today: date | None = None) -> list[dict]:
    """The currently-suppressed items: newest record per key, still in-window.

    Returns dicts with keys: title, source_url, state, date — most recent first.
    """
    today = today or date.today()
    # Newest record per key wins (a later 'done' overrides an earlier 'skipped').
    latest: dict[str, dict] = {}
    for e in _read_all():
        k = e.get("key") or _key(e.get("source_url", ""), e.get("title", ""))
        prev = latest.get(k)
        if prev is None or e.get("ts", "") >= prev.get("ts", ""):
            latest[k] = e
    active = [e for e in latest.values() if _is_active(e, today)]
    active.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return active


def format_for_prompt(today: date | None = None) -> str:
    """Render the suppressed items as a prompt block, or '' if none.

    Injected into the digest prompt so the model skips already-handled work.
    """
    items = handled_recently(today)
    if not items:
        return ""
    lines = []
    for e in items:
        verb = {"done": "done by an agent", "finished": "done by you",
                "skipped": "skipped today"}.get(e.get("state", ""), e.get("state", ""))
        label = e.get("title") or e.get("source_url") or "(untitled)"
        link = f" — {e['source_url']}" if e.get("source_url") else ""
        lines.append(f"- [{verb}, {e.get('date','')}] {label}{link}")
    return (
        "\n## Already handled — DO NOT resurface these\n\n"
        "The user has already acted on the following tasks. Do NOT list any of them "
        "again as an actionable task UNLESS the situation has materially changed "
        "(e.g. a PR you marked done now has NEW review comments). It is fine to "
        "mention one briefly in the digest body for continuity, but it must not "
        "appear in the <<<TASKS>>> list.\n\n" + "\n".join(lines) + "\n"
    )
