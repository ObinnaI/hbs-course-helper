#!/usr/bin/env bash
# mirror.sh — keep an iCloud (or any local) folder in step with the coursework
# data repo that the cloud workflow commits to, and fill in what the cloud
# could not do.
#
# Runs from launchd every 30 minutes (./setup.sh --mirror installs it). Each
# tick:
#   1. If a Participation Tracker in the mirror folder was edited since the
#      last tick, copy it into the clone and commit — ratings entered on the
#      Mac must reach the repo before the next weekly refresh rebuilds the sheet.
#   1b. Copy up any NEW file in a class-day folder or on the materials shelf.
#   2. git pull --rebase (the Mac's commit wins any conflict), push if ahead.
#   3. rsync clone → mirror folder. Never deletes, never overwrites a mirror
#      file that is newer than the clone's copy.
#   4. If the last cloud run reported a NotebookLM login problem with podcasts
#      pending (or hasn't reported in 30 h), generate missing ones here with the
#      Mac's own login, commit, push, rsync again.
#
# The clone lives outside iCloud on purpose: iCloud Drive and a .git directory
# do not get along (partial syncs, "file.icloud" placeholders, duplicated refs).
#
# Settings (in the code repo's .env, or the environment):
#   MIRROR_CLONE   path of the data-repo clone       default ~/hbs-coursework-2026
#   MIRROR_DEST    folder to mirror into             default ~/Library/…/HBS/Classes/2026
#   PODCAST_MAX_PER_RUN  episodes per tick           default 2

set -euo pipefail
# launchd gives a bare PATH; the podcast fallback needs the venv only, but a
# local notes run needs the `claude` CLI (npm global or nvm).
export PATH="$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
for nvm_bin in "$HOME"/.nvm/versions/node/*/bin; do [ -d "$nvm_bin" ] && PATH="$PATH:$nvm_bin"; done

CODE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENVF="$CODE/.env"

envval() {   # envval KEY DEFAULT — environment, then .env, then default
    local v="${!1:-}"
    if [ -z "$v" ] && [ -f "$ENVF" ]; then
        v="$(grep -E "^$1=" "$ENVF" | head -1 | cut -d= -f2- | tr -d '"'"'" || true)"
    fi
    v="${v:-$2}"
    printf '%s' "${v/#\~/$HOME}"
}

CLONE="$(envval MIRROR_CLONE "$HOME/hbs-coursework-2026")"
DEST="$(envval MIRROR_DEST "$HOME/Library/Mobile Documents/com~apple~CloudDocs/HBS/Classes/2026")"
PODCAST_MAX="$(envval PODCAST_MAX_PER_RUN 2)"
XLSX="Participation Tracker.xlsx"
STATE="$HOME/.hbs-mirror"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M')]"

mkdir -p "$STATE"
date '+%Y-%m-%dT%H:%M:%S' > "$STATE/last_run"
[ -d "$CLONE/.git" ] || { echo "$LOG_PREFIX no clone at $CLONE — git clone the data repo there first"; exit 1; }
# launchd runs this as a bare /bin/bash, and macOS keeps iCloud Drive behind
# its privacy gate. Without Full Disk Access the first touch of $DEST fails
# with "Operation not permitted"; say so plainly instead of dying mid-rsync.
if ! ls "$(dirname "$DEST")" >/dev/null 2>&1; then
    echo "$LOG_PREFIX cannot read $(dirname "$DEST") — macOS privacy block."
    echo "$LOG_PREFIX fix: System Settings → Privacy & Security → Full Disk Access → + → ⌘⇧G → /bin/bash,"
    echo "$LOG_PREFIX then: launchctl kickstart -k gui/$(id -u)/com.hbs-coursework.mirror"
    exit 1
fi
mkdir -p "$DEST"

# ── lock (macOS has no flock; a directory mkdir is atomic). Stale after 3 h. ──
if ! mkdir "$STATE/lock" 2>/dev/null; then
    age=$(( $(date +%s) - $(stat -f %m "$STATE/lock") ))
    if [ "$age" -gt 10800 ]; then rmdir "$STATE/lock"; mkdir "$STATE/lock"; else exit 0; fi
fi
trap 'rmdir "$STATE/lock" 2>/dev/null || true' EXIT

cd "$CLONE"
git config user.name  >/dev/null || git config user.name  "hbs-mirror"
git config user.email >/dev/null || git config user.email "hbs-mirror@users.noreply.github.com"

RSYNC_X=(--exclude .git --exclude .github --exclude .gitignore --exclude .trash --exclude /inbox --exclude .DS_Store --exclude '~$*' --exclude '*.icloud')

sync_out() {   # clone → mirror folder; -u keeps a newer mirror copy
    rsync -au "${RSYNC_X[@]}" "$CLONE/" "$DEST/"
}

# ── 1. ratings entered in the mirror folder → repo ────────────────────────────
# One tracker per term folder (Fall/, Spring/), plus the old root one if any.
trackers() { for x in "$DEST/$XLSX" "$DEST"/*/"$XLSX"; do [ -f "$x" ] && printf '%s\n' "${x#$DEST/}"; done; return 0; }
mtime_key() { printf '%s' "$1" | tr '/ ' '__'; }
while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    m="$(stat -f %m "$DEST/$rel")"
    last="$(cat "$STATE/xlsx.$(mtime_key "$rel").mtime" 2>/dev/null || echo 0)"
    if [ "$m" != "$last" ] && ! cmp -s "$DEST/$rel" "$CLONE/$rel"; then
        mkdir -p "$(dirname "$CLONE/$rel")"
        cp "$DEST/$rel" "$CLONE/$rel"
        git add -- "$rel"
        git commit -qm "tracker: ratings entered on the Mac ($rel)" && echo "$LOG_PREFIX committed $rel" || true
    fi
done <<< "$(trackers)"

# ── 1b. files you added in the mirror folder → repo ──────────────────────────
# New files inside class-day folders and the course materials shelf are
# copied up (never overwriting, never deleting) so a PDF or sheet dropped in
# on the Mac reaches the cloud job on its next run. Quiz/, Course Docs/ and
# other folders of your own are left where they are.
if rsync -a --ignore-existing --prune-empty-dirs --max-size=95m \
        --exclude '.DS_Store' --exclude '*.icloud' --exclude '~$*' --exclude '.trash' \
        --exclude 'course_files_export*' --exclude '.git' \
        --include '*/' \
        --include '/*/*/[0-9][0-9][0-9][0-9][0-9][0-9] */***' \
        --include '/*/*/Course */***' --include '/*/*/General/***' \
        --include '/*/*/CLAUDE.md' \
        --include '/*/*/Quiz */' --include '/*/*/Quiz */*[Ss]tud*' --include '/*/*/Quiz */.*[Ss]tud*' \
        --exclude '*' \
        "$DEST/" "$CLONE/" 2>/dev/null; then
    git add -A
    if git commit -qm "mirror: files added on the Mac"; then echo "$LOG_PREFIX committed files added on the Mac"; fi
fi

# ── 2. pull, push ─────────────────────────────────────────────────────────────
if ! git pull --rebase -X theirs -q origin main; then
    git rebase --abort 2>/dev/null || true
    echo "$LOG_PREFIX pull failed (offline?) — will retry next tick"
    exit 0
fi
if [ -n "$(git log --oneline origin/main..HEAD 2>/dev/null)" ]; then
    git push -q origin main && echo "$LOG_PREFIX pushed"
fi

# ── 3. clone → mirror ─────────────────────────────────────────────────────────
sync_out
while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    stat -f %m "$DEST/$rel" > "$STATE/xlsx.$(mtime_key "$rel").mtime" 2>/dev/null || true
done <<< "$(trackers)"

echo "$LOG_PREFIX mirrored $(git rev-parse --short HEAD) → $DEST"

# ── 4. podcast fallback ───────────────────────────────────────────────────────
STATUS="$CLONE/claude/podcast_status.json"
# Step in when the cloud says its login failed and episodes are missing, or
# when it has not written a status for 30 h (a run that skipped the podcast step
# entirely, or no run at all). The pass skips episodes that exist, so a check
# with nothing to do costs a few seconds.
need_podcasts() {
    [ -f "$STATUS" ] || return 0
    "$CODE/.venv/bin/python" - "$STATUS" <<'PY'
import json, sys, datetime as d
try:
    s = json.load(open(sys.argv[1]))
    age = d.datetime.now(d.timezone.utc) - d.datetime.fromisoformat(s["checked_at"])
except Exception:
    sys.exit(0)
if age > d.timedelta(hours=30): sys.exit(0)
sys.exit(0 if (not s.get("auth_ok")) and s.get("pending") else 1)
PY
}
if [ -x "$CODE/.venv/bin/python" ] && need_podcasts; then
    echo "$LOG_PREFIX cloud could not make podcasts (or has not reported) — generating any missing here"
    COURSEWORK_ROOT="$CLONE" \
    CANVAS_CONFIG_FILE="$CLONE/claude/canvas_config.json" \
    CALENDAR_BACKEND=ics \
        "$CODE/.venv/bin/python" "$CODE/scripts/canvas_refresh.py" \
            --podcasts-only --podcast-days 7 --podcast-max "$PODCAST_MAX" || true
    git add -A
    if git commit -qm "podcasts: generated on the Mac"; then
        git pull --rebase -X theirs -q origin main && git push -q origin main && echo "$LOG_PREFIX pushed podcasts"
        sync_out
    fi
fi

# ── 5. keep the cloud's NotebookLM login fresh ───────────────────────────────
# Google's sign-in cookie lasts about a week, so the NOTEBOOKLM_AUTH_JSON secret
# the cloud runs with goes stale weekly. This Mac has its own NotebookLM login
# (notebooklm login, once). When the cloud reports its login failed, or the
# secret is older than MIRROR_NLM_MAX_AGE_DAYS (5), refresh this Mac's login
# (a non-passive check rotates the cookie) and, if it is valid, upload it as
# the secret with gh. Needs `gh auth login` on this Mac; nothing is uploaded
# unless the local login passes its own check, and at most once per 12 h.
NLM_STORAGE="$(envval NOTEBOOKLM_STORAGE "$HOME/.notebooklm/profiles/default/storage_state.json")"
NLM_MAX_AGE="$(envval MIRROR_NLM_MAX_AGE_DAYS 5)"
NLM_MARK="$STATE/nlm_secret_uploaded"
REPO_SLUG="$(git remote get-url origin 2>/dev/null | sed -E 's#^(https://github.com/|git@github.com:)##; s#\.git$##')"
nlm_upload_needed() {   # 0 when the cloud needs a fresh cookie and we have not just sent one
    [ -n "$REPO_SLUG" ] && command -v gh >/dev/null || return 1
    if [ -f "$NLM_MARK" ] && [ "$(( $(date +%s) - $(stat -f %m "$NLM_MARK") ))" -lt 43200 ]; then return 1; fi
    "$CODE/.venv/bin/python" - "$STATUS" "$NLM_MAX_AGE" "$(gh -R "$REPO_SLUG" secret list --json name,updatedAt 2>/dev/null)" <<'PY'
import json, sys, datetime as d
status, max_age, secrets = sys.argv[1], float(sys.argv[2]), sys.argv[3]
now = d.datetime.now(d.timezone.utc)
try:
    s = json.load(open(status))
    if not s.get("auth_ok") and now - d.datetime.fromisoformat(s["checked_at"]) < d.timedelta(hours=30):
        sys.exit(0)                      # the cloud said its login failed
except Exception:
    pass
try:
    for sec in json.loads(secrets or "[]"):
        if sec["name"] == "NOTEBOOKLM_AUTH_JSON":
            age = now - d.datetime.fromisoformat(sec["updatedAt"].replace("Z", "+00:00"))
            sys.exit(0 if age > d.timedelta(days=max_age) else 1)
except Exception:
    sys.exit(1)                          # could not read the secret list: do nothing
sys.exit(0)                              # no such secret yet
PY
}
if [ -x "$CODE/.venv/bin/notebooklm" ] && [ -f "$NLM_STORAGE" ] && nlm_upload_needed; then
    if "$CODE/.venv/bin/notebooklm" auth check --test >/dev/null 2>&1; then
        if gh -R "$REPO_SLUG" secret set NOTEBOOKLM_AUTH_JSON < "$NLM_STORAGE" >/dev/null 2>&1; then
            touch "$NLM_MARK"
            echo "$LOG_PREFIX uploaded this Mac's NotebookLM login as the cloud secret NOTEBOOKLM_AUTH_JSON"
        else
            echo "$LOG_PREFIX could not upload the NotebookLM login (is gh logged in? gh auth status)"
        fi
    else
        echo "$LOG_PREFIX this Mac's NotebookLM login is not valid either — run: $CODE/.venv/bin/notebooklm login"
    fi
fi
