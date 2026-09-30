"""Tests for tools/devices.py (run with `python -m pytest tools` from reflect/ios)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import devices

SCRIPT = Path(__file__).with_name("devices.py")

SHOW_DESTINATIONS = """\
Command line invocation:
    /Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild -project Reflect.xcodeproj -scheme Reflect -showdestinations

--- xcodebuild: WARNING: Using the first of multiple matching destinations:

	Available destinations for the "Reflect" scheme:
		{ platform:macOS, arch:arm64, variant:Designed for [iPad,iPhone], id:00006020-001E2D3A0C40001E, name:My Mac }
		{ platform:iOS, arch:arm64, id:00008130-001A2B3C4D5E001C, name:Heineken }
		{ platform:iOS, id:dvtdevice-DVTiPhonePlaceholder-iphoneos:placeholder, name:Any iOS Device }
		{ platform:iOS Simulator, id:1F2E3D4C-0000-0000-0000-000000000000, OS:26.0, name:iPhone 17 }

	Ineligible destinations for the "Reflect" scheme:
		{ platform:iOS, arch:arm64, id:00008101-000A11223344001E, name:Old Phone, error:Developer Mode disabled. To use Old Phone for development, enable Developer Mode in Settings → Privacy & Security. }
"""

DEVICECTL = {
    "result": {
        "devices": [
            {"identifier": "AAAA", "hardwareProperties": {"platform": "iOS", "deviceType": "iPad", "udid": "ipad"},
             "connectionProperties": {"pairingState": "paired"}, "deviceProperties": {"name": "iPad"}},
            {"identifier": "BBBB", "hardwareProperties": {"platform": "iOS", "deviceType": "iPhone", "udid": "unp"},
             "connectionProperties": {"pairingState": "unpaired"}, "deviceProperties": {"name": "Other"}},
            {"identifier": "CCCC", "hardwareProperties": {"platform": "iOS", "deviceType": "iPhone", "udid": "00008130-001A2B3C4D5E001C"},
             "connectionProperties": {"pairingState": "paired", "tunnelState": "connected"},
             "deviceProperties": {"name": "Heineken", "osVersionNumber": "26.0", "developerModeStatus": "disabled", "ddiServicesAvailable": False}},
        ]
    }
}


def test_phone_prefers_paired_iphone(tmp_path):
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(DEVICECTL))
    out = subprocess.run([sys.executable, str(SCRIPT), "phone", str(path)], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "CCCC|00008130-001A2B3C4D5E001C|Heineken|26.0|paired|disabled|False|connected"


def test_phone_handles_missing_or_broken_file(tmp_path):
    assert devices.phones(str(tmp_path / "nope.json")) == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert devices.phones(str(broken)) == []


def test_destination_matching():
    available, ineligible = devices.parse_destinations(SHOW_DESTINATIONS)
    assert len(available) == 4 and len(ineligible) == 1
    assert devices.pick_destination(available, "00008130-001A2B3C4D5E001C", "x")["name"] == "Heineken"
    assert devices.pick_destination(available, "different-udid", "Heineken")["id"] == "00008130-001A2B3C4D5E001C"
    assert devices.pick_destination(available, "different-udid", "other name")["name"] == "Heineken"  # only one phone
    assert devices.pick_destination([], "a", "b") is None


def test_placeholder_and_simulator_are_not_devices():
    available, _ = devices.parse_destinations(SHOW_DESTINATIONS)
    physical = [entry["name"] for entry in available if devices.is_physical_iphone(entry)]
    assert physical == ["Heineken"]


def test_report_names_the_reason():
    text = devices.report(SHOW_DESTINATIONS)
    assert "Heineken (00008130-001A2B3C4D5E001C)" in text
    assert "Xcode refuses Old Phone: Developer Mode disabled." in text
    assert devices.report("nothing useful here") == "Devices Xcode can build for: none"
