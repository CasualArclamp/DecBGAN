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

# venv puts the interpreter in bin/ on Unix and Scripts/ on Windows.
venv_python() {
    if [ -x ".venv/bin/python" ]; then echo ".venv/bin/python"
    elif [ -x ".venv/Scripts/python.exe" ]; then echo ".venv/Scripts/python.exe"
    fi
}

# A .venv beside this script wins over whatever is on PATH. On a PEP 668
# system that is the only place the dependencies could have been installed --
# see below -- so preferring it is what makes a second launch work.
VENVPY="$(venv_python)"
[ -n "$VENVPY" ] && PY="$VENVPY"

[ -n "$PY" ] || die "No working Python 3.9+ found on PATH (tried python3, python, py)."

# Check what gui.py actually imports, not a weaker proxy for it. `import
# matplotlib` loads neither the Tk backend nor PIL, so it passed on a Fedora 44
# box whose matplotlib then died on `from PIL import ImageTk` -- issue #25. The
# check has to fail where the GUI would fail.
DEPS='import numpy, scipy, numba
import matplotlib.backends.backend_tkagg'

if ! "$PY" -c "$DEPS" >/dev/null 2>&1; then
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
        PY="$(venv_python)"
        [ -n "$PY" ] || die "Created .venv but found no interpreter inside it."
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

# matplotlib's Tk backend does `from PIL import Image, ImageTk`, and several
# distributions ship ImageTk in a separate package from the rest of Pillow.
# Reported on Fedora 44 (issue #25): a pip matplotlib in /usr/local paired with
# a dnf Pillow in /usr/lib64 that had no ImageTk, so the GUI died on import
# after every earlier check had passed. Inside a venv pip supplies its own
# Pillow and this cannot arise, so reaching here means a system interpreter.
if ! "$PY" -c 'from PIL import ImageTk' >/dev/null 2>&1; then
    cat >&2 <<'MSG'

  Pillow is installed but PIL.ImageTk is missing, which matplotlib's Tk
  backend needs. Several distributions package it separately:

      Fedora          sudo dnf install python3-pillow-tk
      Debian/Ubuntu   sudo apt install python3-pil.imagetk
      Arch            included in python-pillow

  Or sidestep the mixed system/pip install altogether by letting this script
  build a virtual environment, where pip supplies its own Pillow:

      rm -rf .venv && python3 -m venv .venv && ./start.sh

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
