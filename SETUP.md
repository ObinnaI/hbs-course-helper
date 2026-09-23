# Setup

Start-to-finish setup, written for someone who has never used a terminal. Budget
about 20 minutes, most of it waiting for downloads.

**Requires a Mac.** The calendar sync and the scheduled runs use macOS-only
tools (Apple Calendar and `launchd`).

---

## Before you start: collect three things

Open a note and paste these in as you go. You'll need all three in Step 3.

### 1. A Canvas API token

1. Log in to Canvas.
2. Click **Account** (bottom left) → **Settings**.
3. Scroll to **Approved Integrations** → click **+ New Access Token**.
4. Purpose: `course helper`. Leave the expiry blank.
5. Click **Generate Token**, then copy the long string.

> Copy it now. Canvas shows the token exactly once — if you close the box, you
> have to delete it and make a new one.

### 2. Your Canvas web address

Look at your browser's address bar while you're in Canvas and copy just the
domain — for HBS it is `https://hbs.instructure.com`. No trailing slash.

### 3. A Claude login (subscription) — or an API key

By default the cheat sheets are written through **Claude Code** with your
Claude Pro/Max subscription, so there is nothing to buy:

1. Install Claude Code: `npm install -g @anthropic-ai/claude-code` (needs
   Node; `brew install node` if you don't have it).
2. Run `claude` once in Terminal and sign in with your claude.ai account.
3. For the cloud job, run `claude setup-token` and keep the token it prints —
   it becomes the `CLAUDE_CODE_OAUTH_TOKEN` secret (valid a year).

Headless runs count against the plan's 5-hour and weekly allowance like your
own chats do; `NOTES_MAX_PER_RUN` in `.env` caps how many sheets one run makes.

Prefer to keep the subscription for interactive work? Set `NOTES_BACKEND=api`
and add an `ANTHROPIC_API_KEY` from https://console.anthropic.com (billed per
token, roughly $0.20–$0.60 per class).

---

## Step 1 — Get the code

Open **Terminal** (⌘-Space, type "Terminal", press Return) and paste this in:

```bash
git clone https://github.com/camcdriscoll-collab/hbs-course-helper.git ~/hbs-course-helper && cd ~/hbs-course-helper
```

If it says `git: command not found`, macOS will offer to install developer
tools — accept, wait for it to finish, then run the line again.

## Step 2 — Run the installer

```bash
./setup.sh
```

This creates an isolated Python environment, installs everything, and writes
a blank `.env` file for your credentials. It's safe to run more than once.

## Step 3 — Fill in your credentials

```bash
open -e .env
```

Replace the placeholder on each line with the values you collected above, then
save (⌘-S) and close. `COURSEWORK_ROOT` is where your course folders live —
`~/Desktop/Coursework` is a fine answer if you don't have one yet. Inside it,
courses go under a term folder (`Fall/Motivating People/`); the first run creates
them, or run `./.venv/bin/python scripts/path_config.py --discover` first to see
what it would create and adjust names in `canvas_config.json`.

> `.env` holds live credentials. It is already excluded from git, so it will
> never be uploaded — but don't paste its contents into email or Slack either.

## Step 4 — First run

```bash
./.venv/bin/python scripts/canvas_refresh.py --daily
```

You should see your courses discovered, files downloading, and notes generating
for anything due in the next two days. First run is the slow one.

## Step 5 — Let it run on its own (optional)

```bash
./setup.sh --schedule
```

Installs two background jobs: a daily sync at 5pm and a full weekly sync on
Sunday at 8am. To stop them later:

```bash
launchctl unload ~/Library/LaunchAgents/com.canvas-course-helper.*.plist
```

## Step 6 — Calendar and podcasts (optional)

- **Calendar**: create a calendar named exactly `Canvas Assignments` in Apple
  Calendar, then run `./.venv/bin/python scripts/calendar_sync.py`.
- **Podcasts**: run `./.venv/bin/notebooklm login` once and sign in to Google.

---

## When something breaks

| What you see | What to do |
|---|---|
| `Missing ANTHROPIC_API_KEY` | A line in `.env` is blank or still says `sk-ant-...` |
| `Canvas HTTP 401` | The Canvas token is wrong or was revoked — generate a new one |
| `WARNING: .env file not found` | You're not in the repo folder. `cd ~/hbs-course-helper` first |
| No courses found | Check `CANVAS_BASE_URL` — domain only, no `/api/v1`, no trailing slash |
| Readings don't download | `./.venv/bin/python -m playwright install chromium` |
| Scheduled runs do nothing | Check `canvas_refresh_daily.log` in the repo folder |

Still stuck? Open an issue on the repo with the error message — with your
tokens removed.

---

## Running without your Mac

The daily and weekly jobs can run in GitHub Actions instead, with your Mac only
copying the results into an iCloud folder when it is awake. See
[Running in the cloud](README.md#running-in-the-cloud-no-mac-needed) in the
README — it needs a private GitHub repo for the generated files and the same
three credentials stored there as repository secrets.


## Is the Mac mirror actually running?

The mirror job (`./setup.sh --mirror`) should touch `~/.hbs-mirror/last_run` every 30 minutes and append a line to `~/Library/Logs/hbs-mirror.log`. If `last_run` is hours old:

1. `launchctl print gui/$(id -u)/com.hbs-coursework.mirror` — `runs = 0` means launchd never started it; run `launchctl kickstart -k gui/$(id -u)/com.hbs-coursework.mirror`.
2. If the log says `Operation not permitted` or `privacy block`, macOS is keeping a bare `/bin/bash` out of iCloud Drive. Grant it Full Disk Access: System Settings → Privacy & Security → Full Disk Access → **+** → ⌘⇧G → `/bin/bash` → Open, then kickstart again.
3. `./setup.sh --mirror` does the kickstart and these checks for you and prints the fix.
