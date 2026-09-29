#!/usr/bin/env bash
# Build the Reflect iPhone app with xcodebuild and install it on your iPhone.
# Run on the Mac (works over SSH). The iPhone must be plugged into the Mac by
# cable the first time.
#
#   ./build.sh           build, sign with your Personal Team, install, launch
#   ./build.sh --check   compile only (no signing) and run the unit tests in the simulator
set -euo pipefail
cd "$(dirname "$0")"

MODE=install
[ "${1:-}" = "--check" ] && MODE=check
BUNDLE_ID=com.neverheard.reflect
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

say() { printf '\n==> %s\n' "$*"; }
stop() { printf '\n*** %s\n\n' "$*" >&2; exit 1; }
# Runs xcodebuild with the full log in build/xcodebuild.log and shows only errors on failure.
xb() {
  mkdir -p build
  if ! xcodebuild "$@" > build/xcodebuild.log 2>&1; then
    grep -E "error:|Error |failed|Code Signing|provisioning" build/xcodebuild.log | sort -u | head -n 40 >&2
    stop "xcodebuild failed. Full log: $PWD/build/xcodebuild.log"
  fi
  grep -E "\*\* (BUILD|TEST) (SUCCEEDED|FAILED)|Executed [0-9]+ test" build/xcodebuild.log | tail -n 3
}

# --- Xcode -------------------------------------------------------------------
xcodebuild -version >/dev/null 2>&1 || stop "Xcode is not selected. Run: sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
if ! xcodebuild -checkFirstLaunchStatus >/dev/null 2>&1; then
  say "Finishing Xcode's first-launch setup (asks for your Mac password)"
  sudo xcodebuild -license accept
  sudo xcodebuild -runFirstLaunch
fi
if ! xcodebuild -showsdks 2>/dev/null | grep -q iphoneos; then
  say "Downloading the iOS platform for Xcode (one time, several GB)"
  xcodebuild -downloadPlatform iOS
fi

# --- XcodeGen ------------------------------------------------------------------
XCODEGEN=$(command -v xcodegen || true)
if [ -z "$XCODEGEN" ]; then
  if command -v brew >/dev/null 2>&1; then
    say "Installing XcodeGen with Homebrew"
    brew install xcodegen
    XCODEGEN=$(command -v xcodegen)
  else
    say "Downloading XcodeGen"
    mkdir -p .tools
    curl -fsSL -o .tools/xcodegen.zip https://github.com/yonaskolb/XcodeGen/releases/latest/download/xcodegen.zip
    unzip -oq .tools/xcodegen.zip -d .tools
    XCODEGEN="$PWD/.tools/xcodegen/bin/xcodegen"
  fi
fi
say "Generating Reflect.xcodeproj"
"$XCODEGEN" generate --quiet

if [ "$MODE" = check ]; then
  say "Compiling for iPhone (unsigned)"
  xb -project Reflect.xcodeproj -scheme Reflect -configuration Release \
    -destination 'generic/platform=iOS' -derivedDataPath build \
    CODE_SIGNING_ALLOWED=NO build
  SIM=$(xcrun simctl list devices available -j | python3 -c '
import json, sys
devices = json.load(sys.stdin)["devices"]
for runtime in sorted(devices, reverse=True):
    if "iOS" in runtime:
        for d in devices[runtime]:
            if d["name"].startswith("iPhone"):
                print(d["udid"]); sys.exit()')
  [ -n "$SIM" ] || stop "No iPhone simulator is installed."
  say "Running unit tests in the simulator"
  xb -project Reflect.xcodeproj -scheme Reflect -destination "id=$SIM" -derivedDataPath build test
  exit 0
fi

# --- Signing team -----------------------------------------------------------------
TEAM=${REFLECT_TEAM:-}
if [ -z "$TEAM" ]; then
  TEAM=$(python3 - <<'PY'
import plistlib, pathlib
path = pathlib.Path.home() / "Library/Preferences/com.apple.dt.Xcode.plist"
teams = []
def walk(node):
    if isinstance(node, dict):
        if "teamID" in node:
            teams.append((not node.get("isFreeProvisioningTeam", False), node["teamID"]))
        for value in node.values():
            walk(value)
    elif isinstance(node, list):
        for value in node:
            walk(value)
try:
    walk(plistlib.loads(path.read_bytes()))
except Exception:
    pass
# Prefer the free Personal Team.
print(sorted(teams)[0][1] if teams else "")
PY
)
fi
if [ -z "$TEAM" ]; then
  TEAM=$(security find-certificate -a -c "Apple Development" -p 2>/dev/null \
    | openssl x509 -noout -subject 2>/dev/null | sed -n 's/.*OU *= *\([A-Z0-9]\{10\}\).*/\1/p' | head -n 1)
fi
[ -n "$TEAM" ] || stop "No Apple ID is signed in to Xcode on this Mac. One-time step on the Mac's screen:
    open Xcode > Settings (Cmd+,) > Accounts > '+' > Apple ID, and sign in with your Apple ID.
    (No monitor? From Windows, connect with a VNC viewer after enabling Screen Sharing:
     sudo launchctl enable system/com.apple.screensharing && sudo launchctl bootstrap system /System/Library/LaunchDaemons/com.apple.screensharing.plist)
Then run ./build.sh again."
say "Signing with team $TEAM"

# --- iPhone -------------------------------------------------------------------------
xcrun devicectl list devices --json-output "$TMP/devices.json" >/dev/null 2>&1 || true
DEVICE_LINE=$(python3 - "$TMP/devices.json" <<'PY'
import json, sys
try:
    devices = json.load(open(sys.argv[1]))["result"]["devices"]
except Exception:
    devices = []
for d in devices:
    hw, conn, props = d.get("hardwareProperties", {}), d.get("connectionProperties", {}), d.get("deviceProperties", {})
    if hw.get("platform") == "iOS" and hw.get("deviceType") == "iPhone" and conn.get("pairingState") == "paired":
        print(d["identifier"], hw.get("udid", ""), props.get("name", "iPhone").replace(" ", "_"))
        break
PY
)
read -r DEVICE_ID DEVICE_UDID DEVICE_NAME <<< "${DEVICE_LINE:-}" || true
[ -n "${DEVICE_ID:-}" ] || stop "No paired iPhone found. Plug the iPhone into the Mac with a cable, unlock it and tap 'Trust' (enter the passcode), then run ./build.sh again.
   If it still is not found: xcrun devicectl list devices"
say "Building for ${DEVICE_NAME//_/ }"

# Over SSH the login keychain (which holds the signing key) is locked.
if [ -n "${SSH_CONNECTION:-}" ]; then
  say "Unlocking the login keychain for code signing (enter your Mac password)"
  security unlock-keychain "$HOME/Library/Keychains/login.keychain-db"
fi

xb -project Reflect.xcodeproj -scheme Reflect -configuration Release \
  -destination "id=$DEVICE_UDID" -derivedDataPath build \
  -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
  DEVELOPMENT_TEAM="$TEAM" CODE_SIGN_STYLE=Automatic build
APP=build/Build/Products/Release-iphoneos/Reflect.app
[ -d "$APP" ] || stop "The build did not produce $APP (see the errors above)."

say "Installing on the iPhone"
xcrun devicectl device install app --device "$DEVICE_ID" "$APP"

say "Launching Reflect"
if ! xcrun devicectl device process launch --device "$DEVICE_ID" "$BUNDLE_ID" >/dev/null 2>&1; then
  cat <<'MSG'

Reflect is installed. The first time, iOS blocks apps from a new developer:
  1. On the iPhone: Settings > Privacy & Security > Developer Mode > On (the iPhone restarts).
  2. Settings > General > VPN & Device Management > your Apple ID > Trust.
  3. Open Reflect from the Home Screen.
MSG
fi
say "Done. Free Apple IDs sign apps for 7 days: run ./build.sh again when Reflect stops opening."
