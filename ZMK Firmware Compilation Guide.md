---
title: ZMK Firmware Compilation Guide
date: 2026-04-14
tags:
  - zmk
  - firmware
  - keyboard
  - sweep
  - docker
aliases:
  - ZMK Build Guide
  - Sweep Firmware Guide
---

# ZMK Firmware Compilation Guide

## Overview

This guide covers compiling ZMK firmware for the **Ferris Sweep (nxtkb variant)** using Docker. The keyboard uses `nice_nano` controllers with a split configuration (left + right halves).

## Prerequisites

- Docker installed and user in `docker` group
- ZMK Docker image pulled: `zmkfirmware/zmk-dev-arm:3.5`
- Run `setup-zmk-docker.sh` in this folder for automated setup
- Source repo: `~/git_repo/Sweep-Pro` (forked from `nxtkb/Sweep-Pro`)

## Quick Reference

| Item | Value |
|------|-------|
| Docker image | `zmkfirmware/zmk-dev-arm:3.5` |
| Board | `nice_nano` |
| Shields | `sweep_left`, `sweep_right`, `settings_reset` |
| Output format | `.uf2` |
| ZMK fork | `lynnlee0522/zmk` branch `ink_rotation` |
| Zephyr | `v4.1.0+zmk-fixes` |

## Build Process

### 1. Prepare Workspace

```bash
TMPDIR="$HOME/my_sync/Obsidian/OPC/tmp/zmk-build"
mkdir -p "$TMPDIR"
cp -r ~/git_repo/Sweep-Pro "$TMPDIR/Sweep-Pro"
```

> [!warning] Docker creates files as root
> All files inside the mounted volume will be owned by root. To copy files out, use `docker run --rm -v ... busybox cp`.

### 2. Initialize and Download Dependencies

This step downloads ~5-10GB of ZMK, Zephyr, and modules. **Only needed once per workspace.**

```bash
docker run --rm \
  -v $TMPDIR/Sweep-Pro:/workspace \
  -w /workspace \
  zmkfirmware/zmk-dev-arm:3.5 \
  bash -c '
    git config --global --add safe.directory "*" &&
    west init -l config &&
    west update &&
    west zephyr-export
  '
```

### 3. Build Firmware Targets

> [!important] zephyr-export must run first
> Every Docker session is fresh. You **must** run `west zephyr-export` before building, or CMake will fail.

#### Build sweep_left (with ZMK Studio snippet)

```bash
docker run --rm \
  -v $TMPDIR/Sweep-Pro:/workspace \
  -w /workspace \
  zmkfirmware/zmk-dev-arm:3.5 \
  bash -c '
    git config --global --add safe.directory "*" &&
    west zephyr-export &&
    cd /workspace/zmk/app &&
    cmake -B build/left -GNinja \
      -DBOARD=nice_nano \
      -DSHIELD=sweep_left \
      -DZMK_CONFIG=/workspace/config \
      -DBOARD_ROOT=/workspace \
      -DSNIPPET=studio-rpc-usb-uart &&
    ninja -C build/left
  '
```

#### Build sweep_right

```bash
docker run --rm \
  -v $TMPDIR/Sweep-Pro:/workspace \
  -w /workspace \
  zmkfirmware/zmk-dev-arm:3.5 \
  bash -c '
    git config --global --add safe.directory "*" &&
    west zephyr-export &&
    cd /workspace/zmk/app &&
    cmake -B build/right -GNinja \
      -DBOARD=nice_nano \
      -DSHIELD=sweep_right \
      -DZMK_CONFIG=/workspace/config \
      -DBOARD_ROOT=/workspace &&
    ninja -C build/right
  '
```

#### Build settings_reset

```bash
docker run --rm \
  -v $TMPDIR/Sweep-Pro:/workspace \
  -w /workspace \
  zmkfirmware/zmk-dev-arm:3.5 \
  bash -c '
    git config --global --add safe.directory "*" &&
    west zephyr-export &&
    cd /workspace/zmk/app &&
    cmake -B build/reset -GNinja \
      -DBOARD=nice_nano \
      -DSHIELD=settings_reset \
      -DZMK_CONFIG=/workspace/config \
      -DBOARD_ROOT=/workspace &&
    ninja -C build/reset
  '
```

### 4. Extract Firmware

Firmware is at `zmk/app/build/{left,right,reset}/zephyr/zmk.uf2`. Copy out using Docker (files are root-owned):

```bash
OUTPUT_DIR="$HOME/my_sync/BkUp/ferris_sweep/current"
mkdir -p "$OUTPUT_DIR"

docker run --rm \
  -v $TMPDIR/Sweep-Pro:/workspace \
  -v $OUTPUT_DIR:/output \
  busybox sh -c '
    cp /workspace/zmk/app/build/left/zephyr/zmk.uf2  /output/sweep_left.uf2 &&
    cp /workspace/zmk/app/build/right/zephyr/zmk.uf2 /output/sweep_right.uf2 &&
    cp /workspace/zmk/app/build/reset/zephyr/zmk.uf2 /output/settings_reset.uf2
  '
```

### 5. Cleanup

```bash
docker run --rm -v $TMPDIR:/cleanup busybox sh -c 'rm -rf /cleanup/Sweep-Pro'
```

## Building a Different Version (e.g., Upstream)

To build the upstream (origin) firmware instead of your fork's HEAD:

1. Extract upstream versions of the files changed by your commits:
   - `boards/shields/sweep/sweep.dtsi`
   - `boards/shields/sweep/sweep_right.overlay`
   - `config/sweep.keymap`
2. Overwrite them in the workspace (reuse existing west deps — no re-download):
   ```bash
   docker run --rm \
     -v $TMPDIR/Sweep-Pro:/workspace \
     -v /path/to/upstream/files:/upstream \
     busybox sh -c '
       cp /upstream/origin_sweep.dtsi /workspace/boards/shields/sweep/sweep.dtsi &&
       cp /upstream/origin_sweep_right.overlay /workspace/boards/shields/sweep/sweep_right.overlay &&
       cp /upstream/origin_sweep.keymap /workspace/config/sweep.keymap &&
       rm -rf /workspace/zmk/app/build/left /workspace/zmk/app/build/right /workspace/zmk/app/build/reset
     '
   ```
3. Rebuild all 3 targets (step 3 above)
4. Copy firmware to `~/my_sync/BkUp/ferris_sweep/origin/`

## Repo Structure

```
Sweep-Pro/
├── boards/shields/sweep/    # Shield definitions (overlay, dtsi, Kconfig)
├── config/
│   ├── west.yml             # West manifest (ZMK fork + modules)
│   ├── sweep.conf           # Shared config (BLE, battery, mouse, etc.)
│   ├── sweep_left.conf      # Left-specific (e-ink display, WPM)
│   ├── sweep_right.conf     # Right-specific (I2C, trackpad, pointing)
│   ├── sweep.keymap         # Keymap (layers, behaviors, encoders)
│   └── behaviors/           # Custom behavior definitions
├── build.yaml               # GitHub Actions build matrix
└── zephyr/module.yml        # Zephyr module registration (board_root)
```

## Key CMake Flags

| Flag | Purpose |
|------|---------|
| `-DBOARD=nice_nano` | Target MCU board |
| `-DSHIELD=sweep_left` | Keyboard shield |
| `-DZMK_CONFIG=/workspace/config` | Path to user config directory |
| `-DBOARD_ROOT=/workspace` | **Required** for shield/board discovery |
| `-DSNIPPET=studio-rpc-usb-uart` | ZMK Studio support (left half only) |

## Troubleshooting

> [!bug] "Shield not found" error
> Ensure `-DBOARD_ROOT=/workspace` is set. Without it, CMake cannot find the shield definitions in `boards/shields/sweep/`.

> [!bug] "Zephyr CMake package not found"
> Run `west zephyr-export` in the same Docker session before building. Each `docker run` starts fresh.

> [!bug] "detected dubious ownership" git error
> Add `git config --global --add safe.directory "*"` at the start of the Docker command.

> [!bug] "Permission denied" when copying files
> Docker-created files are root-owned. Use the `busybox cp` pattern shown in step 4 to copy files out.

## Flashing

1. Connect the nice_nano via USB
2. Double-tap the reset button to enter bootloader mode
3. A USB mass storage device appears (e.g., `NICENANO`)
4. Copy the appropriate `.uf2` file:
   - `sweep_left.uf2` → left half
   - `sweep_right.uf2` → right half
   - `settings_reset.uf2` → use when BLE pairing issues occur (flash to both halves, then re-flash with actual firmware)

## Related

- Repo: `~/git_repo/Sweep-Pro`
- Upstream: `https://github.com/nxtkb/Sweep-Pro`
- Firmware output: `~/my_sync/BkUp/ferris_sweep/`
- Install script: `~/my_sync/BkUp/ferris_sweep/setup-zmk-docker.sh`
