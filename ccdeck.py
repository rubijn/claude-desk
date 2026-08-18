#!/usr/bin/env python3
"""
ccdeck - a live board for every Claude Code session you have running.

Start it once:      python3 ccdeck.py
Open the board:     http://127.0.0.1:8787

Claude Code pushes lifecycle events here through HTTP hooks (see README).
The board shows one strip per session: which project, what it is doing,
how long it has been doing it, and above all which ones are waiting on you.

Environment:
  CCDECK_HOST        bind address (default 127.0.0.1; use 0.0.0.0 in a container)
  CCDECK_PORT        listen port (default 8787)
  CCDECK_NTFY_TOPIC  ntfy.sh topic for phone push (optional)
  CCDECK_BELL        "1" to make Claude Code ring the terminal bell + fire a
                     native desktop notification on attention events (default 1)
  CCDECK_TRANSCRIPTS where to read token usage from (default ~/.claude/projects)
  CCDECK_USAGE_EVERY seconds between usage rescans (default 30; 0 disables)
"""

import json
import os
import queue
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("CCDECK_HOST", "127.0.0.1")
PORT = int(os.environ.get("CCDECK_PORT", "8787"))
NTFY_TOPIC = os.environ.get("CCDECK_NTFY_TOPIC", "").strip()
BELL = os.environ.get("CCDECK_BELL", "1") == "1"
ALERT_IDLE = os.environ.get("CCDECK_ALERT_IDLE", "0") == "1"
ALERT_LIMIT = float(os.environ.get("CCDECK_ALERT_LIMIT", "80"))   # 0 disables
TRANSCRIPTS = os.path.expanduser(os.environ.get("CCDECK_TRANSCRIPTS", "~/.claude/projects"))
USAGE_EVERY = int(os.environ.get("CCDECK_USAGE_EVERY", "30"))

STATE_LOCK = threading.Lock()
SESSIONS = {}          # session_id -> dict
FEED = []              # newest-first list of notable events, capped
SUBSCRIBERS = []       # SSE queues
USAGE = {"ok": False}  # last transcript scan, published in every snapshot
LIMITS = {"ok": False}  # last /api/oauth/usage report, pushed in by usage-probe.sh
_CROSSED = {}          # bar label -> (window id, already alerted) for limit_alerts

ATTENTION_KINDS = {
    "permission_prompt": "Permission needed",
    "agent_needs_input": "Question for you",
    "elicitation_dialog": "Input requested",
    "idle_prompt": "Waiting, idle",
}


# --------------------------------------------------------------------------
# event ingestion
# --------------------------------------------------------------------------

def project_of(cwd: str) -> str:
    cwd = (cwd or "").rstrip("/")
    return os.path.basename(cwd) or cwd or "unknown"


def broadcast(payload: dict) -> None:
    dead = []
    for q in SUBSCRIBERS:
        try:
            q.put_nowait(payload)
        except queue.Full:
            dead.append(q)
    for q in dead:
        if q in SUBSCRIBERS:
            SUBSCRIBERS.remove(q)


def push_phone(title: str, body: str, priority: str = "default") -> None:
    if not NTFY_TOPIC:
        return

    def _send():
        try:
            req = urllib.request.Request(
                "https://ntfy.sh/" + NTFY_TOPIC,
                data=body.encode("utf-8"),
                headers={"Title": title, "Priority": priority, "Tags": "robot"},
            )
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def handle_event(ev: dict) -> dict:
    """Update board state from one Claude Code hook payload. Returns the hook response."""
    name = ev.get("hook_event_name", "?")
    sid = ev.get("session_id") or "unknown"
    cwd = ev.get("cwd", "")
    now = time.time()
    response = {}

    with STATE_LOCK:
        s = SESSIONS.get(sid) or {
            "id": sid,
            "project": project_of(cwd),
            "cwd": cwd,
            "status": "idle",
            "since": now,
            "detail": "",
            "prompt": "",
            "turns": 0,
            "last_seen": now,
        }
        if cwd:
            s["cwd"] = cwd
            s["project"] = project_of(cwd)
        s["last_seen"] = now
        s["mode"] = ev.get("permission_mode", s.get("mode", ""))

        alert = None

        if name == "SessionStart":
            s.update(status="idle", since=now, detail="Session open", prompt="")

        elif name == "UserPromptSubmit":
            prompt = (ev.get("prompt") or "").strip().replace("\n", " ")
            s.update(
                status="working",
                since=now,
                detail="Working",
                prompt=prompt[:160],
                turns=s.get("turns", 0) + 1,
            )

        elif name == "Notification":
            kind = ev.get("notification_type") or ev.get("type") or ""
            msg = (ev.get("message") or "").strip().replace("\n", " ")
            label = ATTENTION_KINDS.get(kind, "Needs you")
            if kind == "idle_prompt" and not ALERT_IDLE:
                # idle_prompt also fires after ordinary turns on some versions,
                # so it stays on the board but never rings anything.
                if s["status"] not in ("needs_you", "error"):
                    s.update(status="idle", detail=msg[:160] or label)
            else:
                s.update(status="needs_you", since=now, detail=msg[:160] or label)
                alert = (label, s["project"], msg or label, "high")

        elif name in ("Stop", "SubagentStop"):
            if name == "SubagentStop":
                s.update(detail="Subagent finished")
            else:
                last = (ev.get("last_assistant_message") or "").strip().replace("\n", " ")
                s.update(status="done", since=now, detail=last[:200] or "Turn finished")
                alert = ("Finished", s["project"], last[:200] or "Turn finished", "default")

        elif name == "StopFailure":
            s.update(status="error", since=now,
                     detail="API error: " + str(ev.get("error_type", "unknown")))
            alert = ("Turn failed", s["project"], s["detail"], "high")

        elif name == "SessionEnd":
            s.update(status="closed", since=now, detail="Session ended")

        elif name == "PreToolUse":
            s["detail"] = "Running " + str(ev.get("tool_name", "tool"))
            if s["status"] not in ("needs_you",):
                s["status"] = "working"

        SESSIONS[sid] = s

        if alert:
            FEED.insert(0, {
                "t": now,
                "kind": alert[0],
                "project": alert[1],
                "text": alert[2],
                "session": sid,
                "urgent": alert[3] == "high",
            })
            del FEED[60:]

        snapshot = board_snapshot()

    if alert:
        push_phone(alert[1] + " - " + alert[0], alert[2], alert[3])
        if BELL:
            # Ask Claude Code to emit a desktop notification + bell for us.
            # OSC 777 covers Ghostty/WezTerm/urxvt; the BEL makes VS Code's
            # terminal badge the tab, which is what you actually notice.
            seq = "\033]777;notify;Claude Code - %s;%s\007\007" % (
                alert[1], alert[2][:120].replace(";", ","))
            response["terminalSequence"] = seq

    broadcast(snapshot)
    return response


# --------------------------------------------------------------------------
# token usage, read straight off the transcripts
#
# There is no local file holding the percentages the /usage panel shows - those
# come from the server. What is on disk is every assistant message Claude Code
# has ever written, each carrying its own `usage` block, so the board totals
# those instead: real tokens, no credentials, works from a read-only mount.
# --------------------------------------------------------------------------

BLOCK_SPAN = 5 * 3600      # Claude Code's rate-limit window, for "now"
WEEK_SPAN = 7 * 86400      # rolling, since the plan's reset weekday is unknown
TOKEN_KEYS = (("in", "input_tokens"), ("out", "output_tokens"),
              ("cache_w", "cache_creation_input_tokens"),
              ("cache_r", "cache_read_input_tokens"))

_READ = {}             # transcript path -> bytes already parsed
_ENTRIES = []          # [ts, model, {in,out,cache_w,cache_r}, dedup key]
_SEEN = set()          # dedup keys of everything in _ENTRIES


def _epoch(stamp) -> float:
    """ISO-8601 out of a transcript line -> epoch seconds, 0 if unparseable."""
    if not isinstance(stamp, str):
        return 0.0
    try:
        dt = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parse_line(line: bytes):
    if b'"usage"' not in line:
        return None
    try:
        d = json.loads(line)
    except Exception:
        return None
    msg = d.get("message")
    if not isinstance(msg, dict):
        return None
    use = msg.get("usage")
    if not isinstance(use, dict):
        return None
    ts = _epoch(d.get("timestamp"))
    if not ts:
        return None
    # The same assistant message shows up twice whenever a session is resumed
    # or forked into a sidechain; id+requestId is what makes it one message.
    key = "%s/%s" % (msg.get("id") or "", d.get("requestId") or "")
    if key == "/" or key in _SEEN:
        return None
    counts = {}
    for short, full in TOKEN_KEYS:
        try:
            counts[short] = int(use.get(full) or 0)
        except (TypeError, ValueError):
            counts[short] = 0
    return [ts, str(msg.get("model") or "unknown"), counts, key]


def _ingest(path: str, size: int) -> None:
    """Parse only the bytes appended since the last scan."""
    start = _READ.get(path, 0)
    if size == start:
        return
    if size < start:       # truncated or replaced - start over on this file
        start = 0
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            raw = fh.read()
    except OSError:
        return
    cut = raw.rfind(b"\n")
    if cut < 0:            # a half-written line; pick it up next time round
        return
    _READ[path] = start + cut + 1
    for line in raw[:cut].split(b"\n"):
        entry = _parse_line(line)
        if entry:
            _ENTRIES.append(entry)
            _SEEN.add(entry[3])


def _prune(now: float) -> None:
    keep, cutoff = [], now - WEEK_SPAN
    for entry in _ENTRIES:
        if entry[0] >= cutoff:
            keep.append(entry)
        else:
            _SEEN.discard(entry[3])
    _ENTRIES[:] = keep


def _window(entries) -> dict:
    out = {"total": 0}
    for short, _full in TOKEN_KEYS:
        out[short] = 0
    for _ts, _model, counts, _key in entries:
        for short, _full in TOKEN_KEYS:
            out[short] += counts[short]
            out["total"] += counts[short]
    return out


def scan_usage() -> dict:
    """Totals for the live 5h block, the last 7 days, and Fable within them."""
    if not os.path.isdir(TRANSCRIPTS):
        return {"ok": False, "reason": "no transcripts at " + TRANSCRIPTS}
    now = time.time()
    stale = now - WEEK_SPAN - 86400
    for root, _dirs, names in os.walk(TRANSCRIPTS):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < stale and path not in _READ:
                continue
            _ingest(path, st.st_size)
    _prune(now)
    entries = sorted(_ENTRIES, key=lambda e: e[0])

    # Rate-limit windows open on the first message after a >=5h gap and are
    # shown from the top of that hour, so that is how the block is bounded.
    block_start, prev = None, None
    for entry in entries:
        ts = entry[0]
        if block_start is None or ts - block_start >= BLOCK_SPAN or ts - prev >= BLOCK_SPAN:
            block_start = ts - (ts % 3600)
        prev = ts
    reset_at = block_start + BLOCK_SPAN if block_start else 0
    live = reset_at > now

    week = [e for e in entries if e[0] >= now - WEEK_SPAN]
    return {
        "ok": True,
        "at": now,
        "now": _window([e for e in week if live and e[0] >= block_start]),
        "week": _window(week),
        "fable": _window([e for e in week if "fable" in e[1].lower()]),
        "reset_at": reset_at if live else 0,
        "messages": len(week),
    }


# The three bars `/usage` draws come from GET /api/oauth/usage, which needs the
# subscription OAuth token - keychain-only on macOS, so unreachable from the
# container. usage-probe.sh runs on the host, makes that one call and POSTs the
# answer here; percentages come in, the token never does.

# `limits` is the list the panel itself renders: one entry per bar, already
# labelled by kind and scope. The top-level five_hour/seven_day objects carry the
# same session and week numbers, so they stay as a fallback shape - but the
# per-model bar exists only in `limits`, under scope.model.display_name.
LIMIT_KINDS = {
    "session": ("Current session", "now"),
    "weekly_all": ("Current week (all models)", "week"),
}
LEGACY_WINDOWS = (("five_hour", "Current session", "now"),
                  ("seven_day", "Current week (all models)", "week"))


def _reset_epoch(value) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        value = float(value)
        return value / 1000.0 if value > 1e11 else value    # ms or s
    return _epoch(value)


def _bar(label, join, pct, resets, severity="", active=False) -> dict:
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        pct = 0.0
    return {
        "label": label,
        "join": join,                                       # local token window
        "pct": max(0.0, min(100.0, round(pct, 1))),
        "resets_at": _reset_epoch(resets),
        "severity": str(severity or ""),
        "active": bool(active),
    }


def _scoped_label(entry: dict):
    """A scoped bar names its model in `scope`, not in `kind`."""
    scope = entry.get("scope") or {}
    name = ""
    for part in (scope.get("model"), scope.get("surface")):
        shown = part.get("display_name") if isinstance(part, dict) else None
        if isinstance(shown, str) and shown.strip():
            name = shown.strip()
            break
    if not name:
        name = str(entry.get("kind") or "scoped").replace("_", " ").title()
    period = "Current session" if entry.get("group") == "session" else "Current week"
    return "%s (%s)" % (period, name), ("fable" if "fable" in name.lower() else "")


def ingest_limits(report: dict) -> dict:
    """Reduce one /api/oauth/usage body into the board's slider list."""
    bars = []
    entries = report.get("limits")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("percent") is None:
                continue
            known = LIMIT_KINDS.get(entry.get("kind"))
            label, join = known if known else _scoped_label(entry)
            bars.append(_bar(label, join, entry.get("percent"), entry.get("resets_at"),
                             entry.get("severity"), entry.get("is_active")))
    if not bars:
        for key, label, join in LEGACY_WINDOWS:
            window = report.get(key)
            if isinstance(window, dict) and window.get("utilization") is not None:
                bars.append(_bar(label, join, window.get("utilization"),
                                 window.get("resets_at")))
    return {"ok": bool(bars), "at": time.time(), "bars": bars}


def _limit_text(bar: dict) -> str:
    when = bar.get("resets_at") or 0
    if when:
        fmt = "%H:%M" if when - time.time() < 20 * 3600 else "%a %H:%M"
        at = time.strftime(fmt, time.localtime(when))
    else:
        at = "?"
    return "%.0f%% used, resets %s. Past 100%% bills as extra usage." % (bar["pct"], at)


def limit_alerts(bars: list) -> list:
    """Bars that just crossed CCDECK_ALERT_LIMIT, at most once per window.

    The probe pushes the same report every 30s, so a bar sitting at 85% must not
    alert 120 times an hour. A bar is armed again when its window rolls over -
    keyed on resets_at rounded to the minute, because the API returns it with
    sub-second jitter - or when it falls back under the threshold, which is what
    a mid-window reset looks like from here.
    """
    if ALERT_LIMIT <= 0:
        return []
    crossed = []
    for bar in bars:
        label = bar["label"]
        window = int((bar.get("resets_at") or 0) // 60)
        seen, fired = _CROSSED.get(label, (window, False))
        if window != seen:
            fired = False
        if bar["pct"] < ALERT_LIMIT:
            fired = False
        elif not fired:
            fired = True
            crossed.append(bar)
        _CROSSED[label] = (window, fired)
    return crossed


def usage_loop() -> None:
    while True:
        try:
            panel = scan_usage()          # file IO stays outside the lock
        except Exception as exc:
            print("ccdeck usage: %s" % exc)
            panel = {"ok": False, "reason": str(exc)}
        global USAGE
        with STATE_LOCK:
            USAGE = panel
            snapshot = board_snapshot()
        broadcast(snapshot)
        time.sleep(USAGE_EVERY)


def board_snapshot() -> dict:
    sessions = sorted(
        SESSIONS.values(),
        key=lambda s: (
            {"needs_you": 0, "error": 1, "done": 2, "working": 3, "idle": 4, "closed": 5}
            .get(s["status"], 6),
            -s["since"],
        ),
    )
    return {
        "now": time.time(),
        "sessions": sessions,
        "feed": FEED[:30],
        "usage": USAGE,
        "limits": LIMITS,
        "waiting": sum(1 for s in sessions if s["status"] in ("needs_you", "error")),
        "done": sum(1 for s in sessions if s["status"] == "done"),
    }


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.rstrip("/") or "/"
        if path not in ("/hook", "/usage"):
            self._send(404, b'{"error":"not found"}')
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        out = {}
        try:
            if path == "/usage":
                out = self.take_limits(body)
            else:
                out = handle_event(body)
        except Exception as exc:  # never let a board bug block a session
            print("ccdeck: %s" % exc)
            out = {}
        self._send(200, json.dumps(out).encode())

    def take_limits(self, body: dict) -> dict:
        global LIMITS
        report = ingest_limits(body if isinstance(body, dict) else {})
        crossed = []
        with STATE_LOCK:
            if report["ok"] or not LIMITS.get("ok"):
                LIMITS = report                 # keep the last good report
                crossed = limit_alerts(report["bars"])
            for bar in crossed:
                FEED.insert(0, {
                    "t": time.time(),
                    "kind": "Usage %d%%" % round(bar["pct"]),
                    "project": bar["label"],
                    "text": _limit_text(bar),
                    "session": "limit:" + bar["label"],
                    "urgent": True,
                })
            if crossed:
                del FEED[60:]
            snapshot = board_snapshot()
        broadcast(snapshot)
        for bar in crossed:
            push_phone("Claude usage - " + bar["label"], _limit_text(bar), "high")
        return {"bars": len(report["bars"]), "alerts": len(crossed)}

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            with STATE_LOCK:
                self._send(200, json.dumps(board_snapshot()).encode())
        elif path == "/events":
            self.stream()
        else:
            self._send(404, b"not found", "text/plain")

    def stream(self):
        q = queue.Queue(maxsize=64)
        SUBSCRIBERS.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            with STATE_LOCK:
                first = board_snapshot()
            self.wfile.write(b"data: " + json.dumps(first).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15)
                    chunk = b"data: " + json.dumps(payload).encode() + b"\n\n"
                except queue.Empty:
                    chunk = b": ping\n\n"
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            if q in SUBSCRIBERS:
                SUBSCRIBERS.remove(q)


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ccdeck</title>
<style>
  :root{
    --board:#0d1116; --strip:#151b22; --edge:#232c36; --ink:#c9d4de; --dim:#6b7c8c;
    --caution:#ffb000;   /* attention: avionics amber */
    --advisory:#00d1c1;  /* running */
    --ok:#4ade80;        /* finished */
    --warn:#ff5f56;      /* failed */
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--board);color:var(--ink);
    font:13px/1.5 ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace;
    padding:22px 20px 60px;-webkit-font-smoothing:antialiased}
  header{display:flex;align-items:baseline;gap:18px;flex-wrap:wrap;
    border-bottom:1px solid var(--edge);padding-bottom:14px;margin-bottom:22px}
  h1{font-size:12px;letter-spacing:.32em;text-transform:uppercase;color:var(--dim);
    font-weight:500;margin:0}
  .count{font-size:38px;line-height:1;letter-spacing:-.03em;color:var(--caution)}
  .count.clear{color:var(--dim)}
  .count small{font-size:11px;letter-spacing:.18em;text-transform:uppercase;
    color:var(--dim);margin-left:8px;vertical-align:middle}
  .usage{display:flex;gap:18px;flex-wrap:wrap;align-items:baseline;
    padding-left:18px;border-left:1px solid var(--edge)}
  .usage span{display:block;font-size:9px;letter-spacing:.18em;text-transform:uppercase;
    color:var(--dim);white-space:nowrap}
  .usage b{font-weight:400;font-size:15px;color:var(--ink);
    font-variant-numeric:tabular-nums;letter-spacing:-.01em}
  .usage .cold b{color:var(--dim)}
  .usage.bars{display:grid;gap:5px;gap:5px 0}
  .bar{display:grid;grid-template-columns:auto 118px 56px auto;gap:0 11px;
    align-items:center;font-size:10px;letter-spacing:.06em}
  .bar .lab{text-transform:uppercase;color:var(--dim);white-space:nowrap}
  .bar .track{height:7px;background:#1c242d;border:1px solid var(--edge)}
  .bar .fill{display:block;height:100%;background:var(--advisory);
    transition:width .4s ease}
  .bar[data-hot="warn"] .fill{background:var(--caution)}
  .bar[data-hot="crit"] .fill{background:var(--warn)}
  .bar .pct{color:var(--ink);font-size:11px;font-variant-numeric:tabular-nums;
    text-align:right}
  .bar .when{color:var(--dim);white-space:nowrap}
  .bar.stale .fill{opacity:.45}
  .tools{margin-left:auto;display:flex;gap:8px}
  button{background:transparent;border:1px solid var(--edge);color:var(--dim);
    font:inherit;font-size:11px;letter-spacing:.1em;text-transform:uppercase;
    padding:6px 11px;border-radius:2px;cursor:pointer}
  button:hover,button:focus-visible{border-color:var(--dim);color:var(--ink)}
  .strip{display:grid;grid-template-columns:8px 200px 1fr 92px;gap:0;
    background:var(--strip);border:1px solid var(--edge);border-left:none;
    margin-bottom:6px;align-items:stretch;overflow:hidden}
  .rail{background:var(--dim)}
  .strip[data-s="needs_you"] .rail{background:var(--caution)}
  .strip[data-s="working"]   .rail{background:var(--advisory)}
  .strip[data-s="done"]      .rail{background:var(--ok)}
  .strip[data-s="error"]     .rail{background:var(--warn)}
  .strip[data-s="needs_you"]{border-color:#4a3a10;background:#1c1808}
  .who{padding:12px 16px;border-right:1px solid var(--edge);min-width:0}
  .proj{font-size:14px;color:#e8eef4;letter-spacing:-.01em;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .path{font-size:10px;color:var(--dim);white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis;direction:rtl;text-align:left}
  .what{padding:12px 16px;min-width:0}
  .state{font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--dim)}
  .strip[data-s="needs_you"] .state{color:var(--caution)}
  .strip[data-s="working"] .state{color:var(--advisory)}
  .strip[data-s="done"] .state{color:var(--ok)}
  .strip[data-s="error"] .state{color:var(--warn)}
  .detail{color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    margin-top:2px}
  .prompt{color:var(--dim);font-size:11px;white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis;margin-top:2px}
  .clock{padding:12px 16px;border-left:1px solid var(--edge);text-align:right;
    color:var(--dim);font-variant-numeric:tabular-nums}
  .clock b{display:block;color:var(--ink);font-weight:400;font-size:15px}
  .empty{color:var(--dim);padding:40px 0;text-align:center}
  h2{font-size:10px;letter-spacing:.28em;text-transform:uppercase;color:var(--dim);
    font-weight:500;margin:34px 0 10px}
  .log{border-top:1px solid var(--edge)}
  .log div{display:grid;grid-template-columns:64px 150px 1fr;gap:14px;
    padding:6px 0;border-bottom:1px solid #1a222b;color:var(--dim);font-size:11px}
  .log b{font-weight:400;color:var(--ink)}
  .log .u b{color:var(--caution)}
  .log span:last-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  @media (max-width:700px){
    .strip{grid-template-columns:6px 1fr 76px}
    .who{grid-column:2;border-right:none;padding-bottom:0}
    .what{grid-column:2;padding-top:4px}
    .clock{grid-row:1/3;grid-column:3}
  }
</style></head><body>
<header>
  <h1>ccdeck</h1>
  <div class="count clear" id="count">0<small id="countlabel">waiting on you</small></div>
  <div class="usage" id="usage" style="display:none"></div>
  <div class="tools">
    <button id="notify">Enable alerts</button>
    <button id="sound" aria-pressed="true">Sound on</button>
  </div>
</header>
<div id="board"></div>
<h2>Recent</h2>
<div class="log" id="log"></div>
<script>
const LABEL={needs_you:"needs you",working:"working",done:"finished",idle:"idle",
             error:"failed",closed:"closed"};
let soundOn=true, lastAlert=0, skew=0;
const el=id=>document.getElementById(id);

function ago(sec){sec=Math.max(0,Math.round(sec));
  if(sec<60)return sec+"s";
  const m=Math.floor(sec/60);if(m<60)return m+"m "+(sec%60)+"s";
  return Math.floor(m/60)+"h "+(m%60)+"m";}

function tokens(n){n=n||0;
  if(n>=1e9)return (n/1e9).toFixed(2)+"B";
  if(n>=1e6)return (n/1e6).toFixed(1)+"M";
  if(n>=1e3)return Math.round(n/1e3)+"K";
  return String(n);}

function breakdown(w){return "in "+tokens(w.in)+" \u00b7 out "+tokens(w.out)
  +" \u00b7 cache write "+tokens(w.cache_w)+" \u00b7 cache read "+tokens(w.cache_r);}

function resetLabel(ts,now){
  if(!ts)return "";
  // windows close on :59.999, so round to the minute or the day reads one off
  const d=new Date(Math.round(ts/60)*60000);
  const clock=d.toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})
    .replace(/\s/g,"").toLowerCase();
  const sameDay=new Date(now*1000).toDateString()===d.toDateString();
  return "resets "+(sameDay?clock
    :d.toLocaleDateString([],{month:"short",day:"numeric"})+" "+clock);
}

// Real percentages when usage-probe.sh is feeding the board; the local token
// totals are what is left when nobody is.
function sliders(lim,u,now){
  const stale=now-lim.at>1800;
  return lim.bars.map(b=>{
    const w=u&&u.ok&&b.join?u[b.join]:null;
    // the API grades each bar itself; thresholds only cover a missing severity
    const hot=({normal:"ok",warning:"warn",warn:"warn",critical:"crit",crit:"crit"})[b.severity]
      ||(b.pct>=90?"crit":b.pct>=75?"warn":"ok");
    const tip=[b.label+": "+b.pct+"% used",
      b.resets_at?"resets in "+ago(b.resets_at-now):"",
      w?tokens(w.total)+" tokens locally \u00b7 "+breakdown(w):"",
      "reported "+ago(now-lim.at)+" ago"].filter(Boolean).join("\n");
    return `<div class="bar${stale?" stale":""}" data-hot="${hot}" title="${esc(tip)}">`
      +`<span class="lab">${esc(b.label)}</span>`
      +`<span class="track"><span class="fill" style="width:${b.pct}%"></span></span>`
      +`<span class="pct">${b.pct}%</span>`
      +`<span class="when">${resetLabel(b.resets_at,now)}</span></div>`;
  }).join("");
}

function tokenCells(u,now){
  const cells=[["now 5h",u.now],["7 days",u.week],["fable 7d",u.fable]].map(
    ([label,w])=>`<div class="${w.total?"":"cold"}" title="${esc(breakdown(w))}">`
      +`<span>${label}</span><b>${tokens(w.total)}</b></div>`);
  cells.push(`<div class="${u.reset_at?"":"cold"}"><span>block resets</span>`
    +`<b>${u.reset_at?ago(u.reset_at-now):"\u2014"}</b></div>`);
  return cells.join("");
}

function usage(state,now){
  const box=el("usage"), lim=state.limits||{}, u=state.usage||{};
  if(lim.ok){
    box.className="usage bars";box.style.display="grid";
    box.innerHTML=sliders(lim,u,now);
  }else if(u.ok){
    box.className="usage";box.style.display="flex";
    box.innerHTML=tokenCells(u,now);
  }else{
    box.style.display="none";
  }
}

function beep(){if(!soundOn)return;try{
  const a=new (window.AudioContext||window.webkitAudioContext)();
  const o=a.createOscillator(),g=a.createGain();
  o.connect(g);g.connect(a.destination);o.type="sine";o.frequency.value=880;
  g.gain.setValueAtTime(.0001,a.currentTime);
  g.gain.exponentialRampToValueAtTime(.15,a.currentTime+.01);
  g.gain.exponentialRampToValueAtTime(.0001,a.currentTime+.35);
  o.start();o.stop(a.currentTime+.36);}catch(e){}}

let state={sessions:[],feed:[],waiting:0};
function render(){
  const now=Date.now()/1000+skew;
  el("count").textContent=state.waiting;
  el("count").className="count"+(state.waiting?"":" clear");
  el("count").appendChild(Object.assign(document.createElement("small"),
    {textContent:state.waiting===1?"waiting on you":"waiting on you"}));
  document.title=(state.waiting?"("+state.waiting+") ":"")+"ccdeck";
  usage(state,now);

  const live=state.sessions.filter(s=>s.status!=="closed");
  el("board").innerHTML = live.length ? live.map(s=>`
    <div class="strip" data-s="${s.status}">
      <div class="rail"></div>
      <div class="who">
        <div class="proj">${esc(s.project)}</div>
        <div class="path">${esc(s.cwd)}</div>
      </div>
      <div class="what">
        <div class="state">${LABEL[s.status]||s.status}${s.mode&&s.mode!=="default"?" &middot; "+esc(s.mode):""}</div>
        <div class="detail">${esc(s.detail||"-")}</div>
        ${s.prompt?`<div class="prompt">&ldquo;${esc(s.prompt)}&rdquo;</div>`:""}
      </div>
      <div class="clock"><b>${ago(now-s.since)}</b>${s.turns||0} turns</div>
    </div>`).join("")
    : `<div class="empty">No sessions yet. Start Claude Code in any project and it will show up here.</div>`;

  el("log").innerHTML=state.feed.map(f=>`
    <div class="${f.urgent?"u":""}">
      <span>${new Date(f.t*1000).toLocaleTimeString()}</span>
      <b>${esc(f.project)} &middot; ${esc(f.kind)}</b>
      <span>${esc(f.text)}</span>
    </div>`).join("");
}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

const src=new EventSource("/events");
src.onmessage=e=>{
  const next=JSON.parse(e.data);
  skew=next.now-Date.now()/1000;
  const fresh=(next.feed[0]||{}).t||0;
  if(fresh>lastAlert){
    if(lastAlert){
      const f=next.feed[0];
      beep();
      if(window.Notification&&Notification.permission==="granted")
        new Notification(f.project+" - "+f.kind,{body:f.text,tag:f.session});
    }
    lastAlert=fresh;
  }
  state=next;render();
};
setInterval(render,1000);
el("notify").onclick=()=>Notification.requestPermission().then(p=>{
  el("notify").textContent=p==="granted"?"Alerts on":"Alerts blocked";});
el("sound").onclick=e=>{soundOn=!soundOn;
  e.target.textContent=soundOn?"Sound on":"Sound off";
  e.target.setAttribute("aria-pressed",soundOn);beep();};
if(window.Notification&&Notification.permission==="granted")
  el("notify").textContent="Alerts on";
</script></body></html>
"""


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    if USAGE_EVERY > 0:
        threading.Thread(target=usage_loop, daemon=True).start()
    # When bound to every interface (container), loopback is still how you reach it.
    shown = "127.0.0.1" if HOST in ("0.0.0.0", "::", "") else HOST
    print("ccdeck listening on http://%s:%d" % (shown, PORT))
    print("  hook endpoint  POST http://%s:%d/hook" % (shown, PORT))
    if HOST != "127.0.0.1":
        print("  bind address   %s" % HOST)
    if NTFY_TOPIC:
        print("  phone push     ntfy.sh/%s" % NTFY_TOPIC)
    if USAGE_EVERY > 0:
        print("  token usage    %s every %ds" % (TRANSCRIPTS, USAGE_EVERY))
    print("  started        %s" % datetime.now().strftime("%H:%M:%S"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nccdeck stopped")


if __name__ == "__main__":
    main()
