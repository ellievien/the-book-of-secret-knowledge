#!/usr/bin/env python3
"""Helpers for build.sh: read `devicectl list devices` JSON and Xcode's `-showdestinations` text.

    devices.py phone  DEVICES.json          -> id|udid|name|os|pairing|developer_mode|ddi|tunnel
    devices.py dest   UDID NAME  < text     -> Xcode's id|name for the physical iPhone, if it lists one
    devices.py report            < text     -> the physical devices and the ineligible ones, for humans
"""

from __future__ import annotations

import json
import sys


def phones(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            devices = json.load(handle)["result"]["devices"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    found = []
    for device in devices:
        hardware = device.get("hardwareProperties", {})
        connection = device.get("connectionProperties", {})
        props = device.get("deviceProperties", {})
        if hardware.get("platform") != "iOS" or hardware.get("deviceType") != "iPhone":
            continue
        found.append(
            {
                "id": device.get("identifier", ""),
                "udid": hardware.get("udid", ""),
                "name": props.get("name", "iPhone"),
                "os": props.get("osVersionNumber", ""),
                "pairing": connection.get("pairingState", "unknown"),
                "developer_mode": props.get("developerModeStatus", "unknown"),
                "ddi": props.get("ddiServicesAvailable"),
                "tunnel": connection.get("tunnelState", "unknown"),
            }
        )
    found.sort(key=lambda phone: phone["pairing"] != "paired")  # paired phones first
    return found


def parse_destinations(text: str) -> tuple[list[dict], list[dict]]:
    """Split `xcodebuild -showdestinations` output into (available, ineligible) entries."""
    available: list[dict] = []
    ineligible: list[dict] = []
    section: list[dict] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Available destinations"):
            section = available
            continue
        if stripped.startswith("Ineligible destinations"):
            section = ineligible
            continue
        if section is None or not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        fields: dict[str, str] = {}
        for part in stripped[1:-1].split(", "):
            key, _, value = part.strip().partition(":")
            fields[key.strip()] = value.strip()
        section.append(fields)
    return available, ineligible


def is_physical_iphone(entry: dict) -> bool:
    identifier = entry.get("id", "")
    return entry.get("platform") == "iOS" and bool(identifier) and "placeholder" not in identifier and not identifier.startswith("dvtdevice-")


def pick_destination(available: list[dict], udid: str, name: str) -> dict | None:
    physical = [entry for entry in available if is_physical_iphone(entry)]
    for entry in physical:
        if udid and entry["id"] == udid:
            return entry
    for entry in physical:
        if name and entry.get("name") == name:
            return entry
    return physical[0] if len(physical) == 1 else None


def report(text: str) -> str:
    available, ineligible = parse_destinations(text)
    lines = []
    physical = [entry for entry in available if is_physical_iphone(entry)]
    lines.append("Devices Xcode can build for: " + (", ".join(f"{e.get('name', '?')} ({e['id']})" for e in physical) or "none"))
    for entry in ineligible:
        if entry.get("platform") == "iOS" and "placeholder" not in entry.get("id", ""):
            lines.append(f"Xcode refuses {entry.get('name', '?')}: {entry.get('error') or 'no reason given'}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "phone":
        found = phones(argv[2])
        if found:
            phone = found[0]
            fields = [phone["id"], phone["udid"], phone["name"], phone["os"], phone["pairing"], phone["developer_mode"], str(phone["ddi"]), phone["tunnel"]]
            print("|".join(field.replace("|", "/") for field in fields))
        return 0
    if len(argv) >= 4 and argv[1] == "dest":
        available, _ = parse_destinations(sys.stdin.read())
        entry = pick_destination(available, argv[2], argv[3])
        if entry:
            print(f"{entry['id']}|{entry.get('name', '')}")
        return 0
    if len(argv) >= 2 and argv[1] == "report":
        print(report(sys.stdin.read()))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
