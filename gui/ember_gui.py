#!/usr/bin/env python3
"""Ember dashboard — a local web UI for the AMD BTX miner.

Standalone, stdlib-only. It does NOT touch the miner's code: it launches the
miner exactly as the `ember` launcher would, tees its log to your terminal AND
into a parser, polls `rocm-smi` for GPU health, and serves a live dashboard on
http://127.0.0.1:<port>.

Invoked by `ember --gui`, which passes the fully-built miner command after `--`:

    python3 ember_gui.py --port 8787 -- python3 -m dexbtx_miner --pool ... --gbt-solve ...

You normally never call this directly; use `./ember --gui`.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── log line patterns (see stratum_client.py / __main__.py) ──────────────────
RE_SHARE_OK = re.compile(r"share OK job=(\S+) nonce=(\d+) \(a/r/b=(\d+)/(\d+)/(\d+)\)")
RE_SHARE_REJ = re.compile(r"share REJECTED job=(\S+) nonce=(\d+) \(a/r=(\d+)/(\d+)\)")
RE_WORKING = re.compile(r"solver: working job=(\S+) nonce_start=(\d+) slice=(\d+)")
RE_RATIO = re.compile(r"ratio_vs_block=([\d.]+)x")
RE_NOTIFY = re.compile(r"notify job=(\S+) height_hint=prev=(\S+?)\.\.\. clean=(\w+)")
RE_DIFF = re.compile(r"difficulty set to (\S+)")
RE_TOTALS = re.compile(r"totals: accepted=(\d+) rejected=(\d+) blocks=(\d+)")


class State:
    """Thread-safe live metrics, updated from the log + rocm-smi threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.wall0 = time.time()
        # shares
        self.accepted = 0
        self.rejected = 0
        self.blocks = 0
        self.share_times: deque[float] = deque(maxlen=4096)  # monotonic
        self.last_share_at: float | None = None
        # pool / job
        self.solver_ready = False
        self.diff_ratio: float | None = None
        self.difficulty: str | None = None
        self.current_job: str | None = None
        self.last_notify_at: float | None = None
        self.job_updates = 0
        # hashrate — derived from nonce_start deltas across slices
        self._last_ns: int | None = None
        self._last_ns_t: float | None = None
        self.slice_size: int | None = None
        self.hr_samples: deque[float] = deque(maxlen=12)  # recent N/s for smoothing
        self.hr_hist: deque[tuple[float, float]] = deque(maxlen=240)  # (wall, N/s)
        # dev fee
        self.fee_events = 0
        self.last_fee_at: float | None = None
        # gpu (from rocm-smi)
        self.gpu: dict = {}
        self.gpu_at: float | None = None
        # log tail
        self.log_tail: deque[str] = deque(maxlen=250)
        # process
        self.miner_alive = True
        self.exit_code: int | None = None

    # -- ingest one log line -------------------------------------------------
    def feed_line(self, line: str) -> None:
        now = time.monotonic()
        with self.lock:
            self.log_tail.append(line.rstrip("\n"))

            m = RE_SHARE_OK.search(line)
            if m:
                self.accepted = int(m.group(3))
                self.rejected = int(m.group(4))
                self.blocks = int(m.group(5))
                self.share_times.append(now)
                self.last_share_at = now
                return
            m = RE_SHARE_REJ.search(line)
            if m:
                self.accepted = int(m.group(3))
                self.rejected = int(m.group(4))
                return
            m = RE_WORKING.search(line)
            if m:
                ns = int(m.group(2))
                self.slice_size = int(m.group(3))
                if self._last_ns is not None and self._last_ns_t is not None:
                    dns = ns - self._last_ns
                    dt = now - self._last_ns_t
                    # only a forward, same-parent advance is a valid rate sample;
                    # a parent change resets nonce_start (dns<=0 or absurd).
                    if dt > 0.4 and 0 < dns < 5e11:
                        rate = dns / dt
                        self.hr_samples.append(rate)
                        self.hr_hist.append((time.time(), self._smoothed_locked()))
                self._last_ns = ns
                self._last_ns_t = now
                return
            m = RE_NOTIFY.search(line)
            if m:
                self.current_job = m.group(1)
                self.last_notify_at = now
                self.job_updates += 1
                return
            m = RE_RATIO.search(line)
            if m:
                self.diff_ratio = float(m.group(1))
                return
            m = RE_DIFF.search(line)
            if m:
                self.difficulty = m.group(1)
                return
            m = RE_TOTALS.search(line)
            if m:
                self.accepted = int(m.group(1))
                self.rejected = int(m.group(2))
                self.blocks = int(m.group(3))
                return
            if "solver daemon ready" in line:
                self.solver_ready = True
                return
            if "dev_fee" in line or "developer fee" in line:
                self.fee_events += 1
                self.last_fee_at = now
                return

    def _smoothed_locked(self) -> float:
        if not self.hr_samples:
            return 0.0
        s = sorted(self.hr_samples)
        return s[len(s) // 2]  # median — robust to slice jitter

    def set_gpu(self, gpu: dict) -> None:
        with self.lock:
            self.gpu = gpu
            self.gpu_at = time.monotonic()

    # -- snapshot for the API ------------------------------------------------
    def snapshot(self) -> dict:
        now = time.monotonic()
        with self.lock:
            # shares/min over the last 60s and 10min
            spm = sum(1 for t in self.share_times if now - t <= 60)
            s10 = [t for t in self.share_times if now - t <= 600]
            spm10 = (len(s10) / (min(600.0, now - self.t0) / 60.0)) if now - self.t0 > 5 else 0.0
            hr = self._smoothed_locked()
            total = self.accepted + self.rejected
            accept_pct = (100.0 * self.accepted / total) if total else 100.0
            power = _num(self.gpu.get("power"))
            eff = (hr / power) if (hr and power) else None

            # status
            if not self.miner_alive:
                status = "stopped"
            elif self.last_notify_at and now - self.last_notify_at < 45:
                status = "mining"
            elif self.solver_ready:
                status = "connecting"
            else:
                status = "starting"

            return {
                "status": status,
                "uptime_s": now - self.t0,
                "hashrate": hr,
                "hashrate_hist": list(self.hr_hist),
                "efficiency": eff,
                "slice_size": self.slice_size,
                "accepted": self.accepted,
                "rejected": self.rejected,
                "blocks": self.blocks,
                "accept_pct": accept_pct,
                "shares_per_min": spm,
                "shares_per_min_avg": spm10,
                "last_share_ago": (now - self.last_share_at) if self.last_share_at else None,
                "diff_ratio": self.diff_ratio,
                "difficulty": self.difficulty,
                "current_job": self.current_job,
                "job_updates": self.job_updates,
                "last_notify_ago": (now - self.last_notify_at) if self.last_notify_at else None,
                "fee_events": self.fee_events,
                "last_fee_ago": (now - self.last_fee_at) if self.last_fee_at else None,
                "gpu": self.gpu,
                "gpu_stale": (self.gpu_at is None) or (now - self.gpu_at > 8),
                "log": list(self.log_tail)[-120:],
                "exit_code": self.exit_code,
            }


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── rocm-smi poller ──────────────────────────────────────────────────────────
_CLK = re.compile(r"\((\d+)\s*Mhz\)", re.I)


def _parse_rocm(js: dict) -> dict:
    # first card only (single-GPU rigs; multi-GPU is a future extension)
    card = None
    for k, v in js.items():
        if k.startswith("card") and isinstance(v, dict):
            card = v
            break
    if not card:
        return {}

    def clk(key):
        raw = card.get(key, "")
        m = _CLK.search(str(raw))
        return int(m.group(1)) if m else None

    vram_t = _num(card.get("VRAM Total Memory (B)"))
    vram_u = _num(card.get("VRAM Total Used Memory (B)"))
    return {
        "temp_edge": _num(card.get("Temperature (Sensor edge) (C)")),
        "temp_junction": _num(card.get("Temperature (Sensor junction) (C)")),
        "temp_mem": _num(card.get("Temperature (Sensor memory) (C)")),
        "power": _num(card.get("Average Graphics Package Power (W)")),
        "use": _num(card.get("GPU use (%)")),
        "sclk": clk("sclk clock speed:"),
        "mclk": clk("mclk clock speed:"),
        "vram_used_gb": (vram_u / 1e9) if vram_u else None,
        "vram_total_gb": (vram_t / 1e9) if vram_t else None,
    }


def rocm_poller(state: State, stop: threading.Event) -> None:
    cmd = [
        "rocm-smi", "--showtemp", "--showpower", "--showuse",
        "--showclocks", "--showmeminfo", "vram", "--json",
    ]
    missing = False
    while not stop.is_set():
        if not missing:
            try:
                out = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=6,
                ).stdout
                state.set_gpu(_parse_rocm(json.loads(out)))
            except FileNotFoundError:
                missing = True  # no rocm-smi (e.g. some WSL2 setups) — stop trying
            except Exception:
                pass  # transient; try again next tick
        stop.wait(2.0)


# ── HTTP server ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    state: State = None  # set on the class before serving

    def log_message(self, *a):  # silence request logging
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.startswith("/api/state"):
            body = json.dumps(self.state.snapshot()).encode()
            self._send(200, body, "application/json")
        elif self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")


def serve(state: State, host: str, port: int) -> ThreadingHTTPServer:
    Handler.state = state
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ── miner subprocess ─────────────────────────────────────────────────────────
def run_miner(argv: list[str], state: State) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )

    def pump():
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)  # keep the terminal log intact
            sys.stdout.flush()
            state.feed_line(line)
        proc.wait()
        with state.lock:
            state.miner_alive = False
            state.exit_code = proc.returncode

    threading.Thread(target=pump, daemon=True).start()
    return proc


def main() -> int:
    # args: [--port N] [--host H] [--no-open] -- <miner argv...>
    port, host, no_open = 8787, "127.0.0.1", False
    args = sys.argv[1:]
    if "--" not in args:
        print("ember_gui: expected '-- <miner command>'", file=sys.stderr)
        return 2
    split = args.index("--")
    opts, miner_argv = args[:split], args[split + 1:]
    i = 0
    while i < len(opts):
        if opts[i] == "--port":
            port = int(opts[i + 1]); i += 2
        elif opts[i] == "--host":
            host = opts[i + 1]; i += 2
        elif opts[i] == "--no-open":
            no_open = True; i += 1
        else:
            i += 1
    if not miner_argv:
        print("ember_gui: no miner command after '--'", file=sys.stderr)
        return 2

    state = State()
    stop = threading.Event()

    try:
        httpd = serve(state, host, port)
    except OSError as e:
        print(f"ember_gui: cannot bind {host}:{port} ({e}). "
              f"Try a different --gui-port.", file=sys.stderr)
        return 1

    url = f"http://{host}:{port}/"
    print(f"\n  ┌─ Ember dashboard ──────────────────────────────\n"
          f"  │  {url}\n"
          f"  └─ (Ctrl-C stops mining and the dashboard)\n", flush=True)

    threading.Thread(target=rocm_poller, args=(state, stop), daemon=True).start()
    proc = run_miner(miner_argv, state)

    if not no_open:
        threading.Timer(1.2, lambda: _try_open(url)).start()

    def shutdown(*_):
        stop.set()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        httpd.shutdown()

    signal.signal(signal.SIGINT, lambda *a: (shutdown(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *a: (shutdown(), sys.exit(0)))

    proc.wait()  # miner exited on its own
    stop.set()
    return proc.returncode or 0


def _try_open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


# ── the dashboard page (self-contained: no external assets, CSP-clean) ───────
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ember — mining dashboard</title>
<style>
  :root{
    --bg:#141210; --panel:#1c1916; --panel2:#211d19; --line:#2c2621;
    --tx:#ece4dc; --mut:#9a8d80; --dim:#6f6459;
    --ember1:#ff5a36; --ember2:#ffb638; --ok:#57d98a; --warn:#ffb638; --bad:#f8746a;
  }
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 600px at 70% -10%,#241d17 0%,var(--bg) 55%);
       color:var(--tx);font:14px/1.5 ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace;
       -webkit-font-smoothing:antialiased}
  a{color:var(--ember2);text-decoration:none} a:hover{text-decoration:underline}
  header{display:flex;align-items:center;gap:14px;padding:18px 22px;border-bottom:1px solid var(--line)}
  .logo{font-weight:700;font-size:20px;letter-spacing:.5px;
        background:linear-gradient(90deg,var(--ember1),var(--ember2));
        -webkit-background-clip:text;background-clip:text;color:transparent}
  .spark-dot{width:9px;height:9px;border-radius:50%;background:var(--ember1);
             box-shadow:0 0 10px 2px var(--ember1)}
  .badge{margin-left:auto;display:flex;align-items:center;gap:8px;padding:6px 12px;
         border:1px solid var(--line);border-radius:999px;background:var(--panel);font-size:12px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--dim)}
  .dot.mining{background:var(--ok);box-shadow:0 0 8px var(--ok);animation:pulse 1.8s infinite}
  .dot.connecting,.dot.starting{background:var(--warn);box-shadow:0 0 8px var(--warn)}
  .dot.stopped{background:var(--bad);box-shadow:0 0 8px var(--bad)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
  .up{color:var(--mut);font-size:12px}
  main{padding:20px;max-width:1180px;margin:0 auto;
       display:grid;grid-template-columns:repeat(12,1fr);gap:16px}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);
        border-radius:14px;padding:16px 18px;position:relative;overflow:hidden}
  .card h2{margin:0 0 12px;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--mut);font-weight:600}
  .col-4{grid-column:span 4} .col-5{grid-column:span 5} .col-6{grid-column:span 6}
  .col-7{grid-column:span 7} .col-8{grid-column:span 8} .col-12{grid-column:span 12}
  @media(max-width:820px){main{grid-template-columns:repeat(6,1fr)}
     .col-4,.col-5,.col-6,.col-7,.col-8{grid-column:span 6}}
  .big{font-size:40px;font-weight:700;line-height:1;letter-spacing:-1px}
  .big .u{font-size:15px;color:var(--mut);font-weight:500;margin-left:6px}
  .sub{color:var(--mut);font-size:12px;margin-top:8px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px 18px;margin-top:4px}
  .kv .k{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  .kv .v{font-size:19px;font-weight:600;margin-top:2px}
  .v.ok{color:var(--ok)} .v.bad{color:var(--bad)} .v.amber{color:var(--ember2)}
  canvas{width:100%;height:60px;display:block;margin-top:10px}
  .bar{height:8px;border-radius:6px;background:#000;overflow:hidden;margin-top:6px}
  .bar>span{display:block;height:100%;border-radius:6px;
            background:linear-gradient(90deg,var(--ember1),var(--ember2))}
  .barrow{margin-bottom:12px}
  .barrow .lab{display:flex;justify-content:space-between;font-size:12px;color:var(--mut)}
  .barrow .lab b{color:var(--tx);font-weight:600}
  .temp .lab b{color:var(--tx)}
  .hot span{background:linear-gradient(90deg,#f8746a,#ff9a3c)!important}
  .log{background:#0d0b09;border:1px solid var(--line);border-radius:10px;padding:10px 12px;
       height:250px;overflow-y:auto;font-size:12px;line-height:1.55;color:#b9ad9f}
  .log div{white-space:pre-wrap;word-break:break-word}
  .log .ok{color:var(--ok)} .log .rej{color:var(--bad)} .log .fee{color:var(--ember2)}
  .log .warn{color:#e5b567}
  .fee-note{font-size:12px;color:var(--mut);margin-top:8px;padding-top:10px;border-top:1px solid var(--line)}
  .foot{grid-column:span 12;text-align:center;color:var(--dim);font-size:11px;padding:6px 0 2px}
</style>
</head>
<body>
<header>
  <span class="spark-dot"></span>
  <span class="logo">EMBER</span>
  <span class="up" id="uptime">—</span>
  <span class="badge"><span class="dot" id="statdot"></span><span id="status">starting…</span></span>
</header>
<main>
  <section class="card col-5">
    <h2>Hashrate</h2>
    <div><span class="big" id="hr">—</span><span class="u">nonces/s</span></div>
    <div class="sub" id="eff">efficiency —</div>
    <canvas id="spark" width="600" height="120"></canvas>
  </section>

  <section class="card col-4">
    <h2>Shares</h2>
    <div><span class="big v ok" id="acc" style="font-size:40px">—</span><span class="u">accepted</span></div>
    <div class="grid2" style="margin-top:14px">
      <div class="kv"><div class="k">Rejected</div><div class="v" id="rej">—</div></div>
      <div class="kv"><div class="k">Accept %</div><div class="v" id="accpct">—</div></div>
      <div class="kv"><div class="k">Shares/min</div><div class="v amber" id="spm">—</div></div>
      <div class="kv"><div class="k">Blocks</div><div class="v" id="blk">—</div></div>
    </div>
  </section>

  <section class="card col-3" style="grid-column:span 3">
    <h2>Pool</h2>
    <div class="kv"><div class="k">Difficulty</div><div class="v" id="diff">—</div></div>
    <div class="kv" style="margin-top:12px"><div class="k">Job updates</div><div class="v" id="jobs">—</div></div>
    <div class="kv" style="margin-top:12px"><div class="k">Last block tmpl</div><div class="v" id="notify" style="font-size:15px">—</div></div>
    <div class="fee-note" id="feenote">Dev fee: idle</div>
  </section>

  <section class="card col-7">
    <h2>GPU health</h2>
    <div id="gpuwrap">
      <div class="barrow temp"><div class="lab"><span>Edge temp</span><b id="te">—</b></div><div class="bar" id="teb"><span></span></div></div>
      <div class="barrow temp"><div class="lab"><span>Junction (hotspot)</span><b id="tj">—</b></div><div class="bar" id="tjb"><span></span></div></div>
      <div class="barrow temp"><div class="lab"><span>Memory temp</span><b id="tm">—</b></div><div class="bar" id="tmb"><span></span></div></div>
      <div class="grid2" style="margin-top:14px">
        <div class="kv"><div class="k">Power</div><div class="v amber" id="pw">—</div></div>
        <div class="kv"><div class="k">GPU load</div><div class="v" id="use">—</div></div>
        <div class="kv"><div class="k">Core clock</div><div class="v" id="sclk">—</div></div>
        <div class="kv"><div class="k">Mem clock</div><div class="v" id="mclk">—</div></div>
        <div class="kv"><div class="k">VRAM</div><div class="v" id="vram" style="font-size:15px">—</div></div>
        <div class="kv"><div class="k">Efficiency</div><div class="v" id="eff2" style="font-size:15px">—</div></div>
      </div>
    </div>
  </section>

  <section class="card col-5">
    <h2>Live log</h2>
    <div class="log" id="log"></div>
  </section>

  <div class="foot">Ember · AMD GPU miner for BTX · dashboard refreshes every 1.5s · a transparent 1% developer fee runs in this binary</div>
</main>

<script>
const $=id=>document.getElementById(id);
const nf=new Intl.NumberFormat('en-US');
function hms(s){s=Math.max(0,Math.floor(s));const d=Math.floor(s/86400);s%=86400;
  const h=Math.floor(s/3600);s%=3600;const m=Math.floor(s/60);const x=s%60;
  return (d?d+'d ':'')+String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(x).padStart(2,'0');}
function ago(s){if(s==null)return '—';if(s<60)return Math.floor(s)+'s ago';
  if(s<3600)return Math.floor(s/60)+'m ago';return Math.floor(s/3600)+'h ago';}
function tclass(el,t,hot){el.className='bar'+(hot?' hot':'');}
function bar(id,val,max,hotAt){const w=Math.max(0,Math.min(100,100*val/max));
  const box=$(id);box.firstElementChild.style.width=w+'%';
  box.className='bar'+(val>=hotAt?' hot':'');}

function drawSpark(hist){
  const c=$('spark'),ctx=c.getContext('2d');
  const W=c.width,H=c.height;ctx.clearRect(0,0,W,H);
  if(!hist||hist.length<2)return;
  const vals=hist.map(p=>p[1]);const mx=Math.max(...vals)*1.15||1;
  const g=ctx.createLinearGradient(0,0,W,0);g.addColorStop(0,'#ff5a36');g.addColorStop(1,'#ffb638');
  ctx.lineWidth=2.5;ctx.strokeStyle=g;ctx.beginPath();
  hist.forEach((p,i)=>{const x=W*i/(hist.length-1);const y=H-(H-8)*p[1]/mx-4;
    i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
  ctx.stroke();
  // fill
  ctx.lineTo(W,H);ctx.lineTo(0,H);ctx.closePath();
  const fg=ctx.createLinearGradient(0,0,0,H);fg.addColorStop(0,'rgba(255,120,60,.28)');fg.addColorStop(1,'rgba(255,120,60,0)');
  ctx.fillStyle=fg;ctx.fill();
}

function fmtHR(v){if(!v)return '—';if(v>=1e6)return (v/1e6).toFixed(2)+'M';
  if(v>=1e3)return (v/1e3).toFixed(1)+'k';return Math.round(v);}

const LOGCLS=l=>l.includes('share OK')?'ok':l.includes('REJECT')?'rej':
  (l.includes('dev_fee')||l.includes('developer fee'))?'fee':
  (l.includes('WARN')||l.includes('warning'))?'warn':'';

async function tick(){
  let s;try{s=await (await fetch('/api/state',{cache:'no-store'})).json();}catch(e){return;}
  // status
  const st=s.status;$('status').textContent=
    ({mining:'mining',connecting:'connecting…',starting:'starting…',stopped:'stopped'})[st]||st;
  $('statdot').className='dot '+st;
  $('uptime').textContent='uptime '+hms(s.uptime_s);
  // hashrate
  $('hr').textContent=fmtHR(s.hashrate);
  $('eff').textContent=s.efficiency?('efficiency '+Math.round(s.efficiency)+' N/s per watt'):'efficiency —';
  drawSpark(s.hashrate_hist);
  // shares
  $('acc').textContent=nf.format(s.accepted);
  $('rej').textContent=nf.format(s.rejected);
  const ap=$('accpct');ap.textContent=(s.accept_pct??100).toFixed(1)+'%';
  ap.className='v '+((s.accept_pct>=98)?'ok':(s.accept_pct>=90?'amber':'bad'));
  $('spm').textContent=(s.shares_per_min||0)+' /min';
  $('blk').textContent=nf.format(s.blocks);
  // pool
  $('diff').textContent=s.diff_ratio?(s.diff_ratio.toFixed(1)+'× easier'):(s.difficulty||'—');
  $('jobs').textContent=nf.format(s.job_updates)+'  ('+ago(s.last_notify_ago)+')';
  $('notify').textContent=s.current_job?('#'+String(s.current_job).slice(0,10)):'—';
  const fn=$('feenote');
  if(s.fee_events>0){fn.innerHTML='Dev fee: <b style="color:var(--ember2)">active</b> · '+
    s.fee_events+' rounds · '+ago(s.last_fee_ago);}
  else{fn.textContent='Dev fee: idle (1% of runtime, disclosed)';}
  // gpu
  const g=s.gpu||{};const stale=s.gpu_stale;
  const setT=(id,idb,v,hot)=>{$(id).textContent=v!=null?(v.toFixed(0)+'°C'):'—';
    bar(idb,v||0,110,hot);};
  setT('te','teb',g.temp_edge,85);
  setT('tj','tjb',g.temp_junction,95);
  setT('tm','tmb',g.temp_mem,95);
  $('pw').textContent=g.power!=null?(g.power.toFixed(0)+' W'):'—';
  $('use').textContent=g.use!=null?(g.use.toFixed(0)+' %'):'—';
  $('sclk').textContent=g.sclk!=null?(g.sclk+' MHz'):'—';
  $('mclk').textContent=g.mclk!=null?(g.mclk+' MHz'):'—';
  $('vram').textContent=(g.vram_used_gb!=null&&g.vram_total_gb!=null)?
    (g.vram_used_gb.toFixed(1)+' / '+g.vram_total_gb.toFixed(1)+' GB'):'—';
  $('eff2').textContent=s.efficiency?(Math.round(s.efficiency)+' N/s/W'):'—';
  document.getElementById('gpuwrap').style.opacity=stale?0.45:1;
  // log
  const box=$('log');const near=box.scrollHeight-box.scrollTop-box.clientHeight<40;
  box.innerHTML=(s.log||[]).map(l=>'<div class="'+LOGCLS(l)+'">'+
    l.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))+'</div>').join('');
  if(near)box.scrollTop=box.scrollHeight;
}
tick();setInterval(tick,1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
