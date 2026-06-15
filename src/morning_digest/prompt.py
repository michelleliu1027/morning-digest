"""The prompt that drives Claude to gather sources and build the digest."""

# Sources Claude gathers itself via tools (Notion MCP + gh CLI).
_SOURCES = """\
## Sources to gather

1. **Notion tasks** — use the Notion MCP tools (notion-search / notion-fetch) to \
find tasks assigned to or authored by Michelle that are open / in-progress / due \
soon. Prefer task databases and her own pages. Skip anything clearly done or archived.

2. **GitHub** — run the `gh` CLI via Bash to find PRs that need her attention:
   - `gh search prs --review-requested=@me --state=open` (PRs awaiting her review)
   - `gh search prs --author=@me --state=open` (her own open PRs — note CI/review status)
   - If GITHUB_REPOS env is set, you may also scope to those repos.

3. **Slack messages** — provided to you below (already fetched). These are \
@mentions of Michelle and DMs to her, within the lookback window. Use them to \
surface things she may have missed or still owes a reply to. IGNORE automated/bot \
messages (including this digest bot's own past messages) and anything she clearly \
already handled.
"""

_DAILY_OUTPUT = """\
## Output format

Output ONLY the digest as clean Slack-flavored markdown (no preamble, no \
"here is your digest"). Structure it as:

*:sunrise: Morning digest — {today}*

Group items by type. Omit any section that's empty.

*:hammer_and_wrench: Code / PRs*
- <one line per item — what it is, why it needs her, link if available>

*:bar_chart: Analysis / Notion tasks*
- <one line per item>

*:inbox_tray: From Slack ({window} — things you may have missed)*
- <one line per @mention/DM that still needs her attention, with who + where + link>

*:speech_balloon: Needs a reply / follow-up*
- <one line per item>

End with a single line: *Top 3 for today:* then a numbered 1-2-3 of what she \
should tackle first, based on urgency.

Keep it tight — a glance-able morning summary, not a report. If a source has \
nothing, say so in one line rather than inventing items.
"""

_WEEKLY_OUTPUT = """\
## Output format — MONDAY WEEKLY REVIEW

This is the Monday run. The Slack window covers the WHOLE PRIOR WEEK + weekend \
({window}). In addition to the usual to-do list, assess Michelle's PROGRESS: \
cross-reference her open/merged PRs and the Slack replies below to judge what got \
done last week, what's still open, and what needs follow-up.

Output ONLY the digest as clean Slack-flavored markdown. Your FIRST characters \
must be the header line below — no preamble, no "Here's your review", no \
"---" rule before it. Structure:

*:calendar: Weekly review — week of {window}*

*:white_check_mark: Likely done last week*
- <PRs merged / tasks that look complete based on PRs + replies>

*:hourglass_flowing_sand: Still open / in progress*
- <her open PRs + active tasks, with current status>

*:rotating_light: Needs follow-up*
- <stale PRs, unanswered @mentions/DMs from last week, threads waiting on her — who + where + link>

*:dart: This week's priorities*
- <numbered 1-2-3-... of what to tackle this week>

Be specific and evidence-based: tie each "done" / "follow-up" call to the PR or \
the Slack message that supports it. Ignore bot/automated messages.
"""


def build_prompt(mode: str, today: str, window: str, slack_messages: str) -> str:
    """Assemble the full prompt for the given mode ('daily' or 'weekly')."""
    output = _WEEKLY_OUTPUT if mode == "weekly" else _DAILY_OUTPUT
    intro = (
        f'You are building Michelle\'s {"Monday weekly review" if mode == "weekly" else "morning to-do digest"} '
        f"for today ({today}). Gather what's on her plate, then produce ONE concise digest.\n"
    )
    slack_block = (
        "\n## Slack messages already fetched (lookback: "
        f"{window})\n\n{slack_messages or '(none / Slack source not configured)'}\n"
    )
    return (
        intro
        + _SOURCES
        + slack_block
        + output.format(today=today, window=window)
    )
