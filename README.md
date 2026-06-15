# Morning Digest

Every morning, Claude looks at your Notion tasks, GitHub PRs, and recent Slack
activity, figures out what's on your plate today, and DMs you a tight to-do list
on Slack. You glance at it, decide what to tackle, and do the real work (code
review, analysis) in Claude Code.

It's intentionally small: no database, no always-on server, no cloud. A scheduled
local job shells out to the Claude Code CLI, which does the gathering and
summarizing using tools you already have connected, then posts the result to your
own Slack DM.

**Two modes, picked automatically by the day of week:**

- **Daily (Tue–Fri):** a to-do list for today, plus a "from Slack" section that
  surfaces yesterday's @mentions and DMs you may have missed.
- **Weekly (Monday):** a review of the *whole prior week* — it reads what you said,
  were @mentioned about, and DM'd across the week, cross-references your open/merged
  PRs, and tells you what got done, what's still open, and what needs follow-up,
  then plans the week.

## Requirements

- macOS (for the `launchd` scheduler; the script itself is plain Python and runs anywhere)
- Python 3.11+
- [Claude Code CLI](https://claude.com/claude-code) on your `PATH` (`claude`)
- [`gh`](https://cli.github.com/) CLI, authenticated (`gh auth login`)
- A Notion MCP server connected to Claude Code (`claude mcp add ...`)
- A Slack workspace where you can create an app

```
launchd (morning, or at login)
      |
      v
python -m morning_digest          (auto: weekly on Monday, daily otherwise)
      |
      +--> fetch Slack @mentions / DMs / sent msgs   (user token, search:read)
      |
      +--> claude -p  ──uses Notion MCP + gh CLI──> gathers + summarizes
      |
      v
Slack DM to you  (just you — not a channel)
```

Reading Slack is optional: leave the user token blank and the digest still runs
on Notion + GitHub alone.

## Setup

### 1. Install

```bash
cd ~/Documents/GitHub/morning-digest
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
```

### 2. Create your Slack app (the part that's easy to forget)

1. Go to **https://api.slack.com/apps** → **Create New App** → **From scratch**.
   Name it `Morning Digest`, pick your workspace.
2. Left sidebar → **OAuth & Permissions**.
3. Scroll to **Scopes → Bot Token Scopes** → **Add an OAuth Scope** → add:
   - `chat:write`  (lets it DM you the digest)
   - `im:write`    (lets it open a DM with you)
4. *(Optional — only if you want the Slack source.)* In the **same OAuth page**,
   scroll to **Scopes → User Token Scopes** (a separate section below Bot Token
   Scopes) → add:
   - `search:read`  (lets it read your @mentions / DMs / sent messages)

   This **must** be a *User* Token Scope. Bot tokens cannot use Slack search —
   they fail with `not_allowed_token_type` no matter what scopes you give them.
5. Scroll up → **Install to Workspace** → **Allow**.
6. Copy the tokens into `.env`:
   - **Bot User OAuth Token** (starts with `xoxb-`) → `SLACK_MORNING_TASKS_DIGEST_BOT_TOKEN`
   - **User OAuth Token** (starts with `xoxp-`, only shown if you added `search:read`)
     → `SLACK_MORNING_TASKS_DIGEST_USER_TOKEN`

> The bot token is required (it sends the DM). The user token is optional — leave
> it blank to skip the Slack source and run on Notion + GitHub only.

### 3. Get your own Slack user ID (and optionally your name)

In Slack: click your profile picture → **Profile** → the **⋮ (More)** button →
**Copy member ID**. It looks like `U01ABC2DEF`. Put it in `.env` as
`MY_SLACK_USER_ID` — this is both where the digest is sent and whose Slack
activity gets searched.

Optionally set `MY_NAME` in `.env` to personalize the prompt (e.g. "Alex's
morning digest"); leave it blank to keep it generic.

### 4. Test it

```bash
# Just gather + print, don't send to Slack yet:
python -m morning_digest --dry-run

# Force the Monday weekly review (any day), printed only:
python -m morning_digest --dry-run --mode weekly

# Real run — auto-picks daily/weekly by weekday and sends the DM:
python -m morning_digest
```

The first `claude -p` run may prompt about tool permissions; the `--allowedTools`
flag pre-approves `gh` and the Notion MCP read tools.

### 5. Run it every morning (when your laptop is on)

See `launchd/` for a ready-to-use macOS Launch Agent that runs the digest at
**8:00 AM** and also at login. It runs `python -m morning_digest` with no flag,
so it auto-picks the weekly review on Mondays and the daily digest otherwise — no
separate Monday job needed. Install it:

```bash
cp launchd/com.morning-digest.plist ~/Library/LaunchAgents/
# Edit the plist first: set the absolute paths (see comments inside).
launchctl load ~/Library/LaunchAgents/com.morning-digest.plist
```

## Roadmap

- **v1 (done):** Notion + GitHub → DM digest. Local-only, send-only.
- **v2 (done):** read Slack @mentions / DMs / sent messages as a source, with a
  Monday weekly review (needs user token + `search:read`).
- **next:** reply-from-Slack to trigger work — click ✅ to spawn Claude Code on a
  task, with approval gates before anything hits production (needs Socket Mode /
  always-on host).

## License

MIT — see [LICENSE](LICENSE).
