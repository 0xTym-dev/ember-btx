#!/usr/bin/env bash
# Ember installer / preflight check.
#
# Verifies the environment can run Ember: ROCm present, GPU visible, GPU arch
# in the fat binary, Python OK. Does NOT touch the system — it only checks and
# tells you what's missing. Run it before your first mine.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Archs embedded in the release fat binary (keep in sync with build-solver.sh).
SUPPORTED="gfx1030 gfx1031 gfx1032 gfx1100 gfx1101 gfx1102 gfx1200 gfx1201"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
fail=0

echo "== Ember preflight =="

# 1. WSL2 vs native
if grep -qiE "microsoft|wsl" /proc/version 2>/dev/null; then
  ENV_KIND="WSL2"; ylw "Environment : WSL2 (Windows). RDNA2/RX 6000 is NOT supported here — RDNA3/4 only."
else
  ENV_KIND="native"; grn "Environment : native Linux"
fi

# 2. ROCm present
if command -v rocminfo >/dev/null 2>&1; then
  grn "ROCm        : found ($(command -v rocminfo))"
else
  red "ROCm        : rocminfo not found. Install ROCm 6.x+ (native) or 7.2+ (WSL2)."
  [ "$ENV_KIND" = "WSL2" ] && echo "              See docs/INSTALL-wsl2.md" || echo "              See docs/INSTALL-linux.md"
  fail=1
fi

# 3. GPU visible + arch supported
# Match the full arch (>=3 digits) — rocminfo also prints a truncated "gfx11"
# generation marker that we must not pick up.
GFX="$(rocminfo 2>/dev/null | grep -oE 'gfx[0-9]{3,}' | sort -u | head -1)"
if [ -z "$GFX" ]; then
  red "GPU         : no AMD GPU detected by ROCm."
  [ "$ENV_KIND" = "WSL2" ] && echo "              Check the Adrenalin 26.1.1 WSL2 driver + 'ls /dev/dxg'."
  fail=1
else
  if echo " $SUPPORTED " | grep -q " $GFX "; then
    grn "GPU         : $GFX (supported)"
  else
    red "GPU         : $GFX is NOT in this build ($SUPPORTED)."
    echo "              Open a GitHub issue to request it (adding an arch is a rebuild)."
    fail=1
  fi
fi

# 4. solver binary + checksum
SOLVER=""
for c in "$ROOT/bin/btx-gbt-solve" "$ROOT/solver-src/build/bin/btx-gbt-solve"; do
  [ -x "$c" ] && { SOLVER="$c"; break; }
done
if [ -z "$SOLVER" ]; then
  red "Solver      : binary missing (bin/btx-gbt-solve)."
  fail=1
else
  grn "Solver      : $SOLVER"
  if [ -f "$ROOT/SHA256SUMS" ]; then
    if ( cd "$(dirname "$SOLVER")" && sha256sum -c --ignore-missing "$ROOT/SHA256SUMS" >/dev/null 2>&1 ); then
      grn "Checksum    : OK"
    else
      red "Checksum    : MISMATCH — do not run a tampered binary. Re-download the release."
      fail=1
    fi
  else
    ylw "Checksum    : no SHA256SUMS present (dev build) — skipping."
  fi
  # embedded arch sanity (process substitution avoids a pipefail+SIGPIPE
  # false-negative when grep -q exits early on a match).
  if [ -n "$GFX" ] && ! grep -q -- "$GFX" < <(strings "$SOLVER" 2>/dev/null); then
    ylw "            : note — $GFX not found in binary strings (may still work)."
  fi
fi

# 5. Python
if command -v python3 >/dev/null 2>&1; then
  grn "Python      : $(python3 --version 2>&1)"
else
  red "Python      : python3 not found (need 3.10+)."
  fail=1
fi

echo
if [ "$fail" = 0 ]; then
  grn "All checks passed. Start mining with:"
  echo "    ./ember --address <your-btx1z-address> --worker rig1"
else
  red "Some checks failed — fix the red items above, then re-run ./install.sh"
  exit 1
fi
