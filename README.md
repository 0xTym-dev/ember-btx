# Ember — AMD GPU miner for BTX

**The first GPU miner for the [BTX](https://pool.minebtx.com) MatMul proof-of-work
that runs on AMD Radeon cards.** The official easyBTX/minebtx solver is NVIDIA-only;
Ember is a HIP/ROCm port of the reference kernels, tuned for RDNA3, so Radeon
owners can finally mine BTX at competitive efficiency.

> Status: **early access (v0.1, Linux)**. Developed and measured on a Radeon
> RX 7800 XT. Other RDNA2/RDNA3/RDNA4 cards are expected to work but are
> community-verified — please open an issue with your results.

## Performance (RX 7800 XT, gfx1101)

| Metric | Value |
|---|---|
| Effective pool rate | ~2,900 N/s (digests/s) |
| Power (with clock cap) | ~133 W |
| Stock power, same hashrate | ~200 W → clock-cap saves ~35% |

The solver is **memory-bound**: capping the core clock at ~1900 MHz gives the
same hashrate as stock at far lower power. See `docs/` for the energy profile.

## Requirements

- AMD Radeon RX 6000 / 7000 / 9000 series (RDNA2/3/4)
- **Linux** with ROCm 6.x+ (native), **or Windows via WSL2** with ROCm 7.2+
  (see below)
- Python 3.10+

## Quick start (Linux, native)

```bash
# 1. install ROCm (distro-specific — see docs/INSTALL-linux.md)
# 2. preflight check (ROCm, GPU arch, binary checksum, Python):
./install.sh
# 3. mine to your payout address:
./ember --address <your-btx-address> --worker rig1
```

Supported cards are listed in [`docs/GPU-SUPPORT.md`](docs/GPU-SUPPORT.md).
Everything after `--worker` is passed through to the miner, so you can add
`--batch-size N`, `--log-level DEBUG`, etc.

## Windows (WSL2)

ROCm 7.2+ officially supports Radeon GPUs inside WSL2, so the same Linux binary
runs on Windows without a native port:

1. Install the **AMD Adrenalin 26.1.1 (WSL2)** Windows driver.
2. `wsl --install Ubuntu-24.04`
3. Inside WSL: `amdgpu-install --usecase=wsl,rocm --no-dkms`
4. Run Ember as above.

Notes: on WSL2 the core-clock energy profile is managed by the Windows driver
(Adrenalin), not by Ember. RDNA2 (RX 6000) is **not** supported on WSL2 — use
native Linux for those. See `docs/INSTALL-wsl2.md`.

## Developer fee

Ember charges a transparent **1% developer fee**. For roughly 1% of mining
wall-time the solver mines to the developers' address instead of yours; the
rest of the time is 100% yours. This funds ongoing kernel optimization and
support.

- The fee is **1%** (about 36 seconds per hour).
- It is **disclosed**: the miner logs the fee at startup
  (`{"event":"dev_fee","fraction":0.01,...}`) and every fee round.
- The fee lives in the compiled solver, so it is honest by construction — and
  blocking the fee connection at the firewall does **not** give you the 1% back
  (the miner enforces the fraction regardless), so there is no reason to try.

If you'd rather not pay it, don't use Ember — but we think 1% for the only
working AMD BTX miner, kept fast, is a fair deal.

## How it works

Ember is two parts:
- **Solver** (`btx-gbt-solve`, closed-source binary): the HIP/ROCm compute core,
  a port of the BTX reference MatMul-PoW kernels with RDNA3-specific
  optimizations (specialized factored-RHS / perturbed-matrix kernels, GPU
  scan-prefetch pipeline). Distributed as a signed binary; the source is not
  public.
- **Client** (`dexbtx-miner`, open-source Python): Stratum connection, job
  management, share submission, auto-heal. Based on the MIT-licensed
  dexbtx-miner client.

## Verifying your download

Every release ships a `SHA256SUMS`. Check the binary before running it:

```bash
cd bin && sha256sum -c ../SHA256SUMS   # must print: btx-gbt-solve: OK
```

(`./install.sh` also does this automatically.)

## License

The Python client (`client/`) is **MIT** — see [`LICENSE`](LICENSE). The solver
binary (`bin/btx-gbt-solve`) is **proprietary**, built on MIT-licensed BTX /
Bitcoin Core code; see [`TERMS.md`](TERMS.md) for the binary's terms and
[`NOTICE`](NOTICE) for the reproduced upstream MIT license. In short: you may
use Ember to mine; you may not redistribute or modify the solver binary.

## Support

Open a GitHub issue with the output of `rocminfo` and your card model. RDNA4 and
WSL2 reports especially welcome while we build out the support matrix.
