# Installing Ember on Windows via WSL2

Ember has no native Windows build, but ROCm 7.2+ officially supports Radeon
GPUs inside WSL2 — so the same Linux binary runs on Windows through WSL2 with
no emulation. This works for **RX 7000 (RDNA3) and RX 9000 (RDNA4)** cards. It
does **not** work for RX 6000 (RDNA2) — those need native Linux.

## 1. Windows side

1. Update to **Windows 11** (or Windows 10 22H2+) with WSL2.
2. Install the **AMD Adrenalin 26.1.1 for WSL2** driver from AMD's site. This
   is the specific driver that exposes the GPU to WSL2 via `/dev/dxg`.
3. Open PowerShell as Administrator:
   ```powershell
   wsl --install Ubuntu-24.04
   wsl --update
   ```

## 2. Inside Ubuntu (WSL2)

Install ROCm with the WSL usecase (no kernel module — WSL provides the GPU):

```bash
sudo apt update
wget https://repo.radeon.com/amdgpu-install/latest/ubuntu/jammy/amdgpu-install_*.deb
sudo apt install ./amdgpu-install_*.deb
amdgpu-install --usecase=wsl,rocm --no-dkms
```

Verify the GPU is visible:

```bash
rocminfo | grep -m1 gfx      # should print your card, e.g. gfx1101
```

If `rocminfo` shows your card, the hard part is done.

## 3. Run Ember

```bash
./ember --address <your-btx-address> --worker rig1
```

## Limitations on WSL2

- **RDNA2 (RX 6000) is not supported on WSL2.** Use native Linux for those.
- **No clock/power tuning from inside WSL2.** The energy profile (core-clock
  cap for lower watts) relies on the amdgpu sysfs interface, which WSL2 does not
  expose. Set your power/clock limits in **AMD Adrenalin on the Windows side**
  instead. `rocm-smi` power readings are also unavailable in WSL2.
- Expect a small performance overhead vs native Linux from the `/dev/dxg`
  translation layer. Report your numbers so we can quantify it.

## Troubleshooting

- `rocminfo` shows no gfx device → the Adrenalin WSL2 driver isn't installed or
  Windows needs a reboot; confirm `ls /dev/dxg` exists.
- `HIP error: no ROCm-capable device` → run `amdgpu-install` with
  `--usecase=wsl,rocm` (not the plain Linux usecase, which installs the kernel
  module WSL can't use).
