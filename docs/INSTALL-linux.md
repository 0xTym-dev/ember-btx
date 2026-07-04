# Installing Ember on native Linux

## 1. Install ROCm (6.x or newer)

ROCm ships AMD's GPU compute runtime. Install it the way your distro documents;
the two common paths:

**Ubuntu / Debian:**
```bash
wget https://repo.radeon.com/amdgpu-install/latest/ubuntu/jammy/amdgpu-install_*.deb
sudo apt install ./amdgpu-install_*.deb
sudo amdgpu-install --usecase=rocm
sudo usermod -aG render,video "$USER"   # then log out/in
```

**Arch / CachyOS:**
```bash
sudo pacman -S rocm-hip-runtime rocminfo
```

Verify:
```bash
rocminfo | grep -m1 gfx      # should print your card's arch, e.g. gfx1101
```

## 2. Preflight + run

```bash
./install.sh                 # checks ROCm, GPU arch, binary, Python
./ember --address <your-btx1z-address> --worker rig1
```

## Lower power (optional)

The solver is memory-bound, so on many cards you can cap the core clock for the
same hashrate at much lower watts. On the RX 7800 XT, 1900 MHz gives full
hashrate at ~133 W instead of ~200 W. This uses the amdgpu OverDrive sysfs
interface and requires `amdgpu.ppfeaturemask=0xffffffff` on the kernel cmdline.
See `docs/POWER.md` (card-specific; community contributions welcome).

## Supported cards

See `docs/GPU-SUPPORT.md`. If `rocminfo` shows an arch not in that list, this
build doesn't include it — open an issue.
