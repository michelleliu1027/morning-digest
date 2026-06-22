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

import re
import subprocess
from dataclasses import dataclass


def _run(args: list[str], cwd: str) -> tuple[int, str]:
    """Run a command, return (exit_code, combined stdout+stderr)."""
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _nothing_to_commit(out: str) -> bool:
    """True when `git commit` failed only because there was nothing to commit.

    This happens when the change is already on the branch (e.g. a prior approval
    committed it, or the agent's run committed it). It is NOT an error — the work
    is already in place — so callers treat it as "already done" instead of looping
    the dashboard forever on a non-zero exit.
    """
    low = out.lower()
    return ("nothing to commit" in low
            or "nothing added to commit" in low      # scoped commit, other untracked files present
            or "no changes added to commit" in low
            or "working tree clean" in low)


def _push(cwd: str, branch: str) -> tuple[int, str]:
    """Push the branch, using gh as the credential helper so it works headless.

    The server runs under launchd with no TTY/keychain, so a plain HTTPS push
    fails with "could not read Username for 'https://github.com'". Injecting
    `gh auth git-credential` as the helper (only for this invocation, via -c)
    supplies the token gh already holds, without touching the repo's git config.
    """
    return _run(
        ["git",
         "-c", "credential.https://github.com.helper=",
         "-c", "credential.https://github.com.helper=!gh auth git-credential",
         "push", "-u", "origin", branch],
        cwd,
    )


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
    # Already committed (a prior approval or the agent's run) → not an error; fall
    # through to push so the existing commit still reaches the PR branch.
    if code != 0 and not _nothing_to_commit(out):
        return CommitResult(False, f"git commit failed: {out}")

    code, out = _push(cwd, branch)
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


def commit_files(cwd: str, branch: str, files: list[str], commit_msg: str,
                 existing_pr: int | None) -> CommitResult:
    """Stage ONLY the given files, commit, push. For per-file approval.

    Same push/Draft-PR behaviour as commit_and_push, but scoped: other changed
    files in the working tree are left untouched so the user can approve them
    (or discard them) separately.
    """
    if not files:
        return CommitResult(False, "no files to commit")
    code, out = _run(["git", "add", "--", *files], cwd)
    if code != 0:
        return CommitResult(False, f"git add failed: {out}")

    code, out = _run(["git", "commit", "-m", commit_msg, "--", *files], cwd)
    # This file's change may already be committed (re-click, or the agent's run
    # committed it). Treat that as already-done and still push, rather than
    # bouncing the file back to "pending" and stranding the dashboard.
    already = code != 0 and _nothing_to_commit(out)
    if code != 0 and not already:
        return CommitResult(False, f"git commit failed: {out}")

    code, out = _push(cwd, branch)
    if code != 0:
        return CommitResult(False, f"git push failed: {out}")

    nfiles = len(files)
    if already:
        scope = f"#{existing_pr}'s branch `{branch}`" if existing_pr else f"`{branch}`"
        return CommitResult(True, f"{nfiles} file(s) already committed → ensured pushed to {scope}.")
    if existing_pr is not None:
        return CommitResult(True, f"committed {nfiles} file(s) → pushed to #{existing_pr}'s branch `{branch}` (PR auto-updates).")

    code, url = _run(["gh", "pr", "create", "--draft", "--fill", "--head", branch], cwd)
    if code != 0:
        return CommitResult(True, f"committed {nfiles} file(s) + pushed `{branch}` (Draft PR open failed: {url})")
    return CommitResult(True, f"committed {nfiles} file(s) + pushed `{branch}` → opened Draft PR: {url.strip()}", pr_url=url.strip())


def discard(cwd: str, label: str) -> CommitResult:
    """Stash the work (recoverable) rather than destroying it."""
    code, out = _run(["git", "stash", "push", "-u", "-m", label], cwd)
    if code != 0:
        return CommitResult(False, f"git stash failed: {out}")
    return CommitResult(True, f"discarded — stashed as `{label}` (recover with `git stash list`).")


def discard_files(cwd: str, files: list[str], label: str) -> CommitResult:
    """Stash ONLY the given files (recoverable), leaving the rest of the tree."""
    if not files:
        return CommitResult(False, "no files to discard")
    code, out = _run(["git", "stash", "push", "-u", "-m", label, "--", *files], cwd)
    if code != 0:
        return CommitResult(False, f"git stash failed: {out}")
    return CommitResult(True, f"discarded {len(files)} file(s) — stashed as `{label}` (recover with `git stash list`).")


def split_diff_by_file(diff: str) -> dict[str, str]:
    """Split a unified diff into {path: that-file's-diff-text}.

    Splits on `diff --git` headers; the path is taken from the b/ side so renames
    and new files map to their destination. Used to show + approve one file's
    change at a time in the dashboard.
    """
    out: dict[str, str] = {}
    if not diff.strip():
        return out
    chunks = re.split(r"(?m)^(?=diff --git )", diff)
    for ch in chunks:
        if not ch.startswith("diff --git"):
            continue
        m = re.search(r"^\+\+\+ b/(.+)$", ch, re.MULTILINE)
        if not m:
            m = re.search(r"diff --git a/.+ b/(.+)$", ch.splitlines()[0])
        path = m.group(1).strip() if m else "(unknown)"
        out[path] = ch.rstrip("\n")
    return out


def pr_url(cwd: str, pr: int) -> str | None:
    """Resolve the web URL for a PR number, for linking in Slack/dashboard."""
    code, out = _run(["gh", "pr", "view", str(pr), "--json", "url", "--jq", ".url"], cwd)
    if code == 0 and out.strip().startswith("http"):
        return out.strip()
    return None


# Bot-comment chrome to strip so the feedback is readable (and cheap in tokens).
# Cursor Bugbot wraps each finding in HTML "Fix in Cursor/Web" buttons (base64
# deep-links), HTML comment markers, a <details> "Additional Locations" block,
# and a <sup>Reviewed by…</sup> footer. None of it is the actual finding.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<(div|a|picture|source|img|sup|details|summary)\b[^>]*>.*?</\1>",
                          re.DOTALL | re.IGNORECASE)
_SELF_CLOSING_RE = re.compile(r"<(img|source|br)\b[^>]*/?>", re.IGNORECASE)
_STRAY_TAG_RE = re.compile(r"</?(div|a|picture|source|sup|details|summary)\b[^>]*>", re.IGNORECASE)
_BLANK_RUN_RE = re.compile(r"\n[ \t]*\n([ \t]*\n)+")


def _clean_comment_body(body: str) -> str:
    """Strip bot HTML chrome from a review comment, keeping the human-readable text."""
    if not body:
        return ""
    out = _HTML_COMMENT_RE.sub("", body)        # <!-- DESCRIPTION/LOCATIONS/BUGBOT_ID -->
    out = _HTML_TAG_RE.sub("", out)             # <div>fix-buttons</div>, <details>…</details>, <sup>…</sup>
    out = _SELF_CLOSING_RE.sub("", out)
    out = _STRAY_TAG_RE.sub("", out)            # any unmatched stray open/close tags
    out = _BLANK_RUN_RE.sub("\n\n", out)        # collapse the blank lines those left behind
    return out.strip()


def fetch_pr_review_comments(cwd: str, pr: int) -> str:
    """Pull EVERY open review comment on a PR (inline bot comments + review bodies).

    Run server-side (deterministic gh, never the sandboxed agent) so a code task
    that names a PR sees the full set of cursor/CodeRabbit findings, not just the
    one issue the digest happened to mention. Read-only: only GET endpoints.

    Each comment body is run through `_clean_comment_body` to drop the bot HTML
    chrome (Cursor "Fix in Cursor" buttons, base64 deep-links, <!-- markers -->,
    <details>, <sup> footers) — that noise is unreadable in the dashboard and just
    burns tokens in the agent's prompt.
    """
    code, nwo = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], cwd)
    nwo = nwo.strip()
    if code != 0 or "/" not in nwo:
        return ""

    parts: list[str] = []
    # Inline review comments (the cursor[bot] / coderabbitai[bot] line comments).
    # Pull a tab-delimited (login, path, line, body) tuple per comment so we can
    # clean each body's HTML here in Python rather than in a brittle jq filter.
    code, out = _run(
        ["gh", "api", f"repos/{nwo}/pulls/{pr}/comments", "--paginate",
         "--jq", '.[] | [.user.login, .path, (.line // .original_line | tostring), '
                 '(.body | gsub("\\t"; " ") | gsub("\\n"; "\\u0001"))] | @tsv'],
        cwd)
    if code == 0 and out.strip():
        lines = []
        for raw in out.splitlines():
            cols = raw.split("\t")
            if len(cols) < 4:
                continue
            login, path, line, body = cols[0], cols[1], cols[2], "\t".join(cols[3:])
            body = _clean_comment_body(body.replace("\x01", "\n"))
            if not body:
                continue
            lines.append(f"[{login}] {path}:{line}\n{body}\n")
        if lines:
            parts.append("INLINE REVIEW COMMENTS:\n" + "\n".join(lines).strip())

    # Review summaries that carry a body (e.g. CodeRabbit's top-level review).
    code, out = _run(
        ["gh", "api", f"repos/{nwo}/pulls/{pr}/reviews", "--paginate",
         "--jq", '.[] | select(.body != "") | [.user.login, .state, '
                 '(.body | gsub("\\t"; " ") | gsub("\\n"; "\\u0001"))] | @tsv'],
        cwd)
    if code == 0 and out.strip():
        lines = []
        for raw in out.splitlines():
            cols = raw.split("\t")
            if len(cols) < 3:
                continue
            login, state, body = cols[0], cols[1], "\t".join(cols[2:])
            body = _clean_comment_body(body.replace("\x01", "\n"))
            if not body:
                continue
            lines.append(f"[{login}] ({state})\n{body}\n")
        if lines:
            parts.append("REVIEW SUMMARIES:\n" + "\n".join(lines).strip())

    return "\n\n".join(parts).strip()


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
