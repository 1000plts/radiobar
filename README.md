# RadioBar

A minimal macOS menu bar internet radio player.





![menubar](https://img.shields.io/badge/macOS-Apple%20Silicon-black) ![python](https://img.shields.io/badge/built%20with-Python%20%2B%20PyObjC-blue)

## Features

- Lives entirely in the menu bar — no Dock icon, no windows
- **Left-click** the menu bar item to pause/resume, **right-click** for the station menu
- Fixed-width scrolling marquee shows the current station and show title
- Live show names for [NTS](https://www.nts.live) channels via their API; ICY stream metadata for everything else (including legacy Shoutcast servers)
- Native "Manage Stations" panel: add, remove, and drag-to-reorder up to 10 stations
- Audio via macOS AVFoundation — no VLC or other dependencies

## Install

1. Download `RadioBar-x.x.dmg` from [Releases](../../releases/latest)
2. Drag **RadioBar.app** to **Applications**
3. First launch only: **right-click → Open**, then confirm. (The app is not
   notarized with Apple; this one-time step tells Gatekeeper you trust it.)

Requires an Apple Silicon Mac (M1 or newer).

## Build from source

```bash
python3 -m pip install rumps pyobjc-framework-AVFoundation pyinstaller
./build.sh
```

The DMG lands in the project root. Stations are stored in
`~/.radio_bar_config.json`.

## Run as a plain script

```bash
python3 -m pip install rumps pyobjc-framework-AVFoundation
python3 radio_bar.py
```
