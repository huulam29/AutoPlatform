#!/usr/bin/env bash
# Build agentline.app (SwiftUI app + bundled engine), ad-hoc signed.
#
#   macos/scripts/build_app.sh              # -> macos/build/Agentline.app
#   macos/scripts/build_app.sh --backend    # also (re)build the engine bundle first
#   macos/scripts/build_app.sh --install    # then copy to /Applications (or ~/Applications)
#
# The engine bundle comes from macos/build/backend/ (made by macos/scripts/build_backend.sh).
# Without it the app still builds, but only runs in dev mode (AGENTLINE_DEV_PYTHON=...).
set -euo pipefail

MAC="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$MAC/build"
APP="$BUILD/Agentline.app"
INSTALL=0
BACKEND=0
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --backend) BACKEND=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ $BACKEND == 1 ]]; then
  "$MAC/scripts/build_backend.sh"
fi

echo "==> swift build -c release"
(cd "$MAC" && swift build -c release)
BIN="$(cd "$MAC" && swift build -c release --show-bin-path)/Agentline"

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Agentline"

VERSION="0.1.0"
BUILD_NUMBER="$(date +%Y%m%d%H%M)"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleExecutable</key><string>Agentline</string>
  <key>CFBundleIdentifier</key><string>dev.agentline.app</string>
  <key>CFBundleName</key><string>agentline</string>
  <key>CFBundleDisplayName</key><string>agentline</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$BUILD_NUMBER</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>15.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSSupportsAutomaticTermination</key><false/>
  <key>NSSupportsSuddenTermination</key><false/>
  <key>NSUserNotificationAlertStyle</key><string>alert</string>
  <key>NSHumanReadableCopyright</key><string>Built locally for personal use.</string>
</dict>
</plist>
PLIST

echo "==> icon"
ICONSET="$BUILD/AppIcon.iconset"
if [[ ! -f "$BUILD/AppIcon.icns" || "$MAC/scripts/make_icon.swift" -nt "$BUILD/AppIcon.icns" ]]; then
  rm -rf "$ICONSET"
  swift "$MAC/scripts/make_icon.swift" "$ICONSET" >/dev/null
  iconutil -c icns "$ICONSET" -o "$BUILD/AppIcon.icns"
  rm -rf "$ICONSET"
fi
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

if [[ -x "$BUILD/backend/python/bin/python3" ]]; then
  echo "==> bundling the engine from $BUILD/backend"
  ditto "$BUILD/backend" "$APP/Contents/Resources/backend"
else
  echo "warning: no engine bundle at $BUILD/backend (run macos/scripts/build_backend.sh," >&2
  echo "         or pass --backend). The app will only run with AGENTLINE_DEV_PYTHON set." >&2
fi

echo "==> ad-hoc signing"
codesign --force --deep -s - "$APP"
codesign --verify --deep "$APP" && echo "signature ok"
echo "built $APP ($(du -sh "$APP" | cut -f1))"

if [[ $INSTALL == 1 ]]; then
  DEST=/Applications
  if [[ ! -w "$DEST" ]]; then
    DEST="$HOME/Applications"
    mkdir -p "$DEST"
  fi
  if pgrep -x Agentline >/dev/null; then
    echo "==> quitting the running app"
    osascript -e 'tell application id "dev.agentline.app" to quit' >/dev/null 2>&1 || true
    for _ in $(seq 1 50); do pgrep -x Agentline >/dev/null || break; sleep 0.2; done
  fi
  rm -rf "$DEST/Agentline.app"
  ditto "$APP" "$DEST/Agentline.app"
  echo "installed $DEST/Agentline.app"
fi
