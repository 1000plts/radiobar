#!/bin/zsh
# Build RadioBar.app and RadioBar-<version>.dmg
set -euo pipefail

VERSION="1.0"

rm -rf build dist dmg-staging
python3 -m PyInstaller --noconfirm --windowed --name RadioBar \
    --osx-bundle-identifier pl.jeremiasz.radiobar radio_bar.py

# Menubar-only app: no Dock icon, no Cmd-Tab entry
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" \
    dist/RadioBar.app/Contents/Info.plist
codesign --force --deep -s - dist/RadioBar.app

mkdir dmg-staging
cp -R dist/RadioBar.app dmg-staging/
ln -s /Applications dmg-staging/Applications
cp install-README.txt dmg-staging/README.txt

hdiutil create -volname "RadioBar" -srcfolder dmg-staging -ov -format UDZO \
    "RadioBar-${VERSION}.dmg"
echo "Built RadioBar-${VERSION}.dmg"
