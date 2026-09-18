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
#   4. If the last cloud run reported a NotebookLM login problem (or hasn't
#      reported in 36 h) and podcasts are pending, generate them here with the
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
[ -d "$CLONE/.git" ] || { echo "$LOG_PREFIX no clone at $CLONE — git clone the data repo there first"; exit 1; }
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

RSYNC_X=(--exclude .git --exclude .github --exclude .gitignore --exclude .trash --exclude .DS_Store --exclude '~$*' --exclude '*.icloud')

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

# ── 4. podcast fallback ───────────────────────────────────────────────────────
STATUS="$CLONE/claude/podcast_status.json"
need_podcasts() {
    [ -f "$STATUS" ] || return 1
    "$CODE/.venv/bin/python" - "$STATUS" <<'PY'
import json, sys, datetime as d
s = json.load(open(sys.argv[1]))
if not s.get("pending"): sys.exit(1)
try:
    age = d.datetime.now(d.timezone.utc) - d.datetime.fromisoformat(s["checked_at"])
except Exception:
    sys.exit(0)
sys.exit(0 if (not s.get("auth_ok")) or age > d.timedelta(hours=36) else 1)
PY
}
if [ -x "$CODE/.venv/bin/python" ] && need_podcasts; then
    echo "$LOG_PREFIX cloud could not make podcasts — generating here"
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
