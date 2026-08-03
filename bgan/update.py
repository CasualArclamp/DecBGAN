"""Check whether a newer version has been published, and offer to fetch it.

Deliberately small, and deliberately not automatic.

The check fetches one short text file -- the repository's VERSION -- and
compares it with the local one. That is all it does. **No code is downloaded
and nothing is executed as a result of the network call**, so the worst a
wrong or hostile answer can produce is a spurious prompt.

Applying an update is a separate call, made only after the user has been
asked, and it runs `git pull --ff-only` in the existing checkout. The
--ff-only is the point: it cannot create a merge commit and cannot discard
local commits, so a checkout carrying your own work refuses to update rather
than quietly losing it. A dirty working tree refuses too.

`check()` never raises. Offline, DNS failure, a proxy, a rate limit and a
missing VERSION file all return None, because a version check failing is not
worth interrupting anyone over -- and until VERSION exists on the default
branch, the check is silently unavailable rather than noisy.

Set BGAN_NO_UPDATE_CHECK=1 to switch it off. This module is the only thing
in the project that touches the network.
"""
from __future__ import annotations
import os
import re
import subprocess
import urllib.request
from pathlib import Path

REPO = "CasualArclamp/DecBGAN"
BRANCH = "main"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 4.0

# Only used when the VERSION file is missing -- a vendored copy of the package
# without the repository around it. VERSION is the source of truth; if you
# bump one, bump the other.
FALLBACK_VERSION = "0.4.0"

_DIGITS = re.compile(r"\d+")


def local_version():
    # strip() is load-bearing on Windows: with core.autocrlf the checked-out
    # VERSION is "0.4.0\r\n" while the server serves the stored "0.4.0\n",
    # and comparing those raw would report an update on every start.
    try:
        v = (ROOT/"VERSION").read_text(encoding="utf-8").strip()
        return v or FALLBACK_VERSION
    except OSError:
        return FALLBACK_VERSION


def remote_version(timeout=TIMEOUT, url=VERSION_URL):
    """The published version string. Raises on any network problem."""
    req = urllib.request.Request(
        url, headers={"User-Agent": f"bgan-decoder/{local_version()}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # Bounded read: this file is a version string, not a payload.
        return r.read(64).decode("utf-8", "replace").strip()


def _key(v):
    """Dotted numeric version as a tuple, or None if it is not one."""
    parts = v.strip().lstrip("vV").split(".")
    if not parts or not all(_DIGITS.fullmatch(p) for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_newer(remote, local):
    a, b = _key(remote), _key(local)
    if a is None or b is None:
        return False            # unrecognised: say nothing rather than nag
    n = max(len(a), len(b))
    return a + (0,)*(n - len(a)) > b + (0,)*(n - len(b))


def check(timeout=TIMEOUT):
    """dict(local, remote, newer), or None if no answer could be had."""
    if os.environ.get("BGAN_NO_UPDATE_CHECK"):
        return None
    loc = local_version()
    try:
        rem = remote_version(timeout)
    except Exception:
        return None
    if not rem or len(rem) > 32 or "\n" in rem:
        return None             # not a version string; treat as no answer
    return dict(local=loc, remote=rem, newer=is_newer(rem, loc))


def _git(*args):
    return subprocess.run(("git",) + args, cwd=ROOT, capture_output=True,
                          text=True, timeout=120)


def can_update():
    """(True, "") if a fast-forward pull is safe here, else (False, why)."""
    if not (ROOT/".git").exists():
        return False, ("this is not a git checkout, so it cannot update "
                       "itself -- re-download or git clone the repository")
    try:
        r = _git("status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return False, "git is not available on PATH"
    if r.returncode != 0:
        return False, (r.stderr.strip() or "git status failed")
    if r.stdout.strip():
        return False, ("you have uncommitted changes -- commit or stash them "
                       "first, so the update cannot overwrite your work")
    return True, ""


def apply_update():
    """Fast-forward the checkout to the published version. (ok, message)."""
    ok, why = can_update()
    if not ok:
        return False, why
    try:
        r = _git("pull", "--ff-only")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git pull failed: {exc}"
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        # --ff-only refusing is the safety net doing its job, so say so.
        if "non-fast-forward" in out or "diverge" in out:
            out += ("\n\nYour checkout has commits that are not published. "
                    "Nothing was changed.")
        return False, out or "git pull failed"
    return True, out or "already up to date"


def main(argv=None):
    # check() folds every failure into None, which is right for the GUI and
    # unhelpful for someone who typed the command. Separate the cases here.
    if os.environ.get("BGAN_NO_UPDATE_CHECK"):
        print(f"update check is disabled (BGAN_NO_UPDATE_CHECK). "
              f"Local version {local_version()}.")
        return 0
    try:
        rem = remote_version()
    except Exception as exc:
        print(f"could not reach {VERSION_URL}\n  {type(exc).__name__}: {exc}")
        print(f"local version {local_version()}")
        return 1
    st = dict(local=local_version(), remote=rem,
              newer=is_newer(rem, local_version()))
    if not _key(rem):
        print(f"published version {rem!r} is not a version number; "
              f"local version {st['local']}")
        return 1
    if not st["newer"]:
        print(f"up to date: {st['local']}")
        return 0
    print(f"version {st['remote']} is available; you have {st['local']}")
    ok, why = can_update()
    if not ok:
        print(f"cannot update automatically: {why}")
        return 1
    if input("fetch it now with git pull? [y/N] ").strip().lower() != "y":
        return 0
    done, out = apply_update()
    print(out)
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
