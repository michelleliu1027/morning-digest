"""The prompt that drives Claude to gather sources and build the digest."""

DIGEST_PROMPT = """\
You are building Michelle's "morning to-do digest" for today. Gather what's on \
her plate from two sources, then produce ONE concise digest.

## Sources to gather

1. **Notion tasks** — use the Notion MCP tools (notion-search / notion-fetch) to \
find tasks assigned to or authored by Michelle that are open / in-progress / due \
soon. Prefer task databases and her own pages. Skip anything clearly done or archived.

2. **GitHub** — run the `gh` CLI via Bash to find PRs that need her attention:
   - `gh search prs --review-requested=@me --state=open` (PRs awaiting her review)
   - `gh search prs --author=@me --state=open` (her own open PRs — note CI/review status)
   - If GITHUB_REPOS env is set, you may also scope to those repos.

## Output format

Output ONLY the digest as clean Slack-flavored markdown (no preamble, no \
"here is your digest"). Structure it as:

*:sunrise: Morning digest — <today's date>*

Group items by type and tag each with a priority. Use this shape:

*:hammer_and_wrench: Code / PRs*
- <one line per item — what it is, why it needs her, link if available>

*:bar_chart: Analysis / Notion tasks*
- <one line per item>

*:speech_balloon: Needs a reply / follow-up*
- <one line per item>

End with a single line: *Top 3 for today:* then a numbered 1-2-3 of what she \
should tackle first, based on urgency.

Keep it tight — this is a glance-able morning summary, not a report. If a section \
has nothing, omit it entirely. If you genuinely find nothing in a source, say so \
in one line rather than inventing items.
"""
