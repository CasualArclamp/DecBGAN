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

# A venv built by install-bgan-decoder.sh wins over whatever is on PATH -- it
# is where that installer put the dependencies, and on a PEP 668 system it is
# the only place they could have gone. Scripts/ is checked as well as bin/ so
# a venv made under Git Bash or MSYS is still found by this launcher.
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
    PY=".venv/Scripts/python.exe"
fi

[ -n "$PY" ] || die "No working Python 3.9+ found on PATH (tried python3, python, py)."

if ! "$PY" -c 'import numpy, scipy, numba, matplotlib' >/dev/null 2>&1; then
    echo "  Installing dependencies (first run only)..."
    if ! "$PY" -m pip install --quiet -r requirements.txt 2>/dev/null; then
        # PEP 668: Debian 12+, Ubuntu 23.04+, Fedora 38+ and Homebrew Python
        # refuse to install into the system interpreter at all. Build a venv
        # rather than telling the user to override the refusal with
        # --break-system-packages, which is exactly what it sounds like.
        echo "  System Python will not accept packages; building a venv..."
        "$PY" -m venv .venv \
            || die "Could not create .venv. You may need your distribution's
  python3-venv package (Debian/Ubuntu: sudo apt install python3-venv)."
        PY=".venv/bin/python"
        "$PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
        "$PY" -m pip install --quiet -r requirements.txt \
            || die "Dependency install failed. Try: $PY -m pip install -r requirements.txt"
    fi
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
