"""Actionable tasks the digest can offer as Slack buttons.

A Task is one thing an agent can do on the user's behalf. Each carries the
prompt to run, the repo to run it in, and a `gate` describing how its output is
approved (the v2 "Gate 2"):

  - "code"    : agent edits files but NEVER commits. It first judges whether the
                change belongs in an existing open PR/branch (add a commit) or a
                new branch, then stops. The dashboard shows the diff; the user
                clicks commit. Nothing reaches the repo before approval.
  - "draft"   : agent drafts a reply/review and posts it to Slack for approval;
                nothing is sent until the user okays it.
  - "readonly": analysis only, no side effects.

For now tasks are built from a simple list (hand-authored or, later, generated
from the digest). Keeping them as data lets the listener render buttons and the
monitor spawn agents without either knowing the other's internals.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

# Marker the digest prompt emits to separate the human-readable digest from the
# machine-readable task list (see prompt._ACTIONS_BLOCK).
TASKS_MARKER = "<<<TASKS>>>"

# Prepended to every "code" task. Tells the writer to pick the right branch and
# STOP before committing — the diff-preview → approve → commit gate lives in the
# server (the writer toolset has no commit/push), so this just steers placement.
CODE_PREAMBLE = (
    "You are making a code change that a human will review as a diff before it is "
    "committed. Do NOT commit, push, or open a PR — you do not have those tools, and "
    "committing happens only after the human approves your diff.\n\n"
    "STEP 1 — get on the RIGHT branch (do not assume the current branch is correct):\n"
    "  - If the task names a specific PR (e.g. \"#1098\"), you MUST run "
    "`gh pr checkout <n>` to switch to that PR's real branch FIRST. Branch names can "
    "look similar but differ — never guess the branch from its name; let "
    "`gh pr checkout` resolve it. After checkout, run `git branch --show-current` and "
    "confirm it matches the PR's head branch from `gh pr view <n> --json headRefName`.\n"
    "  - If the task does NOT name a PR, run `gh pr list --author @me --state open`. If "
    "the change clearly extends one of those PRs, `gh pr checkout <n>` it. If it is "
    "genuinely new work, create a new branch with `git switch -c <descriptive-name>`.\n"
    "  - If you are UNSURE which PR/branch it belongs to, do NOT guess — make no edit, "
    "stop, and say clearly in your final message which options you considered and what "
    "you need the human to decide.\n\n"
    "STEP 2 — make the edit in the working tree, then STOP. Do not commit.\n\n"
    "CRITICAL: if you find the change already appears to exist, do NOT just declare the "
    "work done. First verify you are on the PR's actual branch (step 1); the file may "
    "exist on a different branch. If after checking out the correct branch the change "
    "truly is already present, say so explicitly and explain how you verified it — do "
    "not silently end with no diff.\n\n"
    "End with a short summary of what you changed and which branch/PR it targets.\n\n"
    "--- TASK ---\n"
)


@dataclass
class Task:
    id: str          # stable short id, used in the button's action value
    title: str       # one-line label shown next to the button
    prompt: str      # what the agent is told to do
    cwd: str = "."   # repo/dir to run in
    gate: str = "readonly"  # "code" | "draft" | "readonly"

    @property
    def gate_label(self) -> str:
        return {
            "code": "edits → you approve the diff → commit",
            "draft": "drafts for your approval",
            "readonly": "read-only analysis",
        }.get(self.gate, self.gate)

    @property
    def agent_prompt(self) -> str:
        """The prompt actually sent to the agent (code tasks get the placement preamble)."""
        return CODE_PREAMBLE + self.prompt if self.gate == "code" else self.prompt


_VALID_GATES = {"code", "draft", "readonly"}
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def split_digest_and_tasks(raw: str) -> tuple[str, list[Task]]:
    """Split the model output into (human digest text, parsed Task list).

    The digest is everything before TASKS_MARKER; after it is a JSON array of
    task specs. Malformed/missing JSON yields no tasks (the digest still sends).
    """
    if TASKS_MARKER not in raw:
        return raw.strip(), []
    digest, _, tail = raw.partition(TASKS_MARKER)
    m = _JSON_ARRAY_RE.search(tail)
    if not m:
        return digest.strip(), []
    try:
        specs = json.loads(m.group(0))
    except json.JSONDecodeError:
        return digest.strip(), []

    tasks: list[Task] = []
    for spec in specs if isinstance(specs, list) else []:
        if not isinstance(spec, dict) or not spec.get("title") or not spec.get("prompt"):
            continue
        gate = spec.get("gate", "readonly")
        tasks.append(Task(
            id=uuid.uuid4().hex[:8],
            title=str(spec["title"])[:80],
            prompt=str(spec["prompt"]),
            cwd=str(spec.get("cwd", ".")),
            gate=gate if gate in _VALID_GATES else "readonly",
        ))
    return digest.strip(), tasks
