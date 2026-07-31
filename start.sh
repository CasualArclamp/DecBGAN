#!/usr/bin/env bash
# BGAN forward-link decoder - Linux/macOS launcher.
#
# Checks Python, installs dependencies if needed, warns if the ETSI Annex C
# tables are missing, then starts the GUI. Arguments are passed through, so
# "./start.sh capture.wav" opens with that file selected.
set -euo pipefail
cd "$(dirname "$0")"

die() { printf '\n  %s\n\n' "$*" >&2; exit 1; }

# Probe each candidate by actually running it, not just by existence. On
# Windows, Git Bash sees a "python3" that is really a Microsoft Store
# app-execution alias stub: command -v finds it, but it is not an interpreter.
# Taking the first name that exists picks the stub and then fails.
PY=""
for c in python3 python py; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,9) else 1)' \
            >/dev/null 2>&1; then
        PY="$c"
        break
    fi
done
[ -n "$PY" ] || die "No working Python 3.9+ found on PATH (tried python3, python, py)."

if ! "$PY" -c 'import numpy, scipy, numba, matplotlib' >/dev/null 2>&1; then
    echo "  Installing dependencies (first run only)..."
    "$PY" -m pip install --quiet -r requirements.txt \
        || die "Dependency install failed. Try: $PY -m pip install -r requirements.txt"
fi

# tkinter is not pip-installable; it comes from the system package.
if ! "$PY" -c 'import tkinter' >/dev/null 2>&1; then
    cat >&2 <<'MSG'

  tkinter is missing, so the GUI cannot start. It is a system package, not a
  pip one:

      Debian/Ubuntu   sudo apt install python3-tk
      Fedora          sudo dnf install python3-tkinter
      Arch            sudo pacman -S tk
      macOS (brew)    brew install python-tk

  The command-line tools work without it:

      python3 tools/decode_wav.py capture.wav --survey

MSG
    exit 1
fi

C1="ts_1027440201_AnnexC1_v010101p0"
if [ ! -d "$C1" ] && [ ! -d "work/$C1" ] && [ ! -d "annex/$C1" ]; then
    cat >&2 <<'MSG'

  WARNING: the ETSI Annex C tables were not found.

  They are ETSI copyright so they are not shipped with this repository, but
  they are a free download. Decoding will fail without them.

    1. https://www.etsi.org/standards - search for TS 102 744-2-1
    2. download ts_1027440201_AnnexC1_v010101p0.zip and ...AnnexC2...zip
    3. extract both here, next to this script

  See README.md for detail. Starting anyway.

MSG
fi

echo "  Starting BGAN decoder GUI..."
exec "$PY" tools/gui.py "$@"
