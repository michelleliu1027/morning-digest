"""The prompt that drives Claude to gather sources and build the digest."""

import os

# Marker that separates stage-1 triage prose from the candidate list.
CANDIDATES_MARKER = "<<<CANDIDATES>>>"

# Stage 1 of two-stage distillation. A single digest pass over hundreds of raw
# Slack messages (469 across 22 channels in practice) silently drops items that
# live in low-traffic threads — a 7-message group DM ranks below the busy PR
# channels and loses the model's attention, so a real ask (e.g. verify a
# partner's device IDs) never becomes a task. This pass does ONE job: walk every
# channel exhaustively and enumerate every actionable item, with no count cap, so
# nothing gets dropped for being in a quiet thread. Stage 2 then triages this
# clean, compact list instead of the raw firehose.
_SLACK_EXTRACT = """\
You are triaging a user's Slack activity so that NOTHING actionable slips through.

Below are ALL of the user's Slack messages in the window ({window}), grouped by \
channel: @mentions of them, DMs to them, and the user's OWN replies (tagged [ME]).

Walk through EVERY channel and EVERY thread — INCLUDING low-traffic group DMs with \
only a handful of messages. For each distinct ask, request, question, or commitment \
directed at OR made by the user, emit one candidate line. Do NOT merge separate \
threads into one item, do NOT cap the number of items, and do NOT skip a thread \
because it is small or looks minor. Being exhaustive matters far more than being \
concise here — a missed item is the failure mode.

Classify each candidate with a STATUS:
  - OPEN_ASK        : someone asked the user something and there is NO [ME] reply yet
  - OWED_FOLLOWUP   : the user replied but the other person is still waiting (a \
promised deliverable not sent, an unanswered point)
  - INTENT_NOT_DONE : a [ME] reply states a PLAN or intent but not the actual \
result — e.g. "haven't had a chance to check, but I think it's in table X", "will \
run a query on…", "my guess is…". The user committed to work they have NOT done. \
These are PRIME agent tasks.
  - HANDLED         : the user already delivered the actual answer/result; nothing left

IGNORE automated/bot messages (including this digest bot's own past posts).

Output ONLY a line containing `{marker}` then, one item per line in this exact form:
[STATUS] one-sentence summary of the ask/commitment — who, #channel — <permalink or ->

List every non-trivial item you found. Output nothing after the list."""

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

3. **Slack messages** — provided to you below (already fetched). These include \
@mentions of the user, DMs to them, AND the user's OWN replies (tagged [ME]), all \
within the lookback window. Use them to surface things they may have missed or \
still owe a reply to.

   CRITICAL — judge whether each incoming ask was already resolved BY THE USER: \
for every @mention/DM, look for a later [ME] message from the user in the SAME \
channel/thread that answers it. If the user already replied and the reply settles \
the ask, treat it as HANDLED — do NOT list it under "needs attention". If the user \
replied but the other person is still waiting (a follow-up question, a promised \
deliverable not yet sent, an unanswered point), list it as awaiting follow-up and \
say specifically what is still outstanding. Only list an item as needing a reply \
if there is NO [ME] response to it at all. IGNORE automated/bot messages (including \
this digest bot's own past messages).

   A [ME] reply only counts as "settles the ask" when it delivers the actual \
answer/result. A reply that states an INTENT or a PLAN but not the work itself — \
e.g. "I haven't had a chance to check yet, but I think it's in table X", "will \
look into this", "my guess is…", "I'll run a query on…" — does NOT settle it. The \
user committed to doing something they have not yet done. These are the BEST \
candidates to hand to an agent: surface them (and emit them as actionable tasks), \
phrased as the concrete thing the user said they'd do.
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
- <one line per @mention/DM that STILL needs your attention, with who + where + \
link. Do NOT list items you already answered (a [ME] reply that settles the ask). \
If you replied but they're still waiting, list it and state what's still \
outstanding (e.g. "replied with ETA, still owe them the actual fix").>

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


def build_extract_prompt(window: str, slack_messages: str) -> str:
    """Stage 1: ask the model to exhaustively enumerate every actionable Slack item.

    Returns a prompt whose output (after CANDIDATES_MARKER) is a flat,
    de-firehosed candidate list that stage 2 (build_prompt) consumes instead of
    the raw per-channel message dump.
    """
    head = _SLACK_EXTRACT.format(window=window, marker=CANDIDATES_MARKER)
    body = (
        "\n\n## Slack messages (lookback: "
        f"{window})\n\n{slack_messages or '(none / Slack source not configured)'}\n"
    )
    return head + body


def build_prompt(mode: str, today: str, window: str, slack_messages: str,
                 with_actions: bool = False, already_handled: str = "",
                 slack_candidates: str = "") -> str:
    """Assemble the full prompt for the given mode ('daily' or 'weekly').

    When ``with_actions`` is set, the digest is followed by a `<<<TASKS>>>`
    JSON block the interactive listener turns into ✅/⏭️ buttons.

    ``already_handled`` is an optional block (from the completion ledger) listing
    tasks the user already acted on, so the model does not resurface them.

    ``slack_candidates`` is the stage-1 triage output (see build_extract_prompt).
    When provided it REPLACES the raw per-channel Slack dump: stage 1 already
    walked every channel exhaustively, so stage 2 reasons over the compact,
    de-firehosed list and can't lose a quiet thread in the noise. Falls back to
    the raw ``slack_messages`` when no candidate list is given.
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
    if slack_candidates:
        slack_block = (
            "\n## Slack items — already triaged (lookback: "
            f"{window})\n\nThese were extracted by an exhaustive first pass over "
            "every channel; each line is one actionable item with its STATUS. "
            "Treat this as the COMPLETE set of Slack work — do not assume anything "
            "is missing, and do not drop OPEN_ASK / OWED_FOLLOWUP / INTENT_NOT_DONE "
            f"items.\n\n{slack_candidates}\n"
        )
    else:
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
