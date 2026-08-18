#!/usr/bin/env bash
#
# ccdeck usage probe - the three /usage bars, pushed onto the board.
#
# `claude /usage` gets its percentages from GET /api/oauth/usage, authenticated
# with your Claude subscription OAuth token. That token lives in the macOS
# keychain (or ~/.claude/.credentials.json elsewhere), which is why this cannot
# live inside ccdeck.py: the container has no keychain. So it runs on the host,
# makes that one call, and POSTs the answer to the board. Percentages go to
# ccdeck; the token goes nowhere but api.anthropic.com.
#
#   ./usage-probe.sh            push one report onto the board
#   ./usage-probe.sh --print    print the raw API response, push nothing
#   ./usage-probe.sh --watch    push every CCDECK_PROBE_EVERY seconds (30)
#
# CCDECK_URL takes more than one board, separated by spaces or commas - each
# ccdeck process keeps its bars in memory, so a second one on another port needs
# its own push or it shows nothing but token counts.
#
# Wire it to your turns instead of polling - see README, "The /usage bars".
#
# Exits 0 even when it fails, unless --print: this is meant to be safe to hang
# off a Claude Code hook, where a non-zero exit is noise in your session.

BOARDS="${CCDECK_URL:-http://127.0.0.1:8787}"
EVERY="${CCDECK_PROBE_EVERY:-30}"
MODE="${1:-}"

die() { [ "$MODE" = "--print" ] && { echo "ccdeck usage-probe: $1" >&2; exit 1; }; exit 0; }

read_token() {
  local raw
  if [ -f "$HOME/.claude/.credentials.json" ]; then
    raw=$(cat "$HOME/.claude/.credentials.json" 2>/dev/null)
  else
    raw=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)
  fi
  [ -n "$raw" ] || return 1
  printf '%s' "$raw" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
o = d.get("claudeAiOauth") or d
print(o.get("accessToken") or o.get("access_token") or "")'
}

probe_once() {
  local token report
  token=$(read_token) || die "no Claude credentials found"
  [ -n "$token" ] || die "credentials hold no access token"

  report=$(curl -sS --fail --max-time 10 \
    -H "Authorization: Bearer $token" \
    -H "Accept: application/json" \
    -H "anthropic-version: 2023-06-01" \
    https://api.anthropic.com/api/oauth/usage 2>/dev/null) || die "the usage endpoint refused the call (expired login? run /login)"

  if [ "$MODE" = "--print" ]; then
    printf '%s\n' "$report"
    return
  fi

  local pushed=0 board
  for board in $(printf '%s' "$BOARDS" | tr ',' ' '); do
    if curl -sS --fail --max-time 5 -H "Content-Type: application/json" \
         --data-binary "$report" "${board%/}/usage" >/dev/null 2>&1; then
      pushed=$((pushed + 1))
    fi
  done
  [ "$pushed" -gt 0 ] || die "no board answered on: $BOARDS"
}

if [ "$MODE" = "--watch" ]; then
  while :; do
    ( MODE=""; probe_once )
    sleep "$EVERY"
  done
else
  probe_once
fi
