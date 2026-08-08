# ccdeck — one board for every Claude Code session

A single page that shows every Claude Code session running on your machine: which
project, what it's doing, how long it's been doing it, and which ones are blocked
waiting on you. Alerts fire in the browser, in the terminal, and optionally on your
phone.

No dependencies — Python 3 standard library only. Nothing leaves your machine
unless you turn on phone push.

## 1. Start the board

Either directly:

```bash
python3 ccdeck.py
# ccdeck listening on http://127.0.0.1:8787
```

or in Docker, which is the better bet if you want the board simply always there:

```bash
docker compose up -d
```

Same board, same URL either way. Open <http://127.0.0.1:8787> and click **Enable
alerts** once so the browser can raise desktop notifications, then leave the tab pinned.

### Keeping it running

The compose service is `restart: always`, so the board survives crashes and comes back
on its own the next time the Docker daemon starts — a reboot restores it with nothing
for you to do. Two conditions: Docker Desktop has to launch at login (**Settings →
General → Start Docker Desktop when you log in**), and a container only auto-starts if
it was running when Docker stopped. `docker compose down` is therefore also how you opt
out until you next bring it up.

Running the script directly, that part is yours to arrange — a LaunchAgent, or a VS Code
task you start once and forget.

### Working with the container

The port is published on host loopback only, so the hook URL in step 2 is unchanged.
Optional knobs go in a `.env` beside the compose file:

```bash
CCDECK_NTFY_TOPIC=di-ccdeck-<something-random>
CCDECK_HOST_PORT=8787     # host-side port, if 8787 is taken
CCDECK_ALERT_IDLE=0
```

```bash
docker compose logs -f          # the board's own output
docker compose up -d --build    # after editing ccdeck.py
docker compose down             # stop, and stop auto-starting
```

No volumes and no network beyond that one published port: the container serves the board
and cannot reach your projects. The paths on each strip are host paths the hooks report,
carried in the payload and displayed as text.

## 2. Wire Claude Code to it

Add this to `~/.claude/settings.json` (user scope = every project, every terminal).
If you already have a `hooks` block, merge the events in.

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "http", "url": "http://127.0.0.1:8787/hook", "timeout": 5 }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "http", "url": "http://127.0.0.1:8787/hook", "timeout": 5 }] }],
    "Notification":     [{ "hooks": [{ "type": "http", "url": "http://127.0.0.1:8787/hook", "timeout": 5 }] }],
    "Stop":             [{ "hooks": [{ "type": "http", "url": "http://127.0.0.1:8787/hook", "timeout": 5 }] }],
    "SubagentStop":     [{ "hooks": [{ "type": "http", "url": "http://127.0.0.1:8787/hook", "timeout": 5 }] }],
    "StopFailure":      [{ "hooks": [{ "type": "http", "url": "http://127.0.0.1:8787/hook", "timeout": 5 }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "http", "url": "http://127.0.0.1:8787/hook", "timeout": 5 }] }]
  }
}
```

Restart your Claude Code sessions, or run `/hooks` in a running one to confirm the
handlers are registered under **User Settings**.

Optional, for a live "running Bash…" line on each strip, add `PreToolUse` the same
way. It's one POST per tool call, so only turn it on if you want that detail.

### If the board is down

Hooks fail open. A connection refused is a non-blocking error: Claude Code logs it
and carries on. Your sessions never wait on this server.

### Locking the endpoint down

If you want to be explicit about which URLs hooks may call, add to the same file:

```json
{ "allowedHttpHookUrls": ["http://127.0.0.1:8787/hook"] }
```

## 3. Alerts

| Channel | How to turn it on |
|---|---|
| Browser notification + chime | Click **Enable alerts** on the board |
| Tab badge | Automatic — the title shows `(2) ccdeck` |
| Terminal bell / native toast | On by default. The server answers the hook with a `terminalSequence`, and Claude Code emits it for you — that's what makes the VS Code terminal tab light up, so you see *which* split needs you |
| Phone push | Set `CCDECK_NTFY_TOPIC=di-ccdeck-<something-random>` — exported before starting, or in the `.env` under Docker — then subscribe to that topic in the ntfy app |

Environment knobs:

- `CCDECK_PORT` — default `8787`
- `CCDECK_HOST` — bind address, default `127.0.0.1`. Exists so the container can bind
  `0.0.0.0` and still be published back onto host loopback; running directly, leave it
- `CCDECK_NTFY_TOPIC` — ntfy.sh topic, empty = no phone push
- `CCDECK_BELL` — `0` to stop returning terminal sequences
- `CCDECK_ALERT_IDLE` — `1` to also alert on the 60-second idle notification.
  Off by default: on several versions that event also fires after ordinary turns,
  which is how you end up ignoring your own alerts.

## What each state means

| State | Fired by | Meaning |
|---|---|---|
| `needs you` (amber) | `Notification` | Permission prompt or a question. This is the one that matters |
| `working` (teal) | `UserPromptSubmit` | Turn in flight, clock counting up |
| `finished` (green) | `Stop` | Turn done, last message on the strip |
| `failed` (red) | `StopFailure` | Turn ended on an API error — rate limit, overload, billing |
| `idle` | `SessionStart` | Session open, nothing running |

Strips sort by urgency, so anything waiting on you is always at the top.

## Endpoints

- `POST /hook` — where Claude Code sends events
- `GET /` — the board
- `GET /events` — server-sent event stream, if you'd rather build your own view
- `GET /api/state` — JSON snapshot, handy for a menu-bar widget or a tmux status line
