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
CCDECK_PORT=9000 python3 ccdeck.py   # env knobs: CCDECK_HOST, CCDECK_PORT, CCDECK_NTFY_TOPIC, CCDECK_BELL, CCDECK_ALERT_IDLE
docker compose up -d --build         # same board, containerised, restart: always
```

`CCDECK_HOST` defaults to `127.0.0.1` and exists only so the container can bind
`0.0.0.0`; the compose file publishes back onto host loopback, so the hook URL is
identical either way. Three container details are load-bearing and easy to strip as noise:

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
