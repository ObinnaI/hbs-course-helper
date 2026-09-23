#!/usr/bin/env bash
# setup.sh — one-time setup. Safe to re-run; it skips anything already done.
#
#   ./setup.sh              install deps, create .env
#   ./setup.sh --schedule   also install the 5pm daily / Sunday 8am launchd jobs
#                           (everything runs on this Mac)
#   ./setup.sh --mirror     instead install the every-30-min mirror job that
#                           copies a cloud-run data repo into your iCloud folder
#                           and makes podcasts the cloud couldn't (see README)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

say()  { printf "\n\033[1m%s\033[0m\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }

# ── 1. Python ────────────────────────────────────────────────────────────────
say "1/5  Checking Python"
PY=""
for candidate in python3.12 python3.13 python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
            PY="$(command -v "$candidate")"; break
        fi
    fi
done
if [ -z "$PY" ]; then
    warn "Python 3.12+ not found."
    echo "  Install it, then re-run this script:"
    echo "      brew install python@3.12"
    echo "  (No Homebrew? Get the installer at https://www.python.org/downloads/)"
    exit 1
fi
ok "Using $PY ($("$PY" -V))"

# ── 2. Virtualenv + dependencies ─────────────────────────────────────────────
say "2/5  Installing dependencies"
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
ok "Python packages installed"

# Reading downloads drive a headless browser, which ships as a separate binary.
./.venv/bin/python -m playwright install chromium >/dev/null 2>&1 \
    && ok "Headless Chromium installed" \
    || warn "Chromium install failed — reading downloads will not work. Try: ./.venv/bin/python -m playwright install chromium"

# ── 3. Credentials ───────────────────────────────────────────────────────────
say "3/5  Credentials"
if [ -f .env ]; then
    ok ".env already exists — leaving it alone"
else
    cp .env.example .env
    ok "Created .env from the template"
    warn "Open .env and fill in your three values before running anything."
fi

# ── 4. Coursework folder ─────────────────────────────────────────────────────
say "4/5  Coursework folder"
ROOT="$(grep -E '^COURSEWORK_ROOT=' .env | head -1 | cut -d= -f2- | tr -d '"'"'" || true)"
ROOT="${ROOT/#\~/$HOME}"
if [ -n "$ROOT" ] && [ -d "$ROOT" ]; then
    ok "Found $ROOT"
elif [ -n "$ROOT" ]; then
    mkdir -p "$ROOT" && ok "Created $ROOT"
else
    warn "COURSEWORK_ROOT is not set in .env — set it before your first run."
fi

# ── 5. Scheduled jobs ────────────────────────────────────────────────────────
say "5/5  Scheduled jobs"
AGENTS="$HOME/Library/LaunchAgents"
install_job() {   # install_job LABEL
    sed -e "s|__PYTHON__|$REPO/.venv/bin/python3|g" -e "s|__REPO__|$REPO|g" \
        -e "s|__HOME__|$HOME|g" \
        "launchd/$1.plist.template" > "$AGENTS/$1.plist"
    launchctl unload "$AGENTS/$1.plist" 2>/dev/null || true
    launchctl load "$AGENTS/$1.plist"
    ok "Installed $1"
}
case "${1:-}" in
    --schedule)
        mkdir -p "$AGENTS"
        for job in daily weekly; do install_job "com.canvas-course-helper.$job"; done
        echo "  Daily runs at 5:00pm; weekly runs Sunday 8:00am."
        ;;
    --mirror)
        mkdir -p "$AGENTS" "$HOME/Library/Logs"
        chmod +x scripts/mirror.sh
        install_job "com.hbs-coursework.mirror"
        echo "  Mirrors every 30 minutes (and now). Log: ~/Library/Logs/hbs-mirror.log"
        echo "  Set MIRROR_CLONE / MIRROR_DEST in .env if the defaults don't fit."
        # Prove it actually runs: launchd has been seen to load a job and never
        # start it, and a bare bash cannot read iCloud Drive until you allow it.
        LOG="$HOME/Library/Logs/hbs-mirror.log"; : > "$LOG"
        launchctl kickstart -k "gui/$(id -u)/com.hbs-coursework.mirror" 2>/dev/null || true
        for _ in $(seq 1 20); do [ -s "$LOG" ] && break; sleep 1; done
        if [ ! -s "$LOG" ]; then
            warn "The mirror job did not start. Check: launchctl print gui/$(id -u)/com.hbs-coursework.mirror"
        elif grep -q "Operation not permitted\|privacy block" "$LOG"; then
            warn "macOS blocked access to iCloud Drive for /bin/bash."
            echo "  System Settings → Privacy & Security → Full Disk Access → + → ⌘⇧G → /bin/bash"
            echo "  then: launchctl kickstart -k gui/$(id -u)/com.hbs-coursework.mirror"
        else
            ok "Mirror job ran: $(tail -1 "$LOG")"
        fi
        ;;
    *)
        echo "  Skipped. Re-run as './setup.sh --schedule' (run everything on this Mac)"
        echo "  or './setup.sh --mirror' (mirror a cloud-run data repo into iCloud)."
        ;;
esac

say "Done."
cat <<'NEXT'
  Next steps:
    1. Fill in .env  (Canvas token, Canvas URL, Anthropic API key, folder path)
    2. Try one session:
         ./.venv/bin/python scripts/canvas_refresh.py --daily
NEXT
