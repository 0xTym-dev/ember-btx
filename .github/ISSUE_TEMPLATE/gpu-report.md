---
name: GPU report (card works / doesn't work)
about: Tell us how amdbtx ran on your card so we can mark it verified
title: "[GPU] <your card model> — <works / crashes / slow>"
labels: gpu-report
---

**Card model:** (e.g. RX 7900 XTX)

**GPU arch:** (output of `rocminfo | grep -m1 gfx`)

**Environment:** native Linux / WSL2  (distro + ROCm version)

**Result:** works / crashes / builds but no shares / other

**Observed hashrate (N/s):** (from the miner log, if it ran)

**Command you ran:**
```
./amdbtx --address ... --worker ...
```

**Relevant log output / error:**
```
paste here
```

**Anything else:** (power, temps, whether a game was running, etc.)
