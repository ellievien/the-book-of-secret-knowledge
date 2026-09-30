#!/usr/bin/env bash
# Reflect helper for macOS.
#
#   ./run.sh                          start Reflect (menu bar icon)
#   ./run.sh --pair                   show a new pairing code for another phone
#   ./run.sh --capture-test out.png   save one frame of the Claude window
#   ./run.sh --no-tray                run without the menu bar icon (automatic over SSH)
#
# First run installs Python 3.11 if needed (Homebrew, or uv without admin
# rights), creates .venv and installs the dependencies.
set -euo pipefail
cd "$(dirname "$0")"

is_py311() { "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)' >/dev/null 2>&1; }

find_python() {
  local candidate
  for candidate in ${REFLECT_PYTHON:-} python3.11 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11 \
      /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 "$HOME/.local/bin/python3.11"; do
    if command -v "$candidate" >/dev/null 2>&1 && is_py311 "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  if [ -x "$HOME/.local/bin/uv" ]; then
    candidate=$("$HOME/.local/bin/uv" python find 3.11 2>/dev/null || true)
    if [ -n "$candidate" ] && is_py311 "$candidate"; then
      echo "$candidate"
      return 0
    fi
  fi
  return 1
}

PY=$(find_python || true)
if [ -z "$PY" ]; then
  echo "Python 3.11 was not found. Installing it..."
  BREW=""
  for b in brew /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if command -v "$b" >/dev/null 2>&1; then BREW=$(command -v "$b"); break; fi
  done
  if [ -n "$BREW" ]; then
    "$BREW" install python@3.11
  else
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
    "$HOME/.local/bin/uv" python install 3.11
  fi
  PY=$(find_python || true)
  if [ -z "$PY" ]; then
    echo "Could not install Python 3.11 automatically. Install it from https://www.python.org and run this again." >&2
    exit 1
  fi
fi

VENV=.venv
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating the virtual environment..."
  "$PY" -m venv "$VENV"
fi

STAMP="$VENV/.requirements.sha256"
HASH=$(shasum -a 256 requirements.txt | cut -d' ' -f1)
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$HASH" ]; then
  echo "Installing dependencies (first run only)..."
  "$VENV/bin/python" -m pip install --upgrade pip --disable-pip-version-check --quiet
  "$VENV/bin/python" -m pip install -r requirements.txt --disable-pip-version-check
  echo "$HASH" > "$STAMP"
fi

# Over SSH there is no menu bar to put an icon in.
EXTRA=()
if [ -n "${SSH_CONNECTION:-}" ] && [[ " $* " != *" --no-tray "* ]]; then
  EXTRA+=(--no-tray)
fi

exec "$VENV/bin/python" -m reflect_helper ${EXTRA[@]+"${EXTRA[@]}"} "$@"
