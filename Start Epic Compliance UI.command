#!/usr/bin/env bash
#
# Start Epic Compliance UI.command
#
# Double-click this file in Finder to launch the Epic Compliance web UI.
# It sets everything up (first run may take a minute), starts the server,
# and opens the dashboard in your browser. Close this window to stop the tool.

cd "$(dirname "$0")" || exit 1

PORT=8000
URL="http://localhost:$PORT"

echo "════════════════════════════════════════════"
echo "   Epic Compliance Tool — starting web UI"
echo "════════════════════════════════════════════"
echo

# If it's already running, just open the browser and stop here.
if curl -sf "$URL/healthz" >/dev/null 2>&1; then
  echo "✓ Already running — opening $URL"
  open "$URL"
  echo
  echo "(The tool is running in another window. You can close this one.)"
  read -r -p "Press Return to close..." _
  exit 0
fi

# First-time setup: create the virtualenv and install if needed.
if [ ! -d ".venv" ]; then
  echo "First-time setup: creating virtual environment..."
  python3 -m venv .venv || { echo "Failed to create venv. Is Python 3.11+ installed?"; read -r _; exit 1; }
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import epic_compliance" >/dev/null 2>&1; then
  echo "Installing the tool (first run only, please wait)..."
  pip install -e ".[dev]" >/tmp/ect_install.log 2>&1 || {
    echo "Install failed — see /tmp/ect_install.log"; read -r _; exit 1;
  }
fi

# Open the browser once the server is healthy (runs in the background).
(
  for _ in $(seq 1 40); do
    if curl -sf "$URL/healthz" >/dev/null 2>&1; then open "$URL"; break; fi
    sleep 0.5
  done
) &

echo
echo "Starting server → $URL"
echo "Your browser will open automatically in a moment."
echo
echo "▸ Leave this window open while you use the tool."
echo "▸ To stop the tool, close this window (or press Ctrl-C)."
echo "────────────────────────────────────────────"
echo

# Run in the foreground so closing the window stops the server.
exec python -m epic_compliance serve
