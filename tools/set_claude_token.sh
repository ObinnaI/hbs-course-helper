#!/bin/bash
# Make a 1-year Claude Code token and store it as the data repo's
# CLAUDE_CODE_OAUTH_TOKEN secret, without copying it by hand: the token is
# printed across two lines and copy-paste tends to catch half of it.
#   tools/set_claude_token.sh [owner/repo]
set -euo pipefail
REPO="${1:-ObinnaI/hbs-coursework-2026}"
f=$(mktemp); chmod 600 "$f"; trap 'rm -f "$f"' EXIT
script -q "$f" claude setup-token
t=$(sed 's/\x1b\[[0-9;?]*[A-Za-z]//g' "$f" | tr -d '\r' \
    | sed -n '/OAuth token/,/Store this token/{/OAuth token/d;/Store this token/d;p;}' \
    | tr -d '[:space:]' | grep -o 'sk-ant-oat01-[A-Za-z0-9_-]*' || true)
if [[ ${#t} -lt 90 ]]; then
    echo "Could not find the token in the output (${#t} chars). Nothing was stored."; exit 1
fi
printf %s "$t" | gh -R "$REPO" secret set CLAUDE_CODE_OAUTH_TOKEN
echo "Stored a ${#t}-character token. It was not saved anywhere else."
