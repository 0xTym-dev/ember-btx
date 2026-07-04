# Ember GPU support matrix

The release binary is a **fat binary** — it contains compiled GPU code for every
architecture below, and the ROCm runtime automatically selects the one matching
your card at launch. No per-card download.

| Arch (gfx) | Radeon cards (examples) | Gen | Linux native | WSL2 | Tested |
|---|---|---|---|---|---|
| gfx1030 | RX 6800, 6800 XT, 6900 XT, 6950 XT | RDNA2 | ✅ | ❌¹ | community |
| gfx1031 | RX 6700 XT, 6750 XT | RDNA2 | ✅ | ❌¹ | community |
| gfx1032 | RX 6600, 6600 XT, 6650 XT | RDNA2 | ✅ | ❌¹ | community |
| gfx1100 | RX 7900 XTX, 7900 XT, 7900 GRE | RDNA3 | ✅ | ✅ | community |
| **gfx1101** | **RX 7800 XT, 7700 XT** | **RDNA3** | ✅ | ✅ | **✅ dev card** |
| gfx1102 | RX 7600, 7600 XT | RDNA3 | ✅ | ✅ | community |
| gfx1200 | RX 9060, 9060 XT | RDNA4 | ✅ | ✅ | community |
| gfx1201 | RX 9070, 9070 XT, 9070 GRE | RDNA4 | ✅ | ✅ | community |

¹ RDNA2 is **not** supported by ROCm on WSL2 — use native Linux for RX 6000 cards.

**Tested** = actually run by us. Only the RX 7800 XT (gfx1101) is dev-verified;
every other row is compiled-and-should-work but needs a community report. If you
run Ember on any other card, please open a GitHub issue with the output of
`rocminfo | grep gfx` and your observed N/s — we'll mark it verified.

## Finding your card's arch

```bash
rocminfo | grep -m1 gfx      # e.g. "Name: gfx1101"
```

If your card's arch is not in the table, it is not in this build. RDNA2 APUs
(gfx1035/1036), older GCN, and CDNA (data-center) cards are out of scope for
v0.1 — open an issue if you need one added; adding an arch is a rebuild.

## Notes on WSL2

- Requires **ROCm 7.2+** and the Windows driver **AMD Adrenalin 26.1.1 (WSL2)**.
- The GPU is reached through `/dev/dxg`, not the amdgpu kernel module, so
  sysfs-based tools (clock/power tuning, `rocm-smi` power draw) don't work inside
  WSL2 — manage clocks/power via Adrenalin on the Windows side instead.
- See `docs/INSTALL-wsl2.md`.
