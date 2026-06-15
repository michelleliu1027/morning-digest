"""The prompt that drives Claude to gather sources and build the digest."""

import os

# Sources Claude gathers itself via tools (Notion MCP + gh CLI).
_SOURCES = """\
## Sources to gather

1. **Notion tasks** — use the Notion MCP tools (notion-search / notion-fetch) to \
find tasks assigned to or authored by the user that are open / in-progress / due \
soon. Prefer task databases and their own pages. Skip anything clearly done or archived.

2. **GitHub** — run the `gh` CLI via Bash to find PRs that need their attention:
   - `gh search prs --review-requested=@me --state=open` (PRs awaiting their review)
   - `gh search prs --author=@me --state=open` (their own open PRs — note CI/review status)
   - If GITHUB_REPOS env is set, you may also scope to those repos.

3. **Slack messages** — provided to you below (already fetched). These are \
@mentions of the user and DMs to them, within the lookback window. Use them to \
surface things they may have missed or still owe a reply to. IGNORE automated/bot \
messages (including this digest bot's own past messages) and anything they clearly \
already handled.
"""

_DAILY_OUTPUT = """\
## Output format

Output ONLY the digest as clean Slack-flavored markdown (no preamble, no \
"here is your digest"). Structure it as:

*:sunrise: Morning digest — {today}*

Group items by type. Omit any section that's empty.

*:hammer_and_wrench: Code / PRs*
- <one line per item — what it is, why it needs you, link if available>

*:bar_chart: Analysis / Notion tasks*
- <one line per item>

*:inbox_tray: From Slack ({window} — things you may have missed)*
- <one line per @mention/DM that still needs your attention, with who + where + link>

*:speech_balloon: Needs a reply / follow-up*
- <one line per item>

End with a single line: *Top 3 for today:* then a numbered 1-2-3 of what you \
should tackle first, based on urgency.

Keep it tight — a glance-able morning summary, not a report. If a source has \
nothing, say so in one line rather than inventing items.
"""

_WEEKLY_OUTPUT = """\
## Output format — MONDAY WEEKLY REVIEW

This is the Monday run. The Slack window covers the WHOLE PRIOR WEEK + weekend \
({window}). The Slack messages above are the PRIMARY evidence — they include \
what you were @mentioned about, your DMs, AND what YOU yourself said \
(tagged [ME]). Read them carefully: the [ME] messages are the record of what \
you actually worked on and discussed last week. Cross-reference these against \
your open/merged PRs to judge what got done, what's still open, and what's \
waiting on you.

Output ONLY the digest as clean Slack-flavored markdown. Your FIRST characters \
must be the header line below — no preamble, no "Here's your review", no \
"---" rule before it. Structure:

*:calendar: Weekly review — week of {window}*

*:speech_balloon: What you worked on (from Slack)*
- <2-5 lines summarizing the threads/topics you were active in last week, \
grounded in the [ME] / @mention / DM messages above — who + which channel. \
This section MUST reflect the Slack content, not just PRs.>

*:white_check_mark: Likely done last week*
- <PRs merged / tasks that look complete, corroborated by PRs + the Slack thread>

*:hourglass_flowing_sand: Still open / in progress*
- <your open PRs + active tasks/threads, with current status>

*:rotating_light: Needs follow-up*
- <stale PRs, unanswered @mentions/DMs, threads where someone is waiting on your \
reply — who + where + link>

*:dart: This week's priorities*
- <numbered 1-2-3-... of what to tackle this week>

Be specific and evidence-based: tie each call to the PR or the specific Slack \
message that supports it. Ignore bot/automated messages (including this digest \
bot's own past DMs).
"""


# Appended only when the interactive listener wants tappable task buttons. Asks
# Claude to emit a machine-readable task list after the human digest, so the
# listener can render ✅/⏭️ buttons that spawn an agent per task.
_ACTIONS_BLOCK = """\

## Actionable tasks (machine-readable — append AFTER the digest above)

After the digest, output a line containing only `<<<TASKS>>>` then a JSON array \
of the 1-5 most worthwhile tasks the user could hand to an agent right now. \
Pick only things that are concretely actionable today (a PR to fix, a reply to \
draft, an analysis to run) — skip vague or blocked items. Each task object:

  - "title": short label (<=60 chars), e.g. "Add a schema test to PR #123"
  - "prompt": clear instruction the agent will execute
  - "cwd": absolute repo path the agent should run in (use ~ for home)
  - "gate": one of "code" (edits files), "draft" (drafts a reply/review for \
approval), "readonly" (analysis only)
  - "source": ONE short phrase saying where this task came from — the specific \
trigger you saw, not a guess. E.g. "CodeRabbit review on PR #123", \
"a teammate's DM, 6/13", "#team-channel thread", "Notion task 'Q3 plan'". \
This is shown to the user so they trust why the task is here. Be specific.
  - "source_url": a link to that source if you have one (the PR URL, Slack \
permalink, or Notion page URL), else "".

Output the JSON array and nothing after it. If there are no good tasks, output \
`<<<TASKS>>>` followed by `[]`.
"""


def build_prompt(mode: str, today: str, window: str, slack_messages: str,
                 with_actions: bool = False, already_handled: str = "") -> str:
    """Assemble the full prompt for the given mode ('daily' or 'weekly').

    When ``with_actions`` is set, the digest is followed by a `<<<TASKS>>>`
    JSON block the interactive listener turns into ✅/⏭️ buttons.

    ``already_handled`` is an optional block (from the completion ledger) listing
    tasks the user already acted on, so the model does not resurface them.
    """
    output = _WEEKLY_OUTPUT if mode == "weekly" else _DAILY_OUTPUT
    # Optional: personalize with the user's name; otherwise stay generic ("your").
    name = os.environ.get("MY_NAME", "").strip()
    whose = f"{name}'s" if name else "your"
    plate = f"{name}'s plate" if name else "your plate"
    kind = "Monday weekly review" if mode == "weekly" else "morning to-do digest"
    intro = (
        f"You are building {whose} {kind} "
        f"for today ({today}). Gather what's on {plate}, then produce ONE concise digest.\n"
    )
    slack_block = (
        "\n## Slack messages already fetched (lookback: "
        f"{window})\n\n{slack_messages or '(none / Slack source not configured)'}\n"
    )
    return (
        intro
        + _SOURCES
        + slack_block
        + (already_handled or "")
        + output.format(today=today, window=window)
        + (_ACTIONS_BLOCK if with_actions else "")
    )
