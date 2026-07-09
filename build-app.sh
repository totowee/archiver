#!/bin/zsh
# Build "Folder Archiver.app" (Electron) and bundle the Python engine into it.
# Run this after ANY edit to folder-archiver.py or the app/ sources.
#
# Prereqs: node + npm (Homebrew). The first run downloads Electron (~120 MB);
# later runs are near-instant. node_modules/ and dist/ are disposable build
# artifacts — `rm -rf app/node_modules dist` reclaims the space; this script
# recreates them.
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="Folder Archiver"

# 1. Dependencies (electron + @electron/packager as devDependencies).
(cd app && npm install)

# 2. Package. No asar (default off), so the engine shipped via --extra-resource
#    lands readable at Contents/Resources/folder-archiver.py, spawnable by an
#    external /usr/bin/python3 and re-syncable by the engine's self_sync().
rm -rf dist
(cd app && npx electron-packager . "$APP_NAME" \
  --platform=darwin --arch=arm64 --out=../dist --overwrite \
  --extra-resource=../folder-archiver.py \
  --icon=assets/icon.icns \
  --app-bundle-id=com.folderarchiver.gui \
  --app-version="$(node -p "require('./package.json').version")")

# 3. Replace the app at the project root with the freshly built one.
rm -rf "$APP_NAME.app"
mv "dist/$APP_NAME-darwin-arm64/$APP_NAME.app" .
rm -rf dist

# 4. Ad-hoc codesign (arm64 needs a valid signature to launch) and make the
#    bundled engine executable.
codesign --force --deep --sign - "$APP_NAME.app"
chmod +x "$APP_NAME.app/Contents/Resources/folder-archiver.py"

echo "Built $APP_NAME.app"
