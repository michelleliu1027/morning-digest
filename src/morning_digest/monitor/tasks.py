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

from dataclasses import dataclass

# Prepended to every "code" task. Tells the writer to pick the right branch and
# STOP before committing — the diff-preview → approve → commit gate lives in the
# server (the writer toolset has no commit/push), so this just steers placement.
CODE_PREAMBLE = (
    "You are making a code change that a human will review as a diff before it is "
    "committed. Do NOT commit, push, or open a PR — you do not have those tools, and "
    "committing happens only after the human approves your diff.\n\n"
    "FIRST decide where this change belongs:\n"
    "  - Run `gh pr list --author @me --state open` and read the task. If the change "
    "clearly extends an existing open PR (e.g. adding a test to a PR already in review), "
    "check out that PR's branch (`gh pr checkout <n>`) and make your edit there.\n"
    "  - If it is genuinely new work unrelated to any open PR, create a new branch "
    "(`git switch -c <descriptive-name>`) and edit there.\n"
    "  - If you are UNSURE which PR/branch it belongs to, do NOT guess — stop and say "
    "clearly in your final message which options you considered and what you need the "
    "human to decide.\n\n"
    "THEN make the edit in the working tree and STOP. End with a short summary of what "
    "you changed and which branch/PR it targets.\n\n"
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
