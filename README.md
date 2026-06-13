# Morning Digest

Every morning, Claude looks at your Notion tasks and GitHub PRs, figures out what's
on your plate today, and DMs you a tight to-do list on Slack. You glance at it,
decide what to tackle, and do the real work (code review, analysis) in Claude Code.

It's intentionally small: no database, no always-on server, no cloud. A scheduled
local job shells out to the Claude Code CLI, which does the gathering and
summarizing using tools you already have connected, then posts the result to your
own Slack DM.

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
python -m morning_digest
      |
      +--> claude -p  ──uses Notion MCP + gh CLI──> gathers + summarizes
      |
      v
Slack DM to you  (just you — not a channel)
```

It only **sends** to Slack in v1 (one bot token, `chat:write`). Reading Slack
messages is a later add-on.

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
4. Scroll up → **Install to Workspace** → **Allow**.
5. Copy the **Bot User OAuth Token** (starts with `xoxb-`) → paste into `.env`
   as `SLACK_BOT_TOKEN`.

> v1 needs **only** the bot token. No Socket Mode, no user token, no app token.
> (Those come back if/when we add *reading* your Slack messages.)

### 3. Get your own Slack user ID

In Slack: click your profile picture → **Profile** → the **⋮ (More)** button →
**Copy member ID**. It looks like `U01ABC2DEF`. Put it in `.env` as
`MY_SLACK_USER_ID`.

### 4. Test it

```bash
# Just gather + print, don't send to Slack yet:
python -m morning_digest --dry-run

# Real run — sends the DM:
python -m morning_digest
```

The first `claude -p` run may prompt about tool permissions; the `--allowedTools`
flag pre-approves `gh` and the Notion MCP read tools.

### 5. Run it every morning (when your laptop is on)

See `launchd/` for a ready-to-use macOS Launch Agent that runs the digest at
**8:00 AM** and also at login. Install it:

```bash
cp launchd/com.morning-digest.plist ~/Library/LaunchAgents/
# Edit the plist first: set the absolute paths (see comments inside).
launchctl load ~/Library/LaunchAgents/com.morning-digest.plist
```

## Roadmap

- **v1 (this):** Notion + GitHub → DM digest. Local-only, send-only.
- **v2:** add reading Slack @mentions/DMs as a source (needs user token + `search:read`).
- **v3:** reply-from-Slack to trigger work (needs Socket Mode / always-on host).

## License

MIT — see [LICENSE](LICENSE).
