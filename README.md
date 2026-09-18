# Canvas Course Helper

Automated Canvas file sync, AI-generated case prep notes, reading downloads, weekly planning, calendar integration, and NotebookLM podcast generation for MBA coursework.

Works with any Canvas LMS instance (Harvard Business School, Stanford GSB, Wharton, etc.).

---

## What it does

| Script | Purpose |
|--------|---------|
| `canvas_refresh.py --daily` | Sync files + download readings + regenerate stale notes for sessions in the next 2 days. Runs automatically at 5pm via launchd. |
| `canvas_refresh.py --weekly` | Full 6-week sync, reading downloads, notes for 2-week window, weekly overview doc, calendar sync, participation tracker refresh. Runs automatically Sunday 8am via launchd. |
| `canvas_readings.py YYMMDD COURSE` | Download all linked readings for one session (HBSP cases, articles, YouTube stubs). |
| `canvas_organize.py` | Route files to correct folders (slides to `Slides/`, etc.) and move duplicates to Trash. Runs automatically after every sync. |
| `weekly_overview.py` | Generate `Overview/YYMMDD Overview.docx` — Mon–Fri breakdown of sessions and submissions for the upcoming week. |
| `calendar_sync.py` | Sync Canvas assignment deadlines to Apple Calendar ("Canvas Assignments"). Idempotent. Mac only. |
| `ics_feed.py` | Write the same deadlines as a subscribable `canvas.ics` feed (what the cloud run publishes). |
| `participation_tracker.py` | Build/refresh `Participation Tracker.xlsx` — all courses side by side with a live spoke/entered rate per course. |
| `path_config.py --discover` | Show the courses Canvas returns and the folder each would get, without creating anything. Run before the first sync. |
| `cheat_sheet.py YYMMDD COURSE` | Generate the cheat sheet (`.docx` + `.md`) on demand for a specific class day. |
| `course_brief.py` | Maintain each course's `Course Brief.md` (lenses per class, threads, materials index) and `CLAUDE.md`. Runs after every sync; `--bootstrap` for the first fill. |
| `migrate_folders.py` | Adopt hand-made class folders (`Class 5 - Pave`) into the dated scheme. Dry run by default. |
| `podcast_gen.py YYMMDD COURSE` | Generate a ~30-min NotebookLM audio overview on demand for a specific session. |
| `update_mcps.py` | Check PyPI for dependency updates and upgrade the venv. Run manually when needed. |

---

## Scheduled jobs

Two jobs run automatically once set up — on your Mac via launchd (`./setup.sh --schedule`), or in GitHub Actions with the Mac only mirroring the results (see [Running in the cloud](#running-in-the-cloud-no-mac-needed)):

### Daily at 5pm — `canvas_refresh.py --daily`

1. Discover class sessions with due dates in the next 2 calendar days (quizzes and uploads are deliverables, not sessions — they go to the calendar instead)
2. Sync Canvas-hosted files for those sessions (files attached in Canvas folders and assignments)
3. Download externally-linked readings (HBSP cases, articles → PDF, YouTube → stub)
4. Generate or refresh Notes `.docx` if stale (new files, edited Canvas description, updated prompt)
5. Organize folders and move duplicates to Trash
6. Sync Canvas deadlines to Apple Calendar

### Sunday 8am — `canvas_refresh.py --weekly`

1. Sync all course files across a 6-week forward horizon
2. For every session in the next 2 weeks: sync Canvas files + download linked readings
3. Generate or refresh Notes for those sessions
4. Organize folders and move duplicates to Trash
5. Generate `Overview/YYMMDD Overview.docx` for the upcoming Mon–Fri
6. Sync Canvas deadlines to Apple Calendar
7. Refresh `Participation Tracker.xlsx` (preserves any ratings already entered)
8. *(If `--with-podcast`)* Show upcoming sessions → prompt to skip any → generate podcasts

---

## Reading downloads (`canvas_readings.py`)

For every session in the notes window, the sync automatically downloads all readings linked in the Canvas assignment description:

| Link type | Action |
|-----------|--------|
| `hbsp.harvard.edu/tu/...` | Download PDF (public coursepack links — no login needed) |
| `services.hbsp.harvard.edu/.../sclinks/` | Download PDF (coursepack sclinks) |
| External articles / blog posts | Headless Chromium print-to-PDF |
| YouTube videos | Save a `.txt` stub with the title and URL |
| LinkedIn / social / mailto | Skip silently |
| `instructure.com` Canvas files | Skip (already handled by the Canvas file sync) |

**Oversized files:** If a PDF exceeds the 150-page limit or the 700k-token context budget, it is downloaded to the session folder but excluded from the AI notes. A `{name} (skipped).txt` stub is written next to it so the exclusion is visible. To force inclusion, raise `PDF_PAGE_LIMIT` / `MAX_PDF_TOKEN_BUDGET` in `scripts/canvas_refresh.py`, delete the Notes file, and re-run — deleting the stub alone does nothing.

**File type safety:** If an HBSP download returns a non-PDF (e.g. an Excel exhibit named `.pdf`), the archive is inspected and the file renamed to its real extension (`.xlsx`, `.docx`, `.pptx`) before it reaches the notes generator. Pages that render as a login or paywall screen (or fewer than ~800 characters of text) are skipped rather than saved as a reading; they are retried on the next run.

Debug / preview:
```bash
./.venv/bin/python scripts/canvas_readings.py --list 260908 LTV   # show links without downloading
./.venv/bin/python scripts/canvas_readings.py 260908 LTV          # download for one session
```

---

## Cheat sheets (AI case prep)

Claude reads the assigned PDFs, the Canvas posting and the course's knowledge base (below) and writes `Cheat Sheet - <Case>.docx` (+ `.md`, readable in the GitHub app on a phone) into the class-day folder:
- The discussion questions verbatim, then the case in 90 seconds, the cast and the timeline
- For each question: a 20-second answer to say first, lettered evidence sub-points with page cites, computed numbers in tables, a discussion-ready synthesis and a contrarian line
- 8+ likely follow-ups, key concepts by source, "how this connects to earlier classes", and the numbers to have in hand

**Which Claude.** `NOTES_BACKEND=claude-code` (default) runs `claude -p` — Claude Code's headless mode — signed in with a Claude Pro/Max subscription, so the work draws on the plan's allowance rather than API credit. The model reads the PDFs itself from the class folder. Sign in once with `claude` on the Mac; in the cloud, `claude setup-token` gives a one-year token for the `CLAUDE_CODE_OAUTH_TOKEN` secret. A usage limit parks the remaining notes until the next run (`--notes-max` caps a run); a sign-out stops with the re-login command. `NOTES_BACKEND=api` keeps the original per-token Messages API path with `ANTHROPIC_API_KEY`. `NOTES_MODEL` (default `claude-opus-5`) picks the model; `NOTES_STYLE=compact` restores the original 12pt notes format and prompt (`prompts/cheat_sheet_prompt_compact.md`).

Once a class has happened its cheat sheet is frozen: anything that arrives afterwards feeds the course brief and the next class instead.

Notes are regenerated automatically when:
- The session folder has no Notes file yet
- New reading files have been added since the last generation
- The Canvas assignment description changed (professor edited it)
- The master prompt or course refinement prompt was updated since last generation

…and never once the class is past. All of these are content checks recorded in `.notes_meta.json` (hashes of each posting, each reading, and the prompt) — not file timestamps, which git does not preserve. If a class day has two Canvas postings, both go into one Notes document.

**Prompt customization:** Edit `prompts/cheat_sheet_prompt.md` (master prompt applied to all courses) and/or create `prompts/cheat_sheet_prompt_COURSE_refinement.md` for course-specific instructions. Editing a prompt marks all upcoming Notes as stale so they regenerate on the next run.

---

## Course knowledge base (`course_brief.py`)

Every course keeps a **materials shelf** — a folder you already have (`Course Textbook and Materials`, `Course Docs`) is adopted, otherwise `Course Materials/` is created. Canvas course-level files land there; you drop textbook chapters there; it is never reshuffled or deduplicated. In it lives **`Course Brief.md`**, the running memory of the course:

- **About this course** (written once from the syllabus) and **How this professor runs class** (yours to edit — same role as the refinement prompt)
- **Lenses and frameworks so far** — one block per class that has happened: frameworks introduced and when to use them, takeaways, numbers worth remembering, threads to carry forward, glossary terms. Written by a short Claude call from the cheat sheet, the readings and whatever was posted after class; refreshed only when those inputs change
- **Threads to carry forward** — rolled up from the class blocks
- **Materials index** — one line per file on the shelf, summarised once (cached by content hash)

Auto sections are fenced with `<!-- auto:… -->` markers; everything else in the file is yours and is preserved. The sync **looks back three weeks** for post-class material: recent class folders on Canvas, Modules items (files and pages), and Announcements, filed into the class they name (by class number) or onto the shelf, each fetched once (`claude/synced_items.json`).

The next cheat sheet receives the brief, the previous class's bottom lines and post-class files, a peek at the next posting, and the materials index (with paths the model may read), and is asked to apply at least two named lenses from earlier classes. Each course folder also gets a `CLAUDE.md`, and `/course <ABBREV>` in Claude Code loads the same context for ad-hoc work — the equivalent of a per-course Cowork project. `BRIEF_MODEL` (default `claude-sonnet-5`) picks the model for brief updates.

---

## Adopting folders you made by hand (`migrate_folders.py`)

```bash
./.venv/bin/python scripts/migrate_folders.py --root "~/Library/Mobile Documents/com~apple~CloudDocs/HBS/Classes/2026"          # report
./.venv/bin/python scripts/migrate_folders.py --root … --map "Fall/Negotiations/Class 3 - Treu Pharma=260910" --apply
```

`Class 5 - Pave` becomes `260916 Class 5 - Pave (A)` (date and title from the matching Canvas posting — class number first, title similarity as a tiebreak, `--map` to force, `--accept-fuzzy` for title-only matches). Every file is kept; an existing `Cheat Sheet - *.docx` is recorded as that day's notes so nothing regenerates. `Quiz 1`, `Course Docs`, `RH` and the like are listed as KEEP. Run it against the iCloud folder first, then import into the data-repo clone.

---

## Folder organization (`canvas_organize.py`)

Files are routed to canonical locations after every sync. Each file lives in exactly one place.

| File type | Destination |
|-----------|-------------|
| PPTX / PPT | `<materials shelf>/Slides/` (always, even if Canvas attached them to a class) |
| PDF / DOCX in a class-day folder | Stays in `YYMMDD Class N - Title/` |
| Anything on the materials shelf | Stays put — the shelf is the course's reference, not something to reshuffle |

Duplicates — same name **and identical content (MD5)**, across a course's class-day folders (earliest kept) or between a class folder and `Slides/` — are moved to the macOS Trash, or to `COURSEWORK_ROOT/.trash/` where there is no Trash. Same name, different content: a warning is printed and both copies are kept. The materials shelf, `Quiz*`, `Course Docs` and anything outside the course folders are never touched.

---

## Participation Tracker

`participation_tracker.py` creates/refreshes one `Participation Tracker.xlsx` per term folder (`Fall/`, `Spring/`); a term whose last class is more than 30 days past is left alone:

- All courses displayed side by side (one column group per course, alphabetical)
- Each course gets its own color scheme from a six-colour palette
- **Row 1**: Full course name header
- **Row 2**: Live participation rate — `spoke / entered` (formula updates as you fill in ratings)
- **Row 3**: Column labels — Day | Case Title | Rating
- **Row 4+**: One row per Canvas session, sorted by date

Rating values: `ok`, `good`, `great`, `x` (didn't speak), or blank (not yet entered). Dropdown validation in every Rating cell. Conditional color-coding: great = green, good = light green, ok = yellow, x = gray.

On refresh, existing ratings are preserved (matched by the course name in the column header + the session date), so Canvas title or date updates — or a new course sorting in front of the others — don't clobber your entries.

---

## Weekly Overview

`weekly_overview.py` generates `Overview/YYMMDD Overview.docx`:
- Organized Monday through Friday
- Each day lists readings per course (with page counts where available)
- Deliverables (quizzes, uploads, papers) are called out in **bold** at the top of each day
- Saved in `Coursework/Overview/`

---

## Calendar sync

`calendar_sync.py` creates events in Apple Calendar for every Canvas deliverable (quizzes, uploads, papers). Events appear in the **"Canvas Assignments"** calendar.

- Works with iCloud and Google Calendar — create the calendar in whichever you prefer and it syncs to Apple Calendar automatically
- State is tracked in `~/.canvas_calendar_state.json` — reruns won't create duplicates
- Event title format: `5:00pm — LTV Writing Assignment #1 (LTV)`

**First-time setup:** Create a calendar named exactly `Canvas Assignments` in Apple Calendar (or in iCloud/Google Calendar and let it sync). Then run `calendar_sync.py` once to populate it.

**No Mac? Use the feed instead.** `ics_feed.py` (or `CALENDAR_BACKEND=ics`) writes `canvas.ics` next to `canvas_config.json`: every deliverable as an event with a stable UID, so a moved due date updates the existing event rather than adding one. Host it anywhere a calendar app can fetch a URL — the cloud workflow below publishes it to a secret Gist — and subscribe once in Calendar (File → New Calendar Subscription).

---

## Podcast generation

`podcast_gen.py` creates a conversational audio overview using NotebookLM:
- **Case only**: 20-min deep-dive + 10-min discussion question walkthrough
- **Case + supplemental readings**: adds a 5-min frameworks section
- Saved as `YYMMDD COURSE Podcast.m4a` in the session folder

Prompt templates in `prompts/` are fully editable:
- `podcast_prompt.md` — base template (case only)
- `podcast_prompt_supplemental.md` — template with supplemental readings
- `podcast_prompt_COURSE_refinement.md` — per-course additions (one per course)

---

## Folder structure

```
Coursework/                              ← one folder per academic year (e.g. HBS/Classes/2026)
  Overview/
    260831 Overview.docx                 ← weekly planning doc (+ .md)
  Fall/                                  ← term folder, from Canvas's term name
    Participation Tracker.xlsx           ← one per term
    Launching Tech Ventures/             ← full course name (any folder name works; set folder_name in config)
      CLAUDE.md                          ← context for Claude Code / Cowork in this folder
      Course Materials/                  ← the shelf (or your own "Course Textbook and Materials")
        Course Brief.md                  ← the course's running memory
        Slides/                          ← all PPTX files (always here, never in class folders)
        Announcements/                   ← Canvas announcements not tied to a class
      260902 Class 3 - Rocky Mountain Condiments/
        260902 Rocky Mountain Condiments.pdf   ← HBSP case (auto-downloaded)
        260902 The idea maze.pdf         ← article (auto-downloaded, printed to PDF)
        260902 Beachhead Market (YouTube).txt
        Cheat Sheet - Rocky Mountain Condiments.docx
        Cheat Sheet - Rocky Mountain Condiments.md   ← same notes, readable anywhere Markdown renders
        260902 LTV Podcast.m4a
        260903 Announcement - Class 3 wrap-up.md     ← posted after class; feeds the brief
        .notes_meta.json                 ← what the notes were generated from (hashes)
      260908 Class 4 - Ginkgo Bio/
        260908 Ginkgo Bio.pdf
        260908 Ginkgo Bio (skipped).txt  ← over the page limit; file present but excluded from notes
    Corporate Financial Operations/
      ...
  Spring/                                ← appears by itself when Canvas publishes the next term
  claude/
    canvas_config.json                   ← auto-updated: courses, terms, folder names (abbrev_overrides, term_folders, ignored_courses are yours)
    canvas.ics                           ← deadlines feed
    synced_items.json                    ← post-class items already fetched
```

---

## Setup

**New here? Read [SETUP.md](SETUP.md) instead** — it walks through the same
steps assuming no terminal experience, and explains where each credential
comes from.

The short version, on a Mac:

```bash
git clone https://github.com/camcdriscoll-collab/hbs-course-helper.git ~/hbs-course-helper
cd ~/hbs-course-helper
./setup.sh                # venv, dependencies, headless Chromium, blank .env
open -e .env              # fill in your four values
./.venv/bin/python scripts/canvas_refresh.py --daily
```

Then, optionally, `./setup.sh --schedule` to install the 5pm daily and Sunday
8am `launchd` jobs.

### The four values in `.env`

See [`.env.example`](.env.example) for the annotated template.

| Key | Where it comes from |
|-----|---------------------|
| `CANVAS_API_TOKEN` | Canvas → Account → Settings → **+ New Access Token** |
| `CANVAS_BASE_URL` | Your Canvas domain, e.g. `https://hbs.instructure.com` — no trailing slash, no `/api/v1` |
| `ANTHROPIC_API_KEY` | Only for `NOTES_BACKEND=api`: https://console.anthropic.com → Settings → API keys. The default backend uses your Claude subscription through Claude Code instead. |
| `COURSEWORK_ROOT` | The folder holding your per-course subfolders, e.g. `~/Desktop/Coursework` |

Every key can also be set as an environment variable, which takes precedence over `.env` — that is how the cloud workflow passes secrets. Optional keys (`CANVAS_CONFIG_FILE`, `CALENDAR_BACKEND`, `PODCAST_MAX_PER_RUN`, `MIRROR_*`) are documented in `.env.example`.

> **Subscription or API?** By default the cheat sheets are written through
> Claude Code (`claude -p`) with your Claude Pro/Max login — sign in once with
> `claude` on the Mac (`npm install -g @anthropic-ai/claude-code`). Headless
> runs count against the plan's 5-hour and weekly allowance like any other use.
> `NOTES_BACKEND=api` switches to the developer API, billed per token from the
> Console, if you'd rather keep the subscription for interactive work.

> Courses are auto-discovered from Canvas on the first run. There is no course
> ID configuration to fill in. Run `./.venv/bin/python scripts/path_config.py --discover`
> first to see what will be created; edit `abbrev_overrides`, `ignored_courses`, or a
> course's `folder_name` in `canvas_config.json` if you want different names.

Setting `COURSEWORK_ROOT` means the scripts run in place from this clone. If
you leave it unset, they fall back to assuming they live at
`Coursework/claude/scripts/` and treat the grandparent folder as the root.

## Usage

```bash
# Daily sync (next 2 days)
./.venv/bin/python scripts/canvas_refresh.py --daily

# Weekly sync + overview + calendar
./.venv/bin/python scripts/canvas_refresh.py --weekly

# Weekly with podcasts (interactive skip-list at a terminal; unattended otherwise)
./.venv/bin/python scripts/canvas_refresh.py --weekly --with-podcast

# Only the missing podcasts for the next 7 days, at most 2 this run
./.venv/bin/python scripts/canvas_refresh.py --podcasts-only --podcast-max 2

# Show which courses Canvas returns and what folders they'd get (creates nothing)
./.venv/bin/python scripts/path_config.py --discover

# Download readings for a specific session
./.venv/bin/python scripts/canvas_readings.py 260902 LTV

# List links for a session without downloading
./.venv/bin/python scripts/canvas_readings.py --list 260902 LTV

# Generate notes for a specific session on demand
./.venv/bin/python scripts/cheat_sheet.py 260902 LTV

# Refresh the participation tracker manually
./.venv/bin/python scripts/participation_tracker.py

# Generate a podcast for a specific session
./.venv/bin/python scripts/podcast_gen.py 260902 LTV

# Generate the weekly overview doc manually
./.venv/bin/python scripts/weekly_overview.py

# Sync calendar deadlines manually
./.venv/bin/python scripts/calendar_sync.py
./.venv/bin/python scripts/calendar_sync.py --dry-run   # preview without creating events
./.venv/bin/python scripts/ics_feed.py                  # write canvas.ics instead (no Mac needed)

# Organize and dedup folders
./.venv/bin/python scripts/canvas_organize.py

# Check for dependency updates
./.venv/bin/python scripts/update_mcps.py
```

---

## Flags

| Flag | Script | Effect |
|------|--------|--------|
| `--daily` | `canvas_refresh.py` | Sync next 2 days and refresh stale notes |
| `--weekly` | `canvas_refresh.py` | Full 6-week sync + overview + calendar + tracker |
| `--with-podcast` | `canvas_refresh.py` | Also generate podcasts (weekly at a terminal: interactive skip-list; otherwise unattended) |
| `--podcasts-only` | `canvas_refresh.py` | No sync; just fill in missing podcasts (the Mac's fallback when the cloud couldn't) |
| `--podcast-days N` | `canvas_refresh.py` | Podcast horizon in days (default 7) |
| `--podcast-max N` | `canvas_refresh.py` | Cap podcasts per run (default `PODCAST_MAX_PER_RUN`, else no cap) |
| `--discover` | `path_config.py` | List courses and intended folders without creating them |
| `--skip-prompt-regen` | `canvas_refresh.py` | Don't mark notes stale just because the prompt file changed |
| `--list DATE COURSE` | `canvas_readings.py` | Preview reading links without downloading |
| `--dry-run` | `calendar_sync.py` | Print events that would be created, don't create them |

---

## Notes on cost

Notes generation calls the Claude API. A typical session with 3–4 PDFs costs roughly $0.20–$0.60 depending on reading length. The daily run only regenerates notes that are actually stale (by content hash, so a fresh checkout does not trigger a rebuild), so costs are low after the initial setup run.

The model and its per-token prices live in [`scripts/ai_config.py`](scripts/ai_config.py) — one place to change both, so the printed cost estimate stays honest. Default is `claude-sonnet-5` (a third cheaper per token than Sonnet 4.6); swap in `claude-haiku-4-5` to cut cost or `claude-opus-5` for harder analytical courses.

---

## Running in the cloud (no Mac needed)

Everything except two Mac-only pieces (Calendar.app and the NotebookLM browser login) can run in a GitHub Actions job on a schedule, committing its output to a **private data repo**. Your Mac then only *mirrors* that repo into a local or iCloud folder when it happens to be awake — and fills in podcasts if the cloud's NotebookLM login has expired.

**Shape**

```
ObinnaI/hbs-course-helper   public fork — the code (this repo)
ObinnaI/hbs-coursework-2026 private — the workflow + every generated file, one repo per academic year
~/hbs-coursework-2026       clone of the data repo on the Mac (outside iCloud)
~/Library/…/HBS/Classes/2026   iCloud folder the mirror job copies into
```

The workflow lives in the *data* repo: forks have scheduled workflows off by default and public repos lose schedules after 60 idle days, while the data repo gets a commit every run. It checks out this code repo at `main` and runs `canvas_refresh.py` with:

| Variable | Meaning |
|---|---|
| `COURSEWORK_ROOT=$GITHUB_WORKSPACE` | the data repo checkout *is* the coursework folder |
| `CANVAS_CONFIG_FILE=…/claude/canvas_config.json` | config persists in the data repo, not the throwaway code checkout |
| `CALENDAR_BACKEND=ics` | write `claude/canvas.ics`; a later step publishes it to a secret Gist |
| `NOTEBOOKLM_AUTH_JSON` (secret) | the contents of `~/.notebooklm/profiles/default/storage_state.json` after `notebooklm login` |

Secrets: `CANVAS_API_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token` on the Mac; one year), `GIST_TOKEN` (a classic PAT with only the `gist` scope), `NOTEBOOKLM_AUTH_JSON`. Variables: `CANVAS_BASE_URL`, `GIST_ID`, `NOTES_MODEL`, `NOTES_MAX_PER_RUN`, `PODCAST_MAX_PER_RUN`. The full workflow is in the data repo's `.github/workflows/refresh.yml`; it installs Claude Code on the runner and warns when notes were skipped for a login or usage-limit reason, and when the number of courses Canvas returns changes (the January nudge).

**Bootstrap once, locally**, so the first cloud run finds the folder names you want:

```bash
./.venv/bin/python scripts/path_config.py --discover      # with COURSEWORK_ROOT / CANVAS_CONFIG_FILE pointing at ~/hbs-coursework
# edit ~/hbs-coursework/claude/canvas_config.json (abbrev_overrides, ignored_courses, folder_name), re-run, then commit and push
gh -R ObinnaI/hbs-coursework-2026 workflow run refresh.yml -f mode=daily -f podcasts=false
```

**Podcasts** run in the cloud with the stored login. Google expires that cookie every few weeks; when it does the job logs a warning and writes `claude/podcast_status.json`, the Mac mirror job generates the missing episodes with its own login, and you refresh the secret when convenient:

```bash
./.venv/bin/notebooklm login
gh -R ObinnaI/hbs-coursework-2026 secret set NOTEBOOKLM_AUTH_JSON < ~/.notebooklm/profiles/default/storage_state.json
```

**Mac mirror** — `./setup.sh --mirror` installs a launchd job that every 30 minutes (and on login) pushes any ratings you entered in the iCloud trackers and any new file you dropped into a class folder or the materials shelf, pulls, rsyncs the clone into `MIRROR_DEST` without deleting anything, and runs the podcast fallback. Log: `~/Library/Logs/hbs-mirror.log`. If it reports `Operation not permitted` on the iCloud path, grant Full Disk Access to `/bin/bash` in System Settings → Privacy & Security.

**Calendar** — subscribe once (Calendar → File → New Calendar Subscription, location iCloud, refresh hourly) to `https://gist.githubusercontent.com/<user>/<GIST_ID>/raw/canvas.ics`. A secret Gist is unlisted, not private: anyone with the URL can read assignment titles.

**Spring and later terms** — when Canvas publishes the next term's courses (mid-January for spring), discovery files them under `Spring/` from the term name, a `Spring/Participation Tracker.xlsx` appears once a class is posted, and courses whose term ended more than 30 days ago stop being polled. The one manual step worth keeping: run `path_config.py --discover`, check the TERM column, and set `abbrev_overrides` before the first cheat sheets are generated (abbreviations are baked into file names and prompt names). `term_folders` / `term_ends` in `canvas_config.json` override a term Canvas names unhelpfully.

**Size** — a term's podcasts are ~1.2 GB and everything else a few hundred MB, all well under GitHub's per-file limits. Start a new data repo each academic year (`hbs-coursework-2027`).

---

## Sharing this with classmates

The repo is public and MIT licensed — anyone can clone it. Two things worth saying out loud when you pass it along:

- **Everyone brings their own keys.** Canvas tokens and Anthropic keys are per-person and go in a gitignored `.env`. Nobody shares an account, and nobody should paste a token into a chat.
- **Course materials stay put.** The reading downloader fetches HBSP cases and articles through your own coursepack access, to your own machine. Those PDFs are licensed to you individually — don't redistribute the downloaded files, and don't commit a Coursework folder to git.

---

## License

MIT — see [LICENSE](LICENSE).
