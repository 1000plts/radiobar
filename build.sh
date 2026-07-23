#!/bin/zsh
# Build RadioBar.app and RadioBar-<version>.dmg
#
# IMPORTANT: build with Python 3.12, NOT 3.14. Python 3.14's C-stack overflow
# guard is miscalibrated inside PyInstaller bundles and makes json parsing on
# worker threads raise RecursionError (breaks station search + NTS metadata).
# The .venv312 virtualenv below pins the interpreter.
set -euo pipefail

VERSION="1.1.3"
PY=.venv312/bin/python

# One-time setup: python3.12 -m venv .venv312 && \
#   .venv312/bin/pip install rumps pyobjc-framework-AVFoundation certifi pyinstaller

rm -rf build dist dmg-staging
"$PY" -m PyInstaller --noconfirm --windowed --name RadioBar \
    --osx-bundle-identifier pl.jeremiasz.radiobar radio_bar.py

# Menubar-only app: no Dock icon, no Cmd-Tab entry; label the version
PL=dist/RadioBar.app/Contents/Info.plist
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PL"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${VERSION}" "$PL"

# Allow http:// radio streams. macOS App Transport Security blocks non-TLS
# connections by default, which silently breaks the many http-only stations
# in the Radio Browser directory (AVPlayer just reports the item as failed).
/usr/libexec/PlistBuddy -c "Add :NSAppTransportSecurity dict" "$PL"
/usr/libexec/PlistBuddy -c "Add :NSAppTransportSecurity:NSAllowsArbitraryLoads bool true" "$PL"

codesign --force --deep -s - dist/RadioBar.app

mkdir dmg-staging
cp -R dist/RadioBar.app dmg-staging/
ln -s /Applications dmg-staging/Applications
cp install-README.txt dmg-staging/README.txt

hdiutil create -volname "RadioBar" -srcfolder dmg-staging -ov -format UDZO \
    "RadioBar-${VERSION}.dmg"
echo "Built RadioBar-${VERSION}.dmg"
