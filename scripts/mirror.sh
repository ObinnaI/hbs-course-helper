#!/usr/bin/env bash
# mirror.sh — keep an iCloud (or any local) folder in step with the coursework
# data repo that the cloud workflow commits to, and fill in what the cloud
# could not do.
#
# Runs from launchd every 30 minutes (./setup.sh --mirror installs it). Each
# tick:
#   1. If the Participation Tracker in the mirror folder was edited since the
#      last tick, copy it into the clone and commit — ratings entered on the
#      Mac must reach the repo before the next weekly refresh rebuilds the sheet.
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
#   MIRROR_CLONE   path of the data-repo clone       default ~/hbs-coursework
#   MIRROR_DEST    folder to mirror into             default the iCloud Classes folder below
#   PODCAST_MAX_PER_RUN  episodes per tick           default 2

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

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

CLONE="$(envval MIRROR_CLONE "$HOME/hbs-coursework")"
DEST="$(envval MIRROR_DEST "$HOME/Library/Mobile Documents/com~apple~CloudDocs/2026 HBS/Classes")"
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

RSYNC_X=(--exclude .git --exclude .github --exclude .trash --exclude .DS_Store --exclude '~$*' --exclude '*.icloud')

sync_out() {   # clone → mirror folder; -u keeps a newer mirror copy
    rsync -au "${RSYNC_X[@]}" "$CLONE/" "$DEST/"
}

# ── 1. ratings entered in the mirror folder → repo ────────────────────────────
if [ -f "$DEST/$XLSX" ]; then
    m="$(stat -f %m "$DEST/$XLSX")"
    last="$(cat "$STATE/xlsx.mtime" 2>/dev/null || echo 0)"
    if [ "$m" != "$last" ] && ! cmp -s "$DEST/$XLSX" "$CLONE/$XLSX"; then
        cp "$DEST/$XLSX" "$CLONE/$XLSX"
        git add -- "$XLSX"
        git commit -qm "tracker: ratings entered on the Mac" && echo "$LOG_PREFIX committed tracker ratings" || true
    fi
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
stat -f %m "$DEST/$XLSX" > "$STATE/xlsx.mtime" 2>/dev/null || true

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
