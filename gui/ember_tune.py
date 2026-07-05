#!/usr/bin/env python3
"""Ember DEEP TUNE — auto-find the best miner settings for THIS card.

It launches the miner repeatedly with different settings, measures the
steady-state scan rate (nonces/s, read from the solver's `nonce_start` advance
in the log) over a window, and runs a coordinate search over batch size and CPU
feed workers. The winner is written to ~/.ember/tune-<gfx>.json, which the
`ember` launcher loads automatically on later runs.

Invoked by `ember --deep-tune`, which passes the base miner command after `--`:

    python3 ember_tune.py --gfx gfx1101 [--window 120] [--warmup 40]
        [--objective throughput|efficiency] -- python3 -m dexbtx_miner ...

Tuning takes a while (each trial is warmup+window seconds, and there are ~a
dozen trials) — that's the point; run it once per card, ideally overnight.
Nothing here needs root: it only varies safe runtime flags. Core-clock / power
tuning is manual (see docs) because it needs sysfs OverDrive access.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

RE_WORKING = re.compile(r"solver: working job=\S+ nonce_start=(\d+) slice=(\d+)")

# search space (safe runtime flags)
BATCH_CANDIDATES = [128, 192, 256, 320, 384, 448, 512, 640, 768]
WORKER_CANDIDATES = [8, 12, 16, 20, 24]
DEFAULT_BATCH = 384
DEFAULT_WORKERS = 16


def _rocm_power() -> float | None:
    try:
        out = subprocess.run(
            ["rocm-smi", "--showpower", "--json"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        js = json.loads(out)
        for k, v in js.items():
            if k.startswith("card") and isinstance(v, dict):
                for key, val in v.items():
                    if "Power" in key:
                        try:
                            return float(val)
                        except (TypeError, ValueError):
                            pass
    except Exception:
        return None
    return None


def _strip_flag(argv: list[str], flag: str) -> list[str]:
    """Remove `flag VALUE` from argv (so we can set our own)."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] == flag:
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def measure(base_argv: list[str], batch: int, workers: int,
            warmup: float, window: float, want_power: bool) -> tuple[float, float | None]:
    """Run one trial; return (median nonces/s, avg watts or None)."""
    argv = _strip_flag(_strip_flag(list(base_argv), "--batch-size"), "--prepare-workers")
    argv += ["--batch-size", str(batch), "--prepare-workers", str(workers)]

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env, start_new_session=True,
    )

    samples: list[float] = []      # (nps) during the measurement window
    powers: list[float] = []
    last_ns: int | None = None
    last_t: float | None = None
    slice_size: int | None = None
    t_start = time.monotonic()
    measure_from = t_start + warmup
    measure_to = measure_from + window
    next_power = 0.0

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            now = time.monotonic()
            if now >= measure_to:
                break
            m = RE_WORKING.search(line)
            if m:
                ns = int(m.group(1))
                slice_size = int(m.group(2))
                if last_ns is not None and last_t is not None:
                    dns = ns - last_ns
                    dt = now - last_t
                    # On a pool, nonce_start is reassigned per job and jumps by
                    # ~a full slice (slice_size) without that many nonces being
                    # scanned — a spurious ~slice_size/dt spike. A genuine advance
                    # between two 'working' lines is rate*dt, orders of magnitude
                    # below one slice, so cap accepted deltas at half a slice.
                    ceiling = slice_size * 0.5 if slice_size else 5e11
                    if dt > 0.4 and 0 < dns < ceiling and now >= measure_from:
                        samples.append(dns / dt)
                last_ns, last_t = ns, now
            if want_power and now >= measure_from and now >= next_power:
                p = _rocm_power()
                if p:
                    powers.append(p)
                next_power = now + 3.0
    finally:
        # signal the whole process group so the solver-daemon child dies too —
        # a leaked solver would keep the GPU busy and skew the NEXT trial.
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()

    if not samples:
        return 0.0, (sum(powers) / len(powers) if powers else None)
    samples.sort()
    nps = samples[len(samples) // 2]  # median
    watts = (sum(powers) / len(powers)) if powers else None
    return nps, watts


def score(nps: float, watts: float | None, objective: str) -> float:
    if objective == "efficiency" and watts:
        return nps / watts
    return nps


def main() -> int:
    args = sys.argv[1:]
    if "--" not in args:
        print("ember_tune: expected '-- <miner command>'", file=sys.stderr)
        return 2
    split = args.index("--")
    opts, base_argv = args[:split], args[split + 1:]

    gfx = "unknown"
    window, warmup, objective = 120.0, 40.0, "throughput"
    i = 0
    while i < len(opts):
        if opts[i] == "--gfx":
            gfx = opts[i + 1]; i += 2
        elif opts[i] == "--window":
            window = float(opts[i + 1]); i += 2
        elif opts[i] == "--warmup":
            warmup = float(opts[i + 1]); i += 2
        elif opts[i] == "--objective":
            objective = opts[i + 1]; i += 2
        else:
            i += 1
    if not base_argv:
        print("ember_tune: no miner command after '--'", file=sys.stderr)
        return 2

    want_power = objective == "efficiency"
    unit = "N/s per W" if want_power else "N/s"
    trial_secs = warmup + window
    results: dict[tuple[int, int], tuple[float, float | None]] = {}

    def trial(batch: int, workers: int) -> float:
        if (batch, workers) in results:
            return score(*results[(batch, workers)], objective)
        print(f"  · batch={batch:<4} workers={workers:<3} … "
              f"(~{int(trial_secs)}s)", end="", flush=True)
        nps, watts = measure(base_argv, batch, workers, warmup, window, want_power)
        results[(batch, workers)] = (nps, watts)
        sc = score(nps, watts, objective)
        wtxt = f" @ {watts:.0f} W" if watts else ""
        print(f"  → {nps:,.0f} N/s{wtxt}  [{sc:,.0f} {unit}]")
        return sc

    print(f"\n═══ Ember DEEP TUNE ═══  gfx={gfx}  objective={objective}")
    print(f"    {warmup:.0f}s warmup + {window:.0f}s measure per trial. "
          f"Ctrl-C stops and keeps the best so far.\n")

    best = (DEFAULT_BATCH, DEFAULT_WORKERS)
    best_score = -1.0

    def save_best():
        nps, watts = results.get(best, (0.0, None))
        outdir = Path.home() / ".ember"
        outdir.mkdir(exist_ok=True)
        path = outdir / f"tune-{gfx}.json"
        path.write_text(json.dumps({
            "gfx": gfx,
            "batch_size": best[0],
            "prepare_workers": best[1],
            "nps": round(nps),
            "watts": round(watts) if watts else None,
            "objective": objective,
            "measured_at": int(time.time()),
        }, indent=2))
        return path

    try:
        # 1) sweep batch size at the default worker count
        print("Phase 1/2 — batch size:")
        for b in BATCH_CANDIDATES:
            sc = trial(b, DEFAULT_WORKERS)
            if sc > best_score:
                best_score, best = sc, (b, DEFAULT_WORKERS)
        print(f"  best batch so far: {best[0]}\n")

        # 2) sweep worker count at the winning batch size
        print("Phase 2/2 — CPU feed workers:")
        for w in WORKER_CANDIDATES:
            sc = trial(best[0], w)
            if sc > best_score:
                best_score, best = sc, (best[0], w)
    except KeyboardInterrupt:
        print("\n\nInterrupted — saving the best configuration found so far.")

    path = save_best()
    nps, watts = results.get(best, (0.0, None))
    print("\n═══ DEEP TUNE complete ═══")
    print(f"  best: batch-size {best[0]}, prepare-workers {best[1]}")
    print(f"        {nps:,.0f} N/s" + (f" @ {watts:.0f} W" if watts else ""))
    print(f"  saved → {path}")
    print("  `ember` will now use these automatically (pass --batch-size to override).\n")
    return 0


if __name__ == "__main__":
    # make Ctrl-C reach the KeyboardInterrupt handler cleanly
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
