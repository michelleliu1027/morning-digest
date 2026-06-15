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

# Prepended to EVERY task regardless of gate. The spawned `claude -p` also loads
# the global ~/.claude/CLAUDE.md (which carries this rule), but that is a soft
# hint the agent may skip if it misjudges a task as unrelated. Repeating it here,
# in the task prompt itself, makes consulting the personal knowledge base a hard
# step — the same pattern the code/draft preambles use to force a tool action.
BRAIN_PREAMBLE = (
    "BEFORE you start, consult the user's personal knowledge base (the "
    "\"claude-brain\" Obsidian vault at ~/claude-brain) — it is the source of "
    "truth for her prior investigations, data models, dashboards, and SOPs "
    "(especially marketing attribution, AppsFlyer, ad-spend pipelines, dbt):\n"
    "  1. `grep` ~/claude-brain/INDEX.md for keywords from this task.\n"
    "  2. Read each matching note under ~/claude-brain/notes/ or ~/claude-brain/refs/.\n"
    "  3. Follow its [[wikilinks]] to related notes for fuller context.\n"
    "If a search turns up nothing relevant, say so briefly and proceed — but you "
    "must actually look first rather than assume. This vault is private and "
    "local-only: never copy its contents into a PR, commit, or any shared place.\n\n"
)

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
    "STEP 2 — if the prompt includes an \"ALL OPEN REVIEW FEEDBACK ON PR\" section, "
    "treat EVERY distinct issue in it as in scope. Make the edits for each one in the "
    "working tree, then STOP. Do not commit. If you deliberately skip an issue, say so "
    "and why in your summary — do not silently address only the first.\n\n"
    "CRITICAL: if you find the change already appears to exist, do NOT just declare the "
    "work done. First verify you are on the PR's actual branch (step 1); the file may "
    "exist on a different branch. If after checking out the correct branch the change "
    "truly is already present, say so explicitly and explain how you verified it — do "
    "not silently end with no diff.\n\n"
    "In your final summary, FIRST emit a block delimited by `<<<SOURCE>>>` and "
    "`<<<END SOURCE>>>` containing the original request this change responds to, "
    "quoted as closely as you can — the exact Bugbot comment, PR review note, or "
    "failing-check message. This is shown next to the diff so the human can see WHY "
    "the change was needed. If there is no external source, put the task's own goal "
    "there.\n\n"
    "THEN emit a block delimited by `<<<ISSUES>>>` and `<<<END ISSUES>>>` containing "
    "a JSON array that maps each distinct issue you handled to the files you changed "
    "for it. This is what links a reviewer comment to its diff in the dashboard, so "
    "be precise. Each object:\n"
    "  - \"title\": the issue in <=8 words (e.g. \"join_key NULL collapse\")\n"
    "  - \"severity\": \"High\" | \"Medium\" | \"Low\" | \"\" (copy the reviewer's label if any)\n"
    "  - \"did\": one short line on the fix you made (e.g. \"coalesce campaign_name fallback\")\n"
    "  - \"files\": array of the exact repo-relative paths you edited for THIS issue "
    "(must match the diff's paths). If you addressed an issue WITHOUT editing (already "
    "fixed, or you skipped it), use an empty array and explain in \"did\".\n"
    "Output one object per distinct reviewer issue. If there was no external feedback "
    "(plain task), output a single object describing the task. Example:\n"
    "  [{\"title\":\"join_key NULL collapse\",\"severity\":\"High\","
    "\"did\":\"added coalesce(campaign_name, media_source||'__unknown') fallback\","
    "\"files\":[\"analytics/models/.../campaign_cac_daily.sql\"]}]\n\n"
    "Then continue with your normal summary.\n\n"
    "End with a short summary of what you changed and which branch/PR it targets.\n\n"
    "--- TASK ---\n"
)


# Prepended to every "draft" task. The agent reads the PR and writes draft
# comments but CANNOT post them (it has no comment/review tools). It must emit a
# <<<COMMENTS>>> JSON block the dashboard turns into per-item approve buttons.
DRAFT_PREAMBLE = (
    "You are drafting PR feedback for a human to review and approve. You do NOT "
    "have any tool to post comments, submit reviews, or merge — and you must not "
    "try. Read the PR (gh pr view / gh pr diff) and write your review.\n\n"
    "After your human-readable review, output a line containing only `<<<COMMENTS>>>` "
    "followed by a JSON array of the individual comments you propose posting. Each "
    "object: {\"pr\": <pr number as int>, \"body\": \"<the exact comment markdown>\"}. "
    "Split distinct points into separate comments so the human can approve them one "
    "at a time. If you have no comment worth posting, output `<<<COMMENTS>>>` then `[]`. "
    "Output nothing after the JSON array.\n\n"
    "--- TASK ---\n"
)


@dataclass
class Task:
    id: str          # stable short id, used in the button's action value
    title: str       # one-line label shown next to the button
    prompt: str      # what the agent is told to do
    cwd: str = "."   # repo/dir to run in
    gate: str = "readonly"  # "code" | "draft" | "readonly"
    source: str = ""        # one-line provenance ("CodeRabbit review on PR #1098")
    source_url: str = ""    # link to that source, if any

    @property
    def source_label(self) -> str:
        """A '📎 From: …' line for Slack/dashboard, linked if a URL is known."""
        if not self.source:
            return ""
        if self.source_url:
            return f"📎 From: <{self.source_url}|{self.source}>"
        return f"📎 From: {self.source}"

    @property
    def gate_label(self) -> str:
        return {
            "code": "edits → you approve the diff → commit",
            "draft": "drafts for your approval",
            "readonly": "read-only analysis",
        }.get(self.gate, self.gate)

    @property
    def agent_prompt(self) -> str:
        """The prompt actually sent to the agent.

        Every gate first gets BRAIN_PREAMBLE (consult ~/claude-brain), then its
        own gate preamble (code/draft), then the task itself.
        """
        if self.gate == "code":
            return BRAIN_PREAMBLE + CODE_PREAMBLE + self.prompt
        if self.gate == "draft":
            return BRAIN_PREAMBLE + DRAFT_PREAMBLE + self.prompt
        return BRAIN_PREAMBLE + self.prompt


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
            source=str(spec.get("source", ""))[:120],
            source_url=str(spec.get("source_url", "")),
        ))
    return digest.strip(), tasks
