#!/usr/bin/env bash
# Build the Reflect iPhone app with xcodebuild and install it on your iPhone.
# Run on the Mac (works over SSH). The iPhone must be plugged into the Mac by
# cable, unlocked, trusted, with Developer Mode on (the script checks and tells you).
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
    grep -A 25 "Unable to find a destination" build/xcodebuild.log | head -n 40 >&2 || true
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
list_phone() {
  xcrun devicectl list devices --json-output "$TMP/devices.json" >/dev/null 2>&1 || true
  PHONE_LINE=$(python3 tools/devices.py phone "$TMP/devices.json")
  DEVICE_ID=""; DEVICE_UDID=""; DEVICE_NAME=""; DEVICE_OS=""; PAIRING=""; DEVMODE=""; DDI=""; TUNNEL=""
  if [ -n "$PHONE_LINE" ]; then
    # shellcheck disable=SC2034  # DDI and TUNNEL are read for completeness only
    IFS='|' read -r DEVICE_ID DEVICE_UDID DEVICE_NAME DEVICE_OS PAIRING DEVMODE DDI TUNNEL <<< "$PHONE_LINE"
  fi
}

list_phone
[ -n "$DEVICE_ID" ] || stop "No iPhone found. Plug the iPhone into this Mac with a cable and unlock it.
   Close the 'iPhone Mirroring' window if it is open: while it is mirrored the phone stays locked.
   Then run this again. (To see what the Mac sees: xcrun devicectl list devices)"

if [ "$PAIRING" != "paired" ]; then
  say "$DEVICE_NAME is not trusted by this Mac yet: unlock it and tap 'Trust' on the iPhone (enter its passcode)"
  xcrun devicectl manage pair --device "$DEVICE_ID" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6; do
    sleep 5
    list_phone
    [ "$PAIRING" = "paired" ] && break
  done
  [ "$PAIRING" = "paired" ] || stop "$DEVICE_NAME is still not trusted. Unlock the iPhone (not iPhone Mirroring), tap 'Trust' on it, enter the passcode, then run this again."
fi

if [ "$DEVMODE" = "disabled" ]; then
  stop "Developer Mode is off on $DEVICE_NAME, so Xcode cannot install apps on it. Turn it on first:
   iPhone: Settings > Privacy & Security > Developer Mode > On, then Restart when it asks and unlock the phone.
   (If you do not see 'Developer Mode': keep the iPhone plugged in and unlocked, open Xcode >
    Window > Devices and Simulators, wait until $DEVICE_NAME appears there, then look again.)
   Then run this again."
fi

# Ask Xcode itself which device it will build for; it may still be preparing a new phone for a few minutes.
DEST_ID=""
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  DEST_TEXT=$(xcodebuild -project Reflect.xcodeproj -scheme Reflect -showdestinations 2>&1 || true)
  DEST_LINE=$(printf '%s\n' "$DEST_TEXT" | python3 tools/devices.py dest "$DEVICE_UDID" "$DEVICE_NAME")
  if [ -n "$DEST_LINE" ]; then
    DEST_ID=${DEST_LINE%%|*}
    break
  fi
  [ "$attempt" = 1 ] && say "Waiting for Xcode to get $DEVICE_NAME ready (the first time takes a few minutes; keep it unlocked)"
  sleep 10
done
if [ -z "$DEST_ID" ]; then
  printf '\n%s\n' "$(printf '%s\n' "$DEST_TEXT" | python3 tools/devices.py report)" >&2
  stop "Xcode does not list $DEVICE_NAME (iOS ${DEVICE_OS:-?}) as a device it can build for. Check, in this order:
   1. The iPhone is unlocked, plugged in by cable, and 'Trust' was tapped.
   2. Developer Mode is on (Settings > Privacy & Security > Developer Mode).
   3. Xcode is new enough for this iOS version: run  xcodebuild -version  and update Xcode from the App Store if needed.
   4. Open Xcode > Window > Devices and Simulators and wait until the iPhone shows no 'preparing' message.
   Then run this again. Full list Xcode printed:  xcodebuild -project Reflect.xcodeproj -scheme Reflect -showdestinations"
fi
say "Building for $DEVICE_NAME (iOS $DEVICE_OS)"

# Over SSH the login keychain (which holds the signing key) is locked.
if [ -n "${SSH_CONNECTION:-}" ]; then
  say "Unlocking the login keychain for code signing (enter your Mac password)"
  security unlock-keychain "$HOME/Library/Keychains/login.keychain-db"
fi

xb -project Reflect.xcodeproj -scheme Reflect -configuration Release \
  -destination "id=$DEST_ID" -derivedDataPath build \
  -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
  DEVELOPMENT_TEAM="$TEAM" CODE_SIGN_STYLE=Automatic build
APP=build/Build/Products/Release-iphoneos/Reflect.app
[ -d "$APP" ] || stop "The build did not produce $APP (see the errors above)."

say "Installing on the iPhone"
xcrun devicectl device install app --device "$DEVICE_ID" "$APP"

say "Launching Reflect"
if ! xcrun devicectl device process launch --device "$DEVICE_ID" "$BUNDLE_ID" >/dev/null 2>&1; then
  cat <<'MSG'

Reflect is installed. The first time, iOS blocks apps from a new developer, so on the iPhone:
  1. Settings > General > VPN & Device Management > your Apple ID > Trust.
  2. Open Reflect from the Home Screen.
MSG
fi
say "Done. Free Apple IDs sign apps for 7 days: run ./build.sh again when Reflect stops opening."
