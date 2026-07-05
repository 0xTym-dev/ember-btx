#!/usr/bin/env python3
"""Ember update check — notify (never auto-install) when a newer release exists.

This is Ember's OWN release check against our GitHub releases. It is deliberately
NOT the upstream solver auto-updater — that one would replace the AMD/HIP binary
with an upstream NVIDIA build, so it stays disabled (DEXBTX_NO_SOLVER_AUTOUPDATE
etc. in the launcher). This script only *reads* the latest release tag and prints
a one-line notice; it downloads and installs nothing.

Properties:
  - fail-silent: any network / parse error just returns quietly (never blocks mining)
  - rate-limited: hits the network at most once per 24h, cached in ~/.ember/
  - opt-out: set EMBER_NO_UPDATE_CHECK=1

Usage: ember_update.py --version-file /path/to/VERSION
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

REPO = "0xTym-dev/ember-btx"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES = f"https://github.com/{REPO}/releases/latest"
CACHE = Path.home() / ".ember" / "update-check.json"
INTERVAL = 24 * 3600   # seconds between network checks
TIMEOUT = 3.0          # seconds; keep short so startup is never delayed


def parse_ver(s: str) -> tuple[int, ...]:
    """'v0.1.2', '0.1.2-rc1' -> (0, 1, 2). Best-effort, never raises."""
    s = (s or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    out: list[int] = []
    for p in s.split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out) or (0,)


def read_local(version_file: str | None) -> str | None:
    if not version_file:
        return None
    try:
        v = Path(version_file).read_text(encoding="utf-8").strip()
        return v or None
    except Exception:
        return None


def cached_latest() -> str | None:
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        if time.time() - float(d.get("checked_at", 0)) < INTERVAL:
            return d.get("latest")
    except Exception:
        pass
    return None


def write_cache(latest: str) -> None:
    try:
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps({"checked_at": int(time.time()), "latest": latest}),
                         encoding="utf-8")
    except Exception:
        pass


def fetch_latest() -> str | None:
    req = urllib.request.Request(API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "ember-update-check",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 (fixed https host)
        data = json.load(r)
    return data.get("tag_name") or data.get("name")


def main() -> int:
    if os.environ.get("EMBER_NO_UPDATE_CHECK"):
        return 0

    version_file = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--version-file" and i + 1 < len(args):
            version_file = args[i + 1]

    local = read_local(version_file)
    if not local:
        return 0

    latest = cached_latest()
    if latest is None:
        try:
            latest = fetch_latest()
        except Exception:
            return 0
        if not latest:
            return 0
        write_cache(latest)

    try:
        if parse_ver(latest) > parse_ver(local):
            sys.stderr.write(
                "\n"
                "  ┌─ Ember update available ──────────────────────────────\n"
                f"  │  You're on {local} — {latest} is out.\n"
                f"  │  {RELEASES}\n"
                "  │  (set EMBER_NO_UPDATE_CHECK=1 to silence this)\n"
                "  └────────────────────────────────────────────────────────\n\n"
            )
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
