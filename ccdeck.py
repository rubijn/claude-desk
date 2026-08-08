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
"""

import json
import os
import queue
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("CCDECK_HOST", "127.0.0.1")
PORT = int(os.environ.get("CCDECK_PORT", "8787"))
NTFY_TOPIC = os.environ.get("CCDECK_NTFY_TOPIC", "").strip()
BELL = os.environ.get("CCDECK_BELL", "1") == "1"
ALERT_IDLE = os.environ.get("CCDECK_ALERT_IDLE", "0") == "1"

STATE_LOCK = threading.Lock()
SESSIONS = {}          # session_id -> dict
FEED = []              # newest-first list of notable events, capped
SUBSCRIBERS = []       # SSE queues

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
        if self.path.rstrip("/") != "/hook":
            self._send(404, b'{"error":"not found"}')
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            ev = json.loads(raw or b"{}")
        except Exception:
            ev = {}
        try:
            out = handle_event(ev)
        except Exception as exc:  # never let a board bug block a session
            print("ccdeck: %s" % exc)
            out = {}
        self._send(200, json.dumps(out).encode())

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
    # When bound to every interface (container), loopback is still how you reach it.
    shown = "127.0.0.1" if HOST in ("0.0.0.0", "::", "") else HOST
    print("ccdeck listening on http://%s:%d" % (shown, PORT))
    print("  hook endpoint  POST http://%s:%d/hook" % (shown, PORT))
    if HOST != "127.0.0.1":
        print("  bind address   %s" % HOST)
    if NTFY_TOPIC:
        print("  phone push     ntfy.sh/%s" % NTFY_TOPIC)
    print("  started        %s" % datetime.now().strftime("%H:%M:%S"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nccdeck stopped")


if __name__ == "__main__":
    main()
