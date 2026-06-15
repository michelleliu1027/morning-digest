"""Parse `claude -p --output-format stream-json --verbose` into progress events.

Each line of that stream is one JSON object describing a step the agent took.
`parse_event` maps one such object to a small, UI-agnostic ProgressEvent (or
None for lines we don't surface). The web dashboard and the TUI both consume
these — they never touch the raw Claude JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProgressEvent:
    kind: str          # "start" | "say" | "tool" | "result" | "done"
    text: str          # human-readable one-liner (truncated, for the collapsed feed)
    icon: str = ""     # emoji hint for UIs that want one
    detail: str = ""   # extra payload (e.g. full command, tool name)
    full: str = ""     # UNtruncated text — what the UI shows when a row is expanded
    meta: dict = field(default_factory=dict)  # structured extras (cost, duration, model)


def _short(s: object, n: int = 120) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_detail(inp: dict) -> str:
    """Pick the most informative field from a tool's input for display."""
    for key in ("command", "file_path", "pattern", "query", "path", "url"):
        if inp.get(key):
            return str(inp[key])
    return ""


def parse_event(evt: dict) -> list[ProgressEvent]:
    """Map one stream-json object to zero or more ProgressEvents."""
    et = evt.get("type")

    if et == "system" and evt.get("subtype") == "init":
        model = evt.get("model", "?")
        n_tools = len(evt.get("tools", []))
        return [
            ProgressEvent(
                kind="start",
                icon="🟢",
                text=f"session started ({n_tools} tools available)",
                meta={"model": model, "tools": n_tools},
            )
        ]

    if et == "assistant":
        out: list[ProgressEvent] = []
        for block in evt.get("message", {}).get("content", []):
            bt = block.get("type")
            if bt == "text" and block.get("text", "").strip():
                txt = block["text"].strip()
                out.append(ProgressEvent(kind="say", icon="💬", text=_short(txt), full=txt))
            elif bt == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                if name == "TodoWrite":
                    todos = inp.get("todos", [])
                    done = sum(1 for t in todos if t.get("status") == "completed")
                    out.append(
                        ProgressEvent(
                            kind="todo",
                            icon="✓",
                            text=f"plan: {done}/{len(todos)} done",
                            meta={"todos": todos},
                        )
                    )
                    continue
                detail = _tool_detail(inp)
                out.append(
                    ProgressEvent(
                        kind="tool",
                        icon="🔧",
                        text=f"{name}({_short(detail, 70)})",
                        detail=detail,
                        meta={"tool": name},
                    )
                )
        return out

    if et == "user":  # tool results return as a user turn
        out = []
        for block in evt.get("message", {}).get("content", []):
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                is_err = block.get("is_error", False)
                out.append(
                    ProgressEvent(
                        kind="result",
                        icon="⚠️" if is_err else "↳",
                        text=_short(content, 90),
                        full=str(content),
                        meta={"is_error": is_err},
                    )
                )
        return out

    if et == "result":
        dur = evt.get("duration_ms", 0) / 1000
        cost = evt.get("total_cost_usd")
        is_err = evt.get("is_error", False) or evt.get("subtype") != "success"
        tail = f" · ${cost:.4f}" if cost is not None else ""
        return [
            ProgressEvent(
                kind="done",
                icon="❌" if is_err else "✅",
                text=("failed" if is_err else "done") + f" in {dur:.1f}s{tail}",
                meta={"duration_s": round(dur, 1), "cost_usd": cost, "is_error": is_err},
            )
        ]

    return []
