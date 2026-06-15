"""Git orchestration for the code-task approval gate.

The writer agent makes edits in the working tree but never commits. When it
finishes, the server captures the diff (`capture_diff`) and parks the agent in
an ``awaiting_approval`` state. Only after the user clicks *commit* in the
dashboard does the server run `commit_and_push` — so nothing reaches the repo
without a human reading the diff first. Discarding stashes the work (never a
destructive `checkout --`) so it is always recoverable.

All git/gh calls are deterministic shell-outs; no LLM is involved once the diff
exists. The writer agent's only job is to pick the right branch and edit files.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


def _run(args: list[str], cwd: str) -> tuple[int, str]:
    """Run a command, return (exit_code, combined stdout+stderr)."""
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def current_branch(cwd: str) -> str:
    _, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return out.strip()


def has_changes(cwd: str) -> bool:
    _, out = _run(["git", "status", "--porcelain"], cwd)
    return bool(out.strip())


@dataclass
class DiffSnapshot:
    branch: str
    diff: str          # full unified diff of working tree (tracked + staged)
    files: list[str]   # changed file paths
    existing_pr: int | None  # PR number whose head is this branch, if any


def capture_diff(cwd: str) -> DiffSnapshot:
    """Snapshot the uncommitted change so the dashboard can show it for approval."""
    branch = current_branch(cwd)
    # Include untracked files in the diff by intent-to-add (doesn't stage content).
    _run(["git", "add", "-AN"], cwd)
    _, diff = _run(["git", "diff"], cwd)
    _, names = _run(["git", "diff", "--name-only"], cwd)
    files = [f for f in names.splitlines() if f.strip()]
    return DiffSnapshot(branch=branch, diff=diff, files=files,
                        existing_pr=_pr_for_branch(branch, cwd))


def _pr_for_branch(branch: str, cwd: str) -> int | None:
    code, out = _run(
        ["gh", "pr", "list", "--head", branch, "--state", "open",
         "--json", "number", "--jq", ".[0].number"],
        cwd,
    )
    if code == 0 and out.strip().isdigit():
        return int(out.strip())
    return None


@dataclass
class CommitResult:
    ok: bool
    message: str          # human-readable outcome for Slack/dashboard
    pr_url: str | None = None


def commit_and_push(cwd: str, branch: str, commit_msg: str,
                    existing_pr: int | None) -> CommitResult:
    """Stage everything, commit, push. Opens a Draft PR only for new branches."""
    code, out = _run(["git", "add", "-A"], cwd)
    if code != 0:
        return CommitResult(False, f"git add failed: {out}")

    code, out = _run(["git", "commit", "-m", commit_msg], cwd)
    if code != 0:
        return CommitResult(False, f"git commit failed: {out}")

    code, out = _run(["git", "push", "-u", "origin", branch], cwd)
    if code != 0:
        return CommitResult(False, f"git push failed: {out}")

    if existing_pr is not None:
        return CommitResult(True, f"committed → pushed to #{existing_pr}'s branch `{branch}` (PR auto-updates).")

    # New branch with no open PR → open a Draft PR (never merge).
    code, url = _run(
        ["gh", "pr", "create", "--draft", "--fill", "--head", branch],
        cwd,
    )
    if code != 0:
        return CommitResult(True, f"committed + pushed `{branch}` (Draft PR open failed: {url})")
    return CommitResult(True, f"committed + pushed `{branch}` → opened Draft PR: {url.strip()}", pr_url=url.strip())


def discard(cwd: str, label: str) -> CommitResult:
    """Stash the work (recoverable) rather than destroying it."""
    code, out = _run(["git", "stash", "push", "-u", "-m", label], cwd)
    if code != 0:
        return CommitResult(False, f"git stash failed: {out}")
    return CommitResult(True, f"discarded — stashed as `{label}` (recover with `git stash list`).")


def pr_url(cwd: str, pr: int) -> str | None:
    """Resolve the web URL for a PR number, for linking in Slack/dashboard."""
    code, out = _run(["gh", "pr", "view", str(pr), "--json", "url", "--jq", ".url"], cwd)
    if code == 0 and out.strip().startswith("http"):
        return out.strip()
    return None


def post_pr_comment(cwd: str, pr: int, body: str) -> CommitResult:
    """Post ONE approved comment to a PR via `gh pr comment` (never auto-called).

    This is the only place the bot writes to a PR, and it runs solely after the
    user approves that specific draft in the dashboard — the draft agent itself
    has no comment/review tools at all.
    """
    code, out = _run(["gh", "pr", "comment", str(pr), "--body", body], cwd)
    if code != 0:
        return CommitResult(False, f"posting comment to #{pr} failed: {out}")
    url = out.strip() if out.strip().startswith("http") else None
    return CommitResult(True, f"posted comment to #{pr}.", pr_url=url)
