"""Increment the VERSION file.

Driven by .github/workflows/version-bump.yml after a pull request merges, and
runnable by hand:

    python tools/bump_version.py            # patch: 0.4.0 -> 0.4.1
    python tools/bump_version.py minor      #        0.4.1 -> 0.5.0
    python tools/bump_version.py major      #        0.5.0 -> 1.0.0
    python tools/bump_version.py --dry-run  # print the result, write nothing

VERSION is the only file touched. That is the point of it being a separate
file: bgan/__init__.py reads it, bgan/update.py asks the package, and neither
carries a version literal that could drift out of step with it.

Exits non-zero rather than guessing if VERSION is missing or unparseable, so a
workflow fails loudly instead of silently writing a wrong number over a real
one.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT/"VERSION"
LEVELS = ("major", "minor", "patch")
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def bump(version, level="patch"):
    """'0.4.0', 'minor' -> '0.5.0'. ValueError on anything unexpected."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}, not {level!r}")
    m = _SEMVER.match(version.strip())
    if not m:
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {version!r}")
    major, minor, patch = (int(g) for g in m.groups())
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def level_from_labels(labels):
    """Pick a bump level from PR labels. Anything unlabelled is a patch.

    'major' and 'minor' are honoured; 'major' wins if both are somehow set,
    because under-bumping is the worse mistake -- it publishes a breaking
    change as though it were compatible.
    """
    have = {str(x).strip().lower() for x in labels}
    if "major" in have:
        return "major"
    if "minor" in have:
        return "minor"
    return "patch"


def read_version(path=VERSION_FILE):
    return path.read_text(encoding="utf-8").strip()


def write_version(version, path=VERSION_FILE):
    # Newline-terminated and LF, so the file the server publishes matches what
    # a Windows checkout reads back after autocrlf. See bgan.update.
    path.write_text(version + "\n", encoding="utf-8", newline="\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("level", nargs="?", default="patch", choices=LEVELS)
    ap.add_argument("--labels", default="",
                    help="comma-separated PR labels; overrides `level`")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print-only", action="store_true",
                    help="print the next version and exit, writing nothing")
    a = ap.parse_args(argv)

    if not VERSION_FILE.is_file():
        print(f"error: {VERSION_FILE} does not exist", file=sys.stderr)
        return 2
    try:
        old = read_version()
        level = level_from_labels(a.labels.split(",")) if a.labels else a.level
        new = bump(old, level)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if a.print_only:
        print(new)
        return 0
    print(f"{old} -> {new}  ({level})")
    if not a.dry_run:
        write_version(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
