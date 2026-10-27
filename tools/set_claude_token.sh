#!/bin/bash
# Make a 1-year Claude Code token and store it as the data repo's
# CLAUDE_CODE_OAUTH_TOKEN secret, without copying it by hand: the token is
# printed across two lines and copy-paste tends to catch half of it.
#   tools/set_claude_token.sh [owner/repo]
set -euo pipefail
REPO="${1:-ObinnaI/hbs-coursework-2026}"
f=$(mktemp); chmod 600 "$f"; trap 'rm -f "$f"' EXIT
script -q "$f" claude setup-token || true

# Find the token in the captured terminal output. Terminal control codes are
# stripped, and a token that wrapped onto a second line is joined back.
t=$(python3 - "$f" <<'PY'
import re, sys
raw = open(sys.argv[1], "rb").read().decode("utf-8", "replace")
# Claude Code draws the token with cursor moves rather than newlines: each line
# ends in a carriage return followed by "cursor down" (ESC [ n B). Turn both into
# line breaks before stripping the remaining escape sequences, or the two halves
# of the token (and the text after it) run together.
txt = re.sub(r"\x1b\[\d*B|\r", "\n", raw)
txt = re.sub(r"\x1b\[[^A-Za-z]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]", "", txt)
m = re.search(r"sk-ant-oat01-[A-Za-z0-9_-]+(?:[ \t]*\n(?:[ \t]*\n){0,3}[ \t]*([A-Za-z0-9_-]+)[ \t]*(?=\n))?", txt)
if m:
    print(re.sub(r"\s+", "", m.group(0)))
PY
)

if [[ ${#t} -lt 90 ]]; then
    # Keep a copy of the output with every long token-like run blanked, so the
    # parsing can be fixed without the token ever leaving this machine.
    mkdir -p "$HOME/.hbs-mirror"
    python3 - "$f" > "$HOME/.hbs-mirror/setup-token-debug.txt" <<'PY'
import re, sys
raw = open(sys.argv[1], "rb").read().decode("utf-8", "replace")
print(repr(re.sub(r"[A-Za-z0-9_-]{20,}", "<REDACTED>", raw)))
PY
    echo
    echo "Could not read the token from the output (a redacted copy of the output is in"
    echo "~/.hbs-mirror/setup-token-debug.txt). Select the whole token above, from"
    echo "sk-ant-oat01- to the end of its second line, copy it, paste it here and press"
    echo "Enter twice:"
    t=""
    while IFS= read -r line; do
        [[ -z "$line" ]] && break
        t+="${line//[[:space:]]/}"
    done
    if [[ $t != sk-ant-oat01-* || ${#t} -lt 90 ]]; then
        echo "That is not a token (${#t} characters, should start with sk-ant-oat01-). Nothing was stored."; exit 1
    fi
fi
printf %s "$t" | gh -R "$REPO" secret set CLAUDE_CODE_OAUTH_TOKEN
echo "Stored a ${#t}-character token. It was not saved anywhere else."
