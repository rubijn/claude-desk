# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ccdeck` — a local dashboard showing every Claude Code session running on the machine.
Claude Code pushes lifecycle events to it via HTTP hooks; the board shows one strip per
session and highlights the ones blocked waiting on the user.

The entire program is `ccdeck.py`. There is no build system, no dependency manifest, no
test suite, and no linter config. **Python 3 standard library only** — this is a deliberate
constraint, not an omission. Do not introduce third-party dependencies, and do not split
the program into modules unless asked.

## Running and exercising it

```bash
python3 ccdeck.py                    # serves http://127.0.0.1:8787
CCDECK_PORT=9000 python3 ccdeck.py   # env knobs: CCDECK_HOST, CCDECK_PORT, CCDECK_NTFY_TOPIC, CCDECK_BELL, CCDECK_ALERT_IDLE, CCDECK_TRANSCRIPTS, CCDECK_USAGE_EVERY, CCDECK_ALERT_LIMIT
docker compose up -d --build         # same board, containerised, restart: always
```

`CCDECK_HOST` defaults to `127.0.0.1` and exists only so the container can bind
`0.0.0.0`; the compose file publishes back onto host loopback, so the hook URL is
identical either way. The compose file's one volume — `~/.claude/projects:ro` — is what
makes the usage strip work in the container; without it `scan_usage()` returns
`{"ok": false}` and the strip hides itself. Three container details are load-bearing and
easy to strip as noise:

- `COPY --chmod=0644` — `ccdeck.py` is mode `0600` in this checkout and plain `COPY`
  preserves it, so `USER nobody` gets `Errno 13` and `restart: always` crash-loops.
- `init: true` — python as PID 1 has no SIGTERM handler, so the kernel drops the signal
  and every `compose down`/`restart` would burn the full 10s grace period.
- `PYTHONUNBUFFERED=1` — without it stdout block-buffers and `docker compose logs`
  stays empty.

Restarting the container clears the board: all state is in-memory, and sessions
repopulate on their next hook event.

There are no tests. Verify changes by driving the hook endpoint directly — this is the
fastest way to exercise any state transition without waiting for a real session:

```bash
curl -s localhost:8787/hook -d '{"hook_event_name":"UserPromptSubmit","session_id":"t1","cwd":"/tmp/demo","prompt":"hello"}'
curl -s localhost:8787/hook -d '{"hook_event_name":"Notification","session_id":"t1","notification_type":"permission_prompt","message":"Allow Bash?"}'
curl -s localhost:8787/api/state | python3 -m json.tool
```

The `/hook` response body is not decorative: when `CCDECK_BELL` is on, the server returns
`{"terminalSequence": "..."}` and Claude Code emits that escape sequence itself. That is
how the terminal bell and native toast happen — the server never writes to a terminal.

## Architecture

`ccdeck.py` is two halves in one file:

1. **Server** (top) — `handle_event()` reduces one hook payload into session state,
   `board_snapshot()` serializes it, `Handler` serves `POST /hook`, `GET /`,
   `GET /api/state`, and `GET /events` (SSE).
2. **UI** — the `PAGE` module-level raw string holds the whole front end (HTML + CSS + JS)
   inline. Editing the board means editing that string literal.

State lives in module globals: `SESSIONS` (session_id → dict), `FEED` (newest-first,
capped at 60), `SUBSCRIBERS` (one `queue.Queue` per SSE client). All reads and writes go
through `STATE_LOCK`; `handle_event` builds the snapshot *while still holding the lock*
and broadcasts after releasing it. Keep that ordering — a snapshot taken outside the lock
can tear.

Data flow: hook POST → `handle_event` mutates `SESSIONS` → snapshot → `broadcast()` fans
it out to every SSE queue → browser replaces its whole `state` and re-renders. The client
never fetches; `/api/state` exists only for external consumers (menu-bar widgets, tmux).
`render()` also runs on a 1s interval purely to tick the elapsed clocks, using the `skew`
it derives from the snapshot's server-side `now` — all timestamps are server epoch seconds.

### Statuses

Internal status names are `needs_you`, `working`, `done`, `error`, `idle`, `closed`, and
they are *not* the strings shown to users. Each one appears in four places that must stay
in sync when you add or rename one:

- the branch in `handle_event()` that sets it
- the urgency ranking dict in `board_snapshot()` (drives strip sort order)
- the `LABEL` map in the page JS
- the `.strip[data-s="…"]` CSS rules (rail colour, state colour, amber background)

`Notification` is the event that matters — it maps `notification_type` through
`ATTENTION_KINDS` to a label and raises an alert. `idle_prompt` is special-cased to update
the board without alerting, because on some Claude Code versions it also fires after
ordinary turns; `CCDECK_ALERT_IDLE=1` opts back in.

### Usage: two sources, one strip

The header shows whichever of two independent sources is available, preferring the first:

1. **`LIMITS`** — the real `/usage` percentages. `usage-probe.sh` (host-side, the one file
   in this repo that is not `ccdeck.py`) reads the subscription OAuth token from the macOS
   keychain, calls `GET /api/oauth/usage`, and POSTs the body to `POST /usage`, where
   `ingest_limits()` turns it into one slider per bar. It has to be a separate script
   because the container has no keychain, and it exits 0 on every failure so it is safe on
   a `Stop` hook. ccdeck never sees the token.

   Read `limits[]`, not the top-level windows: that array is what the panel itself draws —
   `kind` (`session`, `weekly_all`, `weekly_scoped`), `percent`, `severity` (which colours
   the bar), `resets_at`, and for a scoped bar the model name under
   `scope.model.display_name`. The per-model bar is *only* there: the top-level
   `seven_day_opus` / `seven_day_sonnet` keys read `null` even while the Fable bar sits at
   32%, and the response also carries a drift of internal codename keys
   (`nimbus_quill`, `tangelo`, …) that a generic `seven_day_*` sweep would have rendered as
   bars. `LEGACY_WINDOWS` keeps `five_hour`/`seven_day` as a fallback for when `limits` is
   absent. Windows close on `:59.999`, so the page rounds `resets_at` to the minute or the
   week bar reads a day early.
2. **`USAGE`** — local token counters, the fallback when nobody is probing.

`take_limits()` keeps the last good report rather than blanking the bars on a malformed
push. Both sources publish through the same snapshot-under-lock-then-`broadcast()` path.

`limit_alerts()` runs on the bars of every accepted report and returns the ones that just
crossed `CCDECK_ALERT_LIMIT` (default 80). They enter `FEED` as urgent and go out through
`push_phone()` — the point being that crossing 100% with extra usage enabled bills
silently instead of blocking, so the bars are the only warning. `_CROSSED` holds
`label -> (window id, already alerted)` so a bar alerts once per window rather than once
per push; the window id is `resets_at` floored to the minute, because the API returns it
with sub-second jitter that would otherwise re-arm the alert on every probe. Falling back
under the threshold also re-arms — that is what a mid-window reset looks like from here.

The token counters are the second data source: `usage_loop()` runs on its
own daemon thread every `CCDECK_USAGE_EVERY` seconds, `scan_usage()` walks
`CCDECK_TRANSCRIPTS` (`~/.claude/projects`) and totals the `message.usage` block of every
assistant line into three windows — the live 5h rate-limit block, a rolling 7 days, and
Fable within that week. It publishes `USAGE`, then reuses the same
snapshot-under-lock-then-`broadcast()` path as `handle_event`, so the board updates with
no hook traffic at all.

These are token counts, not the percentages `/usage` shows — those are server-side and
cached nowhere on disk. Deliberate consequences of reading the transcripts instead:

- `_READ` holds a per-file byte offset so each pass parses only appended bytes, and a
  trailing partial line is left for the next pass. `_SEEN` dedups on `id`+`requestId`,
  because resumed sessions and sidechains write the same assistant message twice.
- `_ENTRIES` is pruned to the 7-day window on every pass, and dropped entries release
  their `_SEEN` key with them — otherwise that set grows without bound.
- The 5h block starts at the top of the hour of the first message following a ≥5h gap,
  which is how the rate-limit window is displayed; `reset_at` is 0 when no block is live.

To exercise it without waiting, point it at a fixture directory:
`CCDECK_TRANSCRIPTS=/tmp/tx CCDECK_USAGE_EVERY=2 python3 ccdeck.py`, write a `.jsonl`
holding lines shaped `{"timestamp":…,"requestId":…,"message":{"id":…,"model":…,"usage":{…}}}`,
then read `/api/state`.

### Never block a session

Hooks are in the critical path of the user's Claude Code turn, so every failure mode is
absorbed: `do_POST` wraps `handle_event` in a bare `except` and still returns 200, bad JSON
degrades to `{}`, `push_phone()` fires in a daemon thread and swallows errors, and a full
SSE queue drops that subscriber rather than blocking the broadcast. Preserve this
posture — a bug in the board must never surface as an error in a session.

## Setup outside the repo

Wiring lives in the user's `~/.claude/settings.json` (`hooks` block pointing at
`http://127.0.0.1:8787/hook`, optionally `allowedHttpHookUrls`) — see README.md. Nothing
in this repo configures it, so a session that appears not to report is usually a hooks
registration problem, checkable with `/hooks` in a running session.
