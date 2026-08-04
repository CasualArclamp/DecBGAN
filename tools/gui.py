"""BGAN forward-link decoder - GUI.

Point it at a capture, press Decode, watch the constellation and spectrum,
read the recovered strings, export a pcap.

Everything shown is measured from the capture. Where a number is an estimate
rather than a measurement it is labelled as such, and the two pcap export
modes deliberately make different claims -- see bgan/pcapout.py.

    python tools/gui.py [capture.wav]
"""
from __future__ import annotations
import os
import queue
import re
import sys
import textwrap
import threading
import wave
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tkinter as tk                                          # noqa: E402
from tkinter import ttk, filedialog, messagebox               # noqa: E402
import tkinter.font as tkfont                                 # noqa: E402

import matplotlib                                             # noqa: E402
matplotlib.use("TkAgg")
from matplotlib.figure import Figure                          # noqa: E402
from matplotlib.backends.backend_tkagg import (                # noqa: E402
    FigureCanvasTkAgg)
from scipy.signal import welch                                # noqa: E402

from bgan import (spec, mod, recv, bulletin, pcapout,          # noqa: E402
                  findings, sip, rtp, terminals, update)
from tools.decode_wav import (decode_capture, survey, NoCarrier,  # noqa: E402
                              channelise, safe_stem, MOS)

NL = chr(10)
BG = "#1c1f26"
FG = "#d8dee9"
ACC = "#5fb3d4"

# Info-panel palette. Labels recede, values come forward, and the three
# status colours are reserved for figures that have a documented threshold --
# so colour on this panel always means something.
DIM = "#79839a"
DIM2 = "#59627a"
VAL = "#e8eef7"
WARN = "#e0a33e"
GOOD = "#8fbf6f"
INFO_TAGS = {
    "sec":  dict(foreground=ACC, spacing1=9, spacing3=4),
    "lab":  dict(foreground=DIM),
    "val":  dict(foreground=VAL),
    "note": dict(foreground=DIM2),
    "ok":   dict(foreground="#8fd18a"),
    "warn": dict(foreground="#e8c07d"),
    "bad":  dict(foreground="#e88388"),
}
LAB_W = 17          # label column width, characters


class Fit(str):
    """A value shortened to fit rather than wrapped over several lines.

    Filenames are the case that matters: wrapping one costs three lines and
    is harder to read than an elided middle, and both ends carry meaning --
    the leading text names the capture, the tail carries frequency and time.
    """


def _elide(s, n):
    if len(s) <= n:
        return s
    if n < 8:
        return s[:n]
    k = (n - 3)//2
    return s[:n - 3 - k] + "..." + s[-k:]


def _grade(v, good, ok, invert=False):
    """Traffic light for a figure with a documented threshold.

    `good`/`ok` are the boundaries; `invert` for metrics where lower is
    better. Returns None for a value that is not finite, which leaves the
    figure uncoloured rather than guessing at it.
    """
    if v is None or not np.isfinite(v):
        return None
    if invert:
        return "ok" if v <= good else ("warn" if v <= ok else "bad")
    return "ok" if v >= good else ("warn" if v >= ok else "bad")


# --------------------------------------------------------------------------
# worker


class Result:
    def __init__(self):
        self.info = {}
        self.offs = self.mets = None
        self.lvls = []
        self.recs = []          # (frame, block, level, agreement, bits)
        self.payload = b""
        self.const = np.zeros(0, complex)
        self.bulletins = []     # (frame, BulletinBoard)
        self.bb_period = None
        self.bb_offset = None
        self.bb_level = None    # coding level block 0 used, e.g. "L3" or "R"


class Worker(threading.Thread):
    def __init__(self, path, secs, search_levels, q, stop):
        super().__init__(daemon=True)
        self.path, self.secs = path, secs
        self.search = search_levels
        self.q, self.stop = q, stop

    def emit(self, kind, **kw):
        self.q.put((kind, kw))

    def run(self):
        try:
            self._run()
        except Exception:
            self.emit("error", text=traceback.format_exc())

    def _run(self):
        r = Result()
        self.emit("status", text="loading capture...", pct=2)
        x, sr = recv.load_wav_iq(self.path, secs=self.secs)
        x = x - x.mean()

        self.emit("status", text="computing spectrum...", pct=6)
        fr, P = welch(x, sr, nperseg=8192, return_onesided=False,
                      detrend=False)
        i = np.argsort(fr)
        self.emit("psd", f=fr[i], p=10*np.log10(P[i] + 1e-30), sr=sr)

        self.emit("status", text="channelising and recovering timing...",
                  pct=10)

        def prog(frac, text, const=None):
            if self.stop.is_set():
                raise KeyboardInterrupt
            self.emit("progress", pct=6 + 91*frac, text=text, const=const)

        try:
            recs, info, (tau_idx, offs, lvls, mets) = decode_capture(
                self.path, secs=self.secs, search_levels=self.search,
                progress=prog)
        except KeyboardInterrupt:
            self.emit("status", text="stopped", pct=0)
            return
        except NoCarrier as exc:
            self.emit("nocarrier", rows=exc.rows, path=self.path)
            return
        r.info = dict(info)
        r.info["path"] = self.path
        r.info["file_hz"] = _freq_from_name(self.path)
        r.info["band"] = _band(fr[i], P[i], info.get("centre", 0.0))
        r.offs, r.lvls, r.mets = offs, lvls, mets
        r.recs = [(f, b, lv, ag, bits) for f, b, lv, ag, bits in recs]
        r.const = info.get("const", np.zeros(0, complex))
        self.emit("info", info=dict(r.info))
        rel = offs - np.arange(len(offs))*MOS
        self.emit("track",
                  nplateau=int(np.count_nonzero(np.diff(rel)) + 1),
                  span=int(rel.max() - rel.min()), nframes=len(offs))
        if r.recs:
            r.payload = np.packbits(
                np.concatenate([x[4] for x in r.recs])).tobytes()
        self.emit("status", text="parsing bearer control...", pct=97)
        _bulletins(r)
        self.emit("done", result=r)


def _bb_threshold(n, mod=4096, alpha=0.01):
    """How many frames must agree on a frame-number offset to mean anything.

    bulletin.confirm returns the commonest offset whatever its count, so a
    single chance hit reads as a BulletinBoard -- on 40 random payloads it
    duly invented one. The offsets of unrelated payloads are uniform over
    `mod`, so the count in each of the `mod` bins is Poisson(n/mod); take the
    count whose chance of arising in ANY bin is below alpha.

    It has to scale with n rather than be a constant: 3 agreeing frames out
    of 40 is decisive, out of 2766 it is expected.
    """
    from scipy.stats import poisson
    return max(3, int(poisson.isf(alpha/mod, n/mod)) + 1)


def _bulletins(r):
    """BulletinBoards carried in block 0, whatever coding level it uses.

    Block 0's level is whatever its unique word signalled, and that is not
    always L3. The 1538.099 capture is entirely level R, so filtering on L3
    meant the BulletinBoard was never looked for at all: the panel reported
    no spot beam for a payload whose block 0 opens with 0xc9 -- the
    BulletinBoard Bearer Control header -- in 241 of its 248 frames.

    Levels are tried commonest first rather than pooled, because
    bulletin.confirm needs one payload length and each coding level carries a
    different number of information bits (Annex B2: L3 2000, R 3000).
    """
    b0 = [(f, lv, bits) for (f, b, lv, ag, bits) in r.recs if b == 0]
    if len(b0) < 3:
        return
    best = None
    for lv in sorted({l for _f, l, _b in b0}):
        sel = [(f, bits) for f, l, bits in b0 if l == lv]
        if len(sel) < 3:
            continue
        frames = [f for f, _ in sel]
        pays = [bits for _, bits in sel]
        try:
            off, mask, period = bulletin.confirm(pays, frames)
        except Exception:
            continue
        n = int(np.count_nonzero(mask))
        if n >= _bb_threshold(len(sel)) and (best is None or n > best[0]):
            best = (n, lv, off, mask, period, frames, pays)
    if best is None:
        return
    _n, lv, off, mask, period, frames, pays = best
    r.bb_offset, r.bb_period, r.bb_level = off, period, lv
    r.bulletins = [(frames[i], bulletin.parse(pays[i]))
                   for i in np.flatnonzero(mask)]


def _freq_from_name(path):
    m = re.search(r"(\d{9,10})\s*Hz", Path(path).name)
    return int(m.group(1)) if m else None


# Wall-clock cost of a decode, in seconds of compute per second of capture,
# with the 8-phase timing search on. Measured on this machine over a 30 s
# capture: 1.76 s/s single-threaded, 0.97 s/s across 16 cores -- so a capture
# now decodes in roughly the time it took to record.
#
# Without the 10-level trial decode it is a measured 0.11 ratio, not the 10x
# the level count would suggest, because the timing survey and framing are
# paid either way.
#
# A guide for choosing a length, not a promise. It scales with core count, so
# a machine with fewer cores will be slower -- decode_wav.resolve_jobs uses
# one worker per core unless --jobs or $BGAN_JOBS says otherwise.
EST_SEC_PER_SEC = 1.00
EST_NOSEARCH_RATIO = 0.69


def probe_wav(path):
    """WAV header summary: duration, rate, channels, size. No samples read."""
    try:
        if not path or not os.path.isfile(path):
            return None
        with wave.open(path) as w:
            sr = w.getframerate()
            n = w.getnframes()
            if not sr or not n:
                return None
            return dict(sr=sr, ch=w.getnchannels(), width=w.getsampwidth(),
                        frames=n, secs=n/sr,
                        mb=os.path.getsize(path)/1e6)
    except Exception:
        return None



def _hms(s):
    s = int(round(s))
    if s < 90:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m{s % 60:02d}s"
    return f"{s//3600}h{(s % 3600)//60:02d}m"


def _band(f, p, centre, bearer=None):
    """Where the bearer sits, plus a check that `centre` is right.

    The bandwidth is NOT measured. Roll-off is a spec constant (alpha 0.25,
    2-1 clause 5.2.3), so the occupied band follows from the symbol rate and
    the only unknown is the centre -- which the decoder has already found.

    Measuring it instead made the figure track the NOISE, not the signal. A
    99%-of-total-power rule applied to a whole capture integrates every noise
    bin and every other carrier in the recording, and `np.maximum(p - floor,
    0)` half-wave rectifies a chi-squared periodogram so the residue
    accumulates with the bin count rather than cancelling. Measured:

        same synthetic bearer   166.7 kHz @ 30 dB   398.2 kHz @ 4 dB
        BGAN1, 189 kHz signal   1924 kHz reported in a 2048 kHz capture
        192 kHz captures        pinned at ~162 kHz -- the capture width

    i.e. it returned about 94% of whatever span was recorded whenever SNR
    was modest, and was only accidentally near-right on the cleanest files.

    What IS worth measuring is whether the centre is right, so return the
    power imbalance between the halves of the allocation. Zero dB means
    centred; a lopsided spectrum is exactly what biased the old centroid
    estimator (see bgan/carrier.py). Measured: synthetics -0.01..+0.05 dB,
    512 kHz captures +0.15..+0.33, and +8.5 dB on the one file holding no
    F80T4.5X-8B carrier at all.

    The 192 kHz captures all sit near +1 dB, and that is not distortion --
    their carriers land 8-9 kHz low, so the allocation runs past the lower
    capture edge and the missing power shows up as upper-half bias. `clipped`
    reports that condition directly rather than leaving it to be read out of
    the imbalance.
    """
    b = bearer or spec.F80T45X8B
    alloc = b.alloc_bw
    if f is None:                       # no PSD to hand -- spec figures only
        return dict(alloc=alloc, p99=b.power_bw(0.99), clipped=0.0,
                    balance=float("nan"))
    d = f - centre
    ring = (np.abs(d) > alloc*0.55) & (np.abs(d) <= alloc*0.8)
    q = p - (np.median(p[ring]) if ring.sum() >= 32 else 0.0)
    lo = float(q[(d >= -alloc/2) & (d < 0)].sum())
    hi = float(q[(d >= 0) & (d <= alloc/2)].sum())
    span = float(f[-1] - f[0])
    return dict(alloc=alloc, p99=b.power_bw(0.99),
                clipped=max(0.0, alloc/2 + abs(centre) - span/2),
                balance=10*np.log10(hi/lo) if lo > 0 and hi > 0
                else float("nan"))


# --------------------------------------------------------------------------
# app


class App(tk.Tk):
    def __init__(self, initial=None):
        super().__init__()
        self.title(f"BGAN forward-link decoder {update.local_version()}")
        self.geometry("1280x900")
        self.configure(bg=BG)
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.result = None
        self._upd_busy = False
        self._style()
        self._build()
        if initial:
            self.pathvar.set(initial)
        self.after(80, self._poll)
        self._check_update()

    # -- update control --------------------------------------------------
    #
    # The version and one button, top-left. The button carries the state:
    #   "Check updates"      idle
    #   "Checking..."        a lookup is in flight, disabled
    #   "Update to 0.5.0"    something newer exists -- green, and clicking it
    #                        pulls and then closes the app
    #
    # The startup check drives the button and never opens a dialog. A modal on
    # every launch would be the wrong trade for a background nicety, and there
    # is now a visible control saying the same thing.

    def _check_update(self, announce=False):
        """Ask GitHub for the published version, off the UI thread.

        `announce` is set when the user pressed the button, and is the only
        case that reports "you are up to date" or a failure -- an automatic
        check that finds nothing should say nothing. bgan.update.check()
        swallows its own errors, so a failed lookup costs nothing.
        """
        if self._upd_busy:
            return
        self._upd_busy = True
        self.updbtn.configure(text="Checking...", state="disabled")

        def run():
            st = update.check()
            self.q.put(("updchecked", {"st": st, "announce": announce}))
        threading.Thread(target=run, daemon=True).start()

    def _update_checked(self, st, announce):
        self._upd_busy = False
        if st and st["newer"]:
            self.updbtn.configure(text=f"Update to {st['remote']}",
                                  state="normal", style="Update.TButton",
                                  command=lambda: self._do_update(st))
            self._log(f"update available: {st['local']} -> {st['remote']}")
            return
        self.updbtn.configure(text="Check updates", state="normal",
                              style="TButton",
                              command=lambda: self._check_update(True))
        if not announce:
            return
        if st is None:
            # Distinguish the cases: check() folds them all into None, which
            # is right for a background check and unhelpful to someone who
            # just pressed the button.
            if os.environ.get("BGAN_NO_UPDATE_CHECK"):
                messagebox.showinfo(
                    "Update check disabled",
                    "BGAN_NO_UPDATE_CHECK is set, so nothing was requested.\n\n"
                    f"You are running {update.local_version()}.")
            else:
                messagebox.showinfo(
                    "Update check",
                    "Could not reach GitHub -- offline, or blocked by a "
                    f"proxy.\n\nYou are running {update.local_version()}.")
        else:
            messagebox.showinfo(
                "Up to date", f"You are running {st['local']}, "
                              "which is the published version.")

    def _do_update(self, st):
        """Pull, then close. Confirmed first, and the pull happens off-thread.

        The pull runs BEFORE closing, not after: if it fails there is still a
        window to say so, and the app stays open. Closing first and updating
        from a detached helper would hide exactly the failure worth seeing.
        """
        ok, why = update.can_update()
        if not ok:
            messagebox.showwarning(
                "Cannot update automatically",
                f"Version {st['remote']} is available, but this copy cannot "
                f"update itself:\n\n{why}")
            return
        if not messagebox.askyesno(
                "Update and close",
                f"Update from {st['local']} to {st['remote']}?\n\n"
                "This runs `git pull --ff-only` in this checkout, which "
                "cannot discard local commits, and then closes the app.\n\n"
                "Any decode in progress will be stopped."):
            return
        self._upd_busy = True
        self.updbtn.configure(text="Updating...", state="disabled")
        self.statvar.set("updating...")
        self.stop.set()                 # stop any decode before files move

        def run():
            # Pass the expected version so a pull that exits 0 without moving
            # anything is reported as the failure it is, rather than closing
            # the app and re-offering the same update at the next start.
            done, out = update.apply_update(expect=st["remote"])
            self.q.put(("updapplied", {"done": done, "out": out, "st": st}))
        threading.Thread(target=run, daemon=True).start()

    def _update_applied(self, done, out, st):
        self._upd_busy = False
        self._log("update: " + out)
        if not done:
            self.statvar.set("update failed - see Log tab")
            self.updbtn.configure(text=f"Update to {st['remote']}",
                                  state="normal", style="Update.TButton",
                                  command=lambda: self._do_update(st))
            messagebox.showerror("Update failed", out)
            return
        messagebox.showinfo(
            "Updated",
            f"Now at {st['remote']}.\n\n{out}\n\n"
            "The app will close. Start it again to run the new version.")
        self.destroy()

    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=BG, foreground=FG, fieldbackground="#262b33")
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background="#262b33", foreground=FG,
                    padding=(14, 6))
        s.map("TNotebook.Tab", background=[("selected", "#39404d")])
        s.configure("TButton", background="#39404d", foreground=FG,
                    padding=(10, 4))
        s.map("TButton", background=[("active", "#4a5566")])
        s.configure("Horizontal.TProgressbar", background=ACC,
                    troughcolor="#262b33", borderwidth=0)
        # The update button only wears this once there is something to install,
        # so green here means "an action is waiting", not merely "all well".
        s.configure("Update.TButton", background=GOOD, foreground="#11141a")
        s.map("Update.TButton", background=[("active", "#a5d183")])

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")

        # Version and the update control, top-left. Compact, because this row
        # is already busy and the capture path deserves the width.
        ttk.Label(top, text=f"v{update.local_version()}",
                  foreground=ACC).pack(side="left")
        self.updbtn = ttk.Button(top, text="Check updates", width=14,
                                 command=lambda: self._check_update(True))
        self.updbtn.pack(side="left", padx=(6, 8))
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y",
                                                   padx=(0, 8))

        self.pathvar = tk.StringVar()
        ttk.Label(top, text="Capture").pack(side="left")
        ttk.Entry(top, textvariable=self.pathvar, width=55).pack(
            side="left", padx=4)
        ttk.Button(top, text="Browse", command=self._browse).pack(side="left", padx=2)
        ttk.Label(top, text="secs").pack(side="left", padx=(6, 0))
        self.secsvar = tk.StringVar(value="20")
        ttk.Entry(top, textvariable=self.secsvar, width=5).pack(side="left")
        ttk.Button(top, text="Max", command=self._use_max,
                   width=4).pack(side="left", padx=2)
        self.searchvar = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="search levels (slower, for blocks 1-7)",
                        variable=self.searchvar,
                        command=lambda: self._probe_path()).pack(
                            side="left", padx=6)
        ttk.Button(top, text="Scan", command=self._scan).pack(side="left", padx=2)
        self.btn = ttk.Button(top, text="Decode", command=self._start)
        self.btn.pack(side="left", padx=2)
        ttk.Button(top, text="Stop", command=self.stop.set).pack(side="left", padx=2)

        # capture summary, refreshed whenever the path changes
        cap = ttk.Frame(self, padding=(8, 0, 8, 4))
        cap.pack(fill="x")
        self.capvar = tk.StringVar(value="no capture selected")
        ttk.Label(cap, textvariable=self.capvar, foreground=ACC).pack(
            side="left")
        self.probe = None
        self.pathvar.trace_add("write", lambda *_: self._probe_path())

        pr = ttk.Frame(self, padding=(8, 0))
        pr.pack(fill="x")
        self.pbar = ttk.Progressbar(pr, style="Horizontal.TProgressbar",
                                    maximum=100)
        self.pbar.pack(side="left", fill="x", expand=True)
        self.statvar = tk.StringVar(value="idle")
        ttk.Label(pr, textvariable=self.statvar, width=44,
                  anchor="w").pack(side="left", padx=8)

        # A paned window rather than a fixed 42-column box: the info panel
        # holds the widest lines in the app (band edges, level histograms),
        # and how much room they deserve depends on the capture.
        mid = ttk.PanedWindow(self, orient="horizontal")
        mid.pack(fill="both", expand=True, padx=8, pady=8)

        plots = ttk.Frame(mid)
        self.fig = Figure(figsize=(9, 3.4), facecolor=BG, tight_layout=True)
        self.ax_psd = self.fig.add_subplot(121)
        self.ax_con = self.fig.add_subplot(122)
        for a in (self.ax_psd, self.ax_con):
            a.set_facecolor("#11141a")
            a.tick_params(colors=FG, labelsize=8)
            for sp in a.spines.values():
                sp.set_color("#39404d")
        self.canvas = FigureCanvasTkAgg(self.fig, master=plots)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        side = ttk.Frame(mid)
        self.infobox = tk.Text(side, width=46, bg="#11141a", fg=FG,
                               insertbackground=FG, relief="flat", wrap="none",
                               padx=10, pady=6, cursor="arrow",
                               font=("Consolas", 9))
        isy = ttk.Scrollbar(side, command=self.infobox.yview)
        self.infobox.configure(yscrollcommand=isy.set)
        isy.pack(side="right", fill="y")
        self.infobox.pack(side="left", fill="both", expand=True)

        self._info_font = tkfont.Font(font=self.infobox["font"])
        for tag, opt in INFO_TAGS.items():
            self.infobox.tag_configure(tag, **opt)
        bold = tkfont.Font(font=self.infobox["font"])
        bold.configure(weight="bold")
        self.infobox.tag_configure("sec", font=bold)
        self._info_sections, self._info_cols = [], 0
        self.infobox.bind("<Configure>", self._info_resized)
        self.infobox.configure(state="disabled")

        mid.add(plots, weight=3)
        mid.add(side, weight=1)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tab_int = self._textpage(nb, "Interesting")
        self.tab_term = self._textpage(nb, "Terminals")
        self.tab_files = self._textpage(nb, "Files")
        self.tab_sip = self._textpage(nb, "SIP")
        self.tab_str = self._textpage(nb, "Strings")
        self.tab_bb = self._textpage(nb, "Bearer control")
        self.tab_pkt = self._textpage(nb, "Packets")
        self.tab_log = self._textpage(nb, "Log")

        bot = ttk.Frame(self, padding=(8, 0, 8, 8))
        bot.pack(fill="x")
        ttk.Button(bot, text="Export pcap (decoded blocks)",
                   command=self._exp_blocks).pack(side="left")
        ttk.Button(bot, text="Export pcap (carved IPv4)",
                   command=self._exp_ipv4).pack(side="left", padx=6)
        ttk.Button(bot, text="Export payload .bin",
                   command=self._exp_bin).pack(side="left")
        ttk.Button(bot, text="Save found files",
                   command=self._exp_files).pack(side="left", padx=6)
        ttk.Button(bot, text="Save call audio",
                   command=self._exp_audio).pack(side="left")
        self.expvar = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self.expvar).pack(side="left", padx=10)

    def _textpage(self, nb, title):
        f = ttk.Frame(nb)
        nb.add(f, text=title)
        t = tk.Text(f, bg="#11141a", fg=FG, insertbackground=FG,
                    relief="flat", wrap="none", font=("Consolas", 9))
        sy = ttk.Scrollbar(f, command=t.yview)
        t.configure(yscrollcommand=sy.set)
        sy.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        # Semantic colour tags, shared by every text page. Only the SIP tab
        # uses them today; naming them by role (a status class, a truncation
        # flag) rather than by colour keeps a tag meaning one thing wherever
        # it is applied, the same discipline as the info-panel palette.
        bold = tkfont.Font(font=t["font"])
        bold.configure(weight="bold")
        t.tag_configure("head", foreground=ACC, font=bold)
        t.tag_configure("method", foreground="#b48ead", font=bold)
        t.tag_configure("uri", foreground=ACC)
        t.tag_configure("hname", foreground=DIM)
        t.tag_configure("hval", foreground=VAL)
        t.tag_configure("ok", foreground="#8fd18a", font=bold)
        t.tag_configure("prov", foreground="#8fbfd4")
        t.tag_configure("bad", foreground="#e88388", font=bold)
        t.tag_configure("flag", foreground="#e8c07d")
        t.tag_configure("dim", foreground=DIM)
        t.tag_configure("audio", foreground="#8fd18a", font=bold)
        return t

    # -- actions ---------------------------------------------------------

    def _browse(self):
        p = filedialog.askopenfilename(
            filetypes=[("WAV IQ", "*.wav"), ("All", "*.*")])
        if p:
            self.pathvar.set(p)

    def _scan(self):
        """Fast survey: framing, unique words and timing, no turbo decoding.

        Roughly a ninth of the cost of a full decode (16 s vs 145 s on a 39 s
        capture), so a long recording can be triaged before committing to it.
        """
        p = self.pathvar.get().strip('"')
        if not p or not os.path.exists(p):
            messagebox.showerror("No capture", "Pick a .wav IQ capture first.")
            return
        if self.worker and self.worker.is_alive():
            return
        try:
            secs = float(self.secsvar.get())
        except ValueError:
            secs = None
        self.statvar.set("scanning...")
        self.pbar["value"] = 0
        self.update_idletasks()
        try:
            info, (tau_idx, offs, lvls, mets) = survey(p, secs=secs)
        except NoCarrier as exc:
            self._no_carrier(exc.rows, p)
            return
        except Exception as exc:
            self._log(f"scan failed: {exc}")
            self.statvar.set("scan failed - see Log")
            return
        self.pbar["value"] = 100
        uw = ", ".join(f"{k} x{v}" for k, v in
                       sorted(info["uw_levels"].items(),
                              key=lambda kv: -kv[1]))
        self.statvar.set(f"scan: {info['nframes']} frames, "
                         f">={100*info['est_yield']:.0f}% forecast")

        rows = [
            f"--- scan {Path(p).name} ---",
            f"  {info['secs']:.1f} s, centre {info['centre']:+.1f} Hz, "
            f"rs {info['rs']:.3f} Bd ({info['ppm']:+.2f} ppm)",
            f"  Es/N0 {info['esn0']:.1f} dB, {info['nframes']} frames",
            f"  unique words: {uw}",
            f"  timing phases: {info['tau_hist']}",
            f"  UW metric median {info['metric_med']:.1f}, "
            f"p90 {info['metric_p90']:.1f}",
            f"  forecast at least ~{100*info['est_yield']:.0f}% of blocks "
            f"(conservative)",
            f"  {len(info['runs'])} framing run(s)",
        ]
        for r in info["runs"][:40]:
            rows.append(f"    frames {r['start']:4d}..{r['end']:<4d} "
                        f"offset {r['offset']:6d} tau {r['tau']} "
                        f"UW {r['level']:>3} metric {r['metric']:5.1f}")
        if len(info["runs"]) > 40:
            rows.append(f"    ... {len(info['runs'])-40} more")
        self.tab_log.insert("end", NL + NL.join(rows) + NL)
        self.tab_log.see("end")

        extra = [("SCAN (no decoding)", [
            ("unique words", uw, "", None),
            ("framing runs", str(len(info["runs"])), "", None),
            ("UW metric", f"median {info['metric_med']:.1f}",
             f"p90 {info['metric_p90']:.1f}", None),
            ("forecast", f">= {100*info['est_yield']:.0f}% of blocks", "",
             _grade(info["est_yield"], 0.80, 0.40)),
        ])]
        self._show_info({**info, "path": p,
                         "file_hz": _freq_from_name(p),
                         "band": _band(None, None, 0.0)}, extra)

    def _no_carrier(self, rows, path):
        """Explain a capture with no F80T4.5X-8B carrier, rather than failing.

        A capture holding only 33.6 kBd bearers used to decode to noise and
        still report a high yield forecast. Saying what is actually present is
        more useful than an error.
        """
        self.pbar["value"] = 0
        self.statvar.set("no F80T4.5X-8B carrier in this capture")
        lines = ["", "--- no usable carrier ---", "  candidates probed:"]
        for r in rows:
            why = []
            if r["ratio"] < 1.8:
                why.append(f"UW correlation only {r['ratio']:.2f}x noise")
            if abs(r["ppm"]) > 50:
                why.append(f"clock {r['ppm']:+.0f} ppm off")
            lines.append(f"    {r['centre']/1e3:+9.1f} kHz  "
                         + ("; ".join(why) if why else "accepted"))
        found, err = [], None
        try:
            from tools.scan_bearers import detect_carriers, identify
            x, sr = recv.load_wav_iq(path, secs=20)
            x = x - x.mean()
            for c in detect_carriers(x, sr):
                best, _, _m4 = identify(x, sr, c)
                if best:
                    found.append((c["centre"], c["bw"], best[0], best[1]))
        except Exception as exc:
            err = str(exc)
        if found:
            lines += ["", "  what this capture actually contains:"] + [
                f"    {cen/1e3:+9.1f} kHz  bw {bw/1e3:5.1f} kHz  -> "
                f"{rs/1e3:.1f} kBd  {name}" for cen, bw, rs, name in found]
        if err:
            lines += ["", f"    (bearer scan failed: {err})"]
        lines += ["", "  This decoder handles F80T4.5X-8B (151.2 kBd) only.", ""]
        self.tab_log.insert("end", NL.join(lines) + NL)
        self.tab_log.see("end")

        sec = [(None, "NO USABLE CARRIER -- this decoder handles "
                "F80T4.5X-8B (151.2 kBd) only", "", "bad")]
        for cen, bw, rs, name in found:
            sec.append((f"{cen/1e3:+.1f} kHz", f"{rs/1e3:.1f} kBd",
                        f"{name}, bw {bw/1e3:.1f} kHz", None))
        if err:
            sec.append((None, f"bearer scan failed: {err}", "", "warn"))
        elif not found:
            sec.append((None, "no other bearer identified either", "", "note"))
        self._show_info(
            {"path": path, "file_hz": _freq_from_name(path), "secs": 0.0,
             "raw_sr": 0, "centre": 0.0, "rs": 0.0,
             "ppm": 0.0, "nframes": 0, "m4": float("nan"),
             "esn0": float("nan")},
            [("WHAT IS ACTUALLY HERE", sec)])

    def _probe_path(self):
        """Read the WAV header so the length is known before decoding.

        Header only -- no samples are read, so this stays instant even on a
        multi-gigabyte capture.
        """
        self.probe = probe_wav(self.pathvar.get().strip('"'))
        p = self.probe
        if p is None:
            self.capvar.set("no capture selected (or not a readable WAV)")
            return
        frames = p["secs"]/0.080
        est = p["secs"]*EST_SEC_PER_SEC*(
            1.0 if self.searchvar.get() else EST_NOSEARCH_RATIO)
        self.capvar.set(
            f"{p['secs']:.1f} s  |  {p['sr']/1e3:.0f} kHz  {p['ch']}ch "
            f"{8*p['width']}-bit  |  {p['mb']:.0f} MB  |  "
            f"~{frames:.0f} frames, {frames*8:.0f} blocks  |  "
            f"full decode ~{_hms(est)}")

    def _use_max(self):
        if self.probe:
            self.secsvar.set(f"{self.probe['secs']:.1f}")
        else:
            self.capvar.set("pick a capture first")

    def _start(self):
        p = self.pathvar.get().strip('"')
        if not p or not os.path.exists(p):
            messagebox.showerror("No capture", "Pick a .wav IQ capture first.")
            return
        if self.worker and self.worker.is_alive():
            return
        try:
            secs = float(self.secsvar.get())
        except ValueError:
            secs = None
        # Clamp to the file: asking for more than exists silently gives you
        # the whole file, which makes the progress and time estimate wrong.
        if self.probe and secs and secs > self.probe["secs"]:
            self._log(f"requested {secs:.1f} s but the capture is only "
                      f"{self.probe['secs']:.1f} s - using the whole file")
            secs = self.probe["secs"]
            self.secsvar.set(f"{secs:.1f}")
        for t in (self.tab_int, self.tab_term, self.tab_files, self.tab_sip,
                  self.tab_str, self.tab_bb, self.tab_pkt, self.tab_log):
            t.delete("1.0", "end")
        self.stop.clear()
        self.result = None
        self.worker = Worker(p, secs, self.searchvar.get(), self.q, self.stop)
        self.worker.start()
        self._log(f"decoding {p}  ({secs} s, "
                  f"{'level search' if self.searchvar.get() else 'UW level'})")

    def _log(self, s):
        self.tab_log.insert("end", s + "\n")
        self.tab_log.see("end")

    # -- queue -----------------------------------------------------------

    def _poll(self):
        try:
            while True:
                kind, kw = self.q.get_nowait()
                self._handle(kind, kw)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _handle(self, kind, kw):
        if kind in ("status", "progress"):
            if "pct" in kw:
                self.pbar["value"] = kw["pct"]
            if "text" in kw:
                self.statvar.set(kw["text"])
            if kw.get("const") is not None:
                self._draw_const(kw["const"])
        elif kind == "psd":
            self._draw_psd(kw["f"], kw["p"], kw["sr"])
        elif kind == "info":
            self._show_info(kw["info"])
        elif kind == "track":
            self._log(f"framing: {kw['nplateau']} plateau(s) over "
                      f"{kw['nframes']} frames, offset span {kw['span']} "
                      f"symbols")
        elif kind == "nocarrier":
            self._no_carrier(kw["rows"], kw["path"])
        elif kind == "updchecked":
            self._update_checked(kw["st"], kw["announce"])
        elif kind == "updapplied":
            self._update_applied(kw["done"], kw["out"], kw["st"])
        elif kind == "error":
            self._log(kw["text"])
            self.statvar.set("error - see Log tab")
        elif kind == "done":
            self.result = kw["result"]
            self._finish(kw["result"])

    # -- drawing ---------------------------------------------------------

    def _draw_psd(self, f, p, sr):
        a = self.ax_psd
        a.clear()
        a.set_facecolor("#11141a")
        a.plot(f/1e3, p, lw=0.6, color=ACC)
        a.set_xlabel("offset from centre (kHz)", color=FG, fontsize=8)
        a.set_ylabel("dB", color=FG, fontsize=8)
        a.set_title("spectrum", color=FG, fontsize=9)
        a.grid(alpha=0.15)
        a.tick_params(colors=FG, labelsize=8)
        self.canvas.draw_idle()

    def _draw_const(self, z):
        a = self.ax_con
        a.clear()
        a.set_facecolor("#11141a")
        if len(z):
            n = min(len(z), 6000)
            zz = z[:n]
            a.plot(zz.real, zz.imag, ".", ms=1.1, alpha=0.35, color=GOOD)
            lim = 2.0
            for v in (-1.5, -0.5, 0.5, 1.5):
                a.axhline(v, color="#39404d", lw=0.4)
                a.axvline(v, color="#39404d", lw=0.4)
            a.set_xlim(-lim, lim)
            a.set_ylim(-lim, lim)
        a.set_aspect("equal")
        a.set_title("constellation (16-QAM, pilot-corrected)", color=FG,
                    fontsize=9)
        a.tick_params(colors=FG, labelsize=8)
        self.canvas.draw_idle()

    # -- info panel ------------------------------------------------------
    #
    # Sections are (title, rows); a row is (label, value, note, status).
    # label=None makes a full-width banner. Building the structure rather
    # than pre-formatted lines is what lets a resize re-flow it.

    def _info_resized(self, _evt=None):
        if self._info_width() != self._info_cols:
            self._render_info()

    def _info_width(self):
        w = self.infobox.winfo_width()
        if w <= 1:
            return 44
        return max(30, (w - 26)//max(1, self._info_font.measure("0")))

    def _render_info(self):
        cols = self._info_cols = self._info_width()
        # Size the label column to the labels, once for the whole panel so
        # the sections line up. Never truncate: a clipped label ("Es/N0 (es")
        # costs more than the character it saves.
        labs = [len(r[0]) for _, rows in self._info_sections for r in rows
                if r and r[0] is not None]
        lab_w = min(LAB_W, max(labs) + 3) if labs else LAB_W
        t = self.infobox
        t.configure(state="normal")
        t.delete("1.0", "end")
        for title, rows in self._info_sections:
            rows = [r for r in rows if r]
            if not rows:
                continue
            t.insert("end", f"{title} {'-'*max(3, cols - len(title) - 1)}\n",
                     "sec")
            for row in rows:
                self._info_row(row, cols, lab_w)
        t.configure(state="disabled")

    def _info_row(self, row, cols, lab_w=LAB_W):
        t = self.infobox
        label, value, note, status = row
        if label is None:                       # full-width banner
            for ln in textwrap.wrap(value, max(12, cols - 2)) or [""]:
                t.insert("end", "  " + ln + "\n", status or "warn")
            return
        t.insert("end", "  " + label.ljust(lab_w - 2), "lab")
        if len(label) > lab_w - 3:              # longer than its column
            t.insert("end", "\n" + " "*lab_w)

        if isinstance(value, Fit):      # one token, so internal spaces survive
            words = [(_elide(str(value), max(8, cols - lab_w)),
                      status or "val")]
        else:
            words = [(w, status or "val") for w in str(value).split()]
        if note:
            words += [(w, "note") for w in str(note).split()]
        avail = max(8, cols - lab_w)
        flat = []
        for w, tag in words:                    # hard-split unbreakable tokens
            while len(w) > avail:               # (long filenames, mostly)
                flat.append((w[:avail], tag))
                w = w[avail:]
            flat.append((w, tag))

        col, started = lab_w, False
        for w, tag in flat:
            if started and col + 1 + len(w) > cols:
                t.insert("end", "\n" + " "*lab_w)
                col, started = lab_w, False
            if started:
                t.insert("end", " ")
                col += 1
            t.insert("end", w, tag)
            col += len(w)
            started = True
        t.insert("end", "\n")

    def _show_info(self, i, extra=()):
        # Rows are omitted rather than shown as placeholders. The no-carrier
        # path has no symbol rate, no frames and no M4/M2^2, and printing
        # "0.000 Bd" and "nan" for them reads as a broken decode instead of
        # an absent one.
        hz = i.get("file_hz")
        car = [("file", Fit(Path(i["path"]).name.strip()), "", None)]
        if i.get("secs"):
            car.append(("capture", f"{i['secs']:.1f} s",
                        f"@ {i['raw_sr']/1e3:.0f} kHz" if i.get("raw_sr")
                        else "", None))
        car.append(("centre", f"{(hz + i['centre'])/1e6:.6f} MHz" if hz
                    else f"{i['centre']:+.1f} Hz",
                    "" if hz else "from capture centre", None))
        if i.get("centre"):
            car.append(("carrier offset", f"{i['centre']:+.1f} Hz", "", None))
        b = i.get("band")
        if b:
            if hz:
                lo = (hz + i["centre"] - b["alloc"]/2)/1e6
                car.append(("occupied band",
                            f"{lo:.4f} - {lo + b['alloc']/1e6:.4f} MHz",
                            "", None))
            car.append(("occupied BW", f"{b['alloc']/1e3:.1f} kHz",
                        f"allocated, {b['p99']/1e3:.1f} kHz 99% power "
                        f"(alpha {spec.ROLLOFF}, from spec -- not measured)",
                        None))
            if np.isfinite(b["balance"]):
                bal = 0.0 if abs(b["balance"]) < 0.005 else b["balance"]
                car.append(("band symmetry", f"{bal:+.2f} dB",
                            "upper/lower (0 = centred)",
                            _grade(abs(bal), 0.5, 1.5, invert=True)))
            if b.get("clipped", 0) > 0:
                car.append((None, f"** {b['clipped']/1e3:.1f} kHz of the "
                            f"allocation falls outside the capture",
                            "", "bad"))

        bearer = [("type", "F80T4.5X-8B", "(16-QAM)", None)]
        if i.get("rs"):
            bearer.append(("symbol rate", f"{i['rs']:.3f} Bd",
                           f"({i['ppm']:+.2f} ppm)",
                           _grade(abs(i["ppm"]), 5.0, 50.0, invert=True)))
        bearer.append(("nominal", "151200 Bd",
                       "(80 ms frame = 12096 sym)", None))
        if i.get("nframes"):
            bearer.append(("frames in file", str(i["nframes"]), "", None))

        qual = []
        if np.isfinite(i.get("m4", float("nan"))):
            qual.append(("M4/M2^2", f"{i['m4']:.3f}",
                         "(16QAM 1.32 / QPSK 1.00 / noise 2.00)",
                         _grade(abs(i["m4"] - 1.32), 0.10, 0.30, invert=True)))
        if np.isfinite(i.get("esn0", float("nan"))):
            qual.append(("Es/N0 (est)", f"{i['esn0']:.1f} dB", "",
                         _grade(i["esn0"], 10.0, 6.0)))
        # Unique-word EVM is the only figure here that predicts a residual
        # carrier offset. The UW correlation metric does not -- being
        # differential, it stays at 60-72 on captures that decode nothing.
        if np.isfinite(i.get("uw_evm", float("nan"))):
            qual.append(("UW EVM", f"{i['uw_evm']:.3f}",
                         "(~0.17 decodes, ~0.45 does not)",
                         _grade(i["uw_evm"], 0.25, 0.45, invert=True)))
        if i.get("jobs"):
            qual.append(("decode threads", str(i["jobs"]), "", None))
        if i.get("cfo_applied"):
            past = abs(i["cfo_hz"]) > 552.0
            qual.append(("carrier resid", f"{i['cfo_hz']:+.1f} Hz removed",
                         "(past pilot-unwrap limit)" if past else "",
                         "warn" if past else None))

        self._info_sections = [("CARRIER", car), ("BEARER", bearer),
                               ("QUALITY", qual)] + list(extra)
        self._render_info()

    # -- completion ------------------------------------------------------

    def _finish(self, r):
        self.pbar["value"] = 100
        n = len(r.recs)
        tot = len(r.offs)*8 if r.offs is not None else 0
        self.statvar.set(f"done - {n}/{tot} blocks ({100*n/max(tot,1):.1f}%)")
        if len(r.const):
            self._draw_const(r.const)

        byb = np.zeros(8, int)
        lv = {}
        for f, b, l, ag, _ in r.recs:
            byb[b] += 1
            lv[l] = lv.get(l, 0) + 1
        ags = [x[3] for x in r.recs]
        pct = 100*n/max(tot, 1)
        dec = [
            ("blocks", f"{n}/{tot}", f"({pct:.1f}%)", _grade(pct, 80.0, 40.0)),
            ("per block idx", " ".join(str(v) for v in byb), "", None),
            ("median agree", f"{np.median(ags):.3f}", "",
             _grade(float(np.median(ags)), 0.85, 0.75)) if ags else None,
            ("levels", ", ".join(
                f"{k}:{v}" for k, v in sorted(lv.items(), key=lambda kv: -kv[1])
            ), "", None),
            ("payload", f"{len(r.payload):,} bytes", "", None),
        ]
        if r.payload:
            z = 100*r.payload.count(0)/len(r.payload)
            dec.append(("zero bytes", f"{z:.1f}%", "", None))
        extra = [("DECODE", dec)]
        if r.bulletins:
            bb = r.bulletins[0][1]
            bc = [
                ("BulletinBoards", str(len(r.bulletins)),
                 (f"every {r.bb_period} frames" if r.bb_period else "")
                 + (f", block 0 at level {r.bb_level}"
                    if r.bb_level else ""), None),
                ("rnc-id/bct-id", f"{bb.rnc_id} / {bb.bct_id}", "", None),
                ("f-bearer", str(bb.f_bearer), f"net-ver {bb.net_ver}", None),
                ("spot-beam-id", str(bb.spot_beam_id), "", None),
            ]
            plmn = _plmn(bb)
            if plmn:
                bc.append(("PLMN", plmn, "", None))
            extra.append(("BEARER CONTROL", bc))
        self._show_info(r.info, extra)

        self._fill_interesting(r)
        self._fill_terminals(r)
        self._fill_files(r)
        self._fill_sip(r)
        self._fill_strings(r)
        self._fill_bb(r)
        self._fill_pkts(r)
        self._log(f"done: {n} blocks, {len(r.payload)} bytes payload")

    def _fill_terminals(self, r):
        """Terminal addresses, their public IPs, and the ICMP around them.

        The forward link carries traffic *to* terminals, so every carved
        destination is one. Public addresses get a second, independent check
        from the "what is my IP" DNS answers, and where such an answer was
        delivered to the very address it named, the terminal is holding a
        public IP directly rather than sitting behind carrier NAT.
        """
        t = self.tab_term

        def put(s, *tags):
            t.insert("end", s, tags or ())

        ts = terminals.terminals(r.payload)
        msgs, tally, flows = terminals.icmp_summary(r.payload)
        pairs = terminals.nat_pairs(r.payload)
        r.terminals, r.icmp = ts, msgs
        if not ts and not msgs:
            put("No terminal addresses found." + NL*2
                + "Needs checksum-valid IPv4 in the payload, so a short or "
                  "lossy decode yields none." + NL)
            return

        pub = [x for x in ts if x.kind == "public" and x.confidence != "weak"]
        priv = [x for x in ts if x.kind == "private" and x.confidence != "weak"]
        weak = [x for x in ts if x.confidence == "weak"]

        put(f"PUBLIC IPs  ({len(pub)}){NL}", "head")
        if pub:
            put("   Every destination on the forward link is a terminal."
                + NL
                + "   SELF-CONFIRMED means a 'what is my IP' answer naming "
                  "that address was" + NL
                + "   delivered to it, so the terminal holds it directly "
                  "rather than sitting" + NL
                + "   behind carrier NAT." + NL, "dim")
            for x in pub:
                put(f"   {x.addr:16s}", "ok")
                put(f"  {x.packets:5d} pkts", "dim")
                if x.echoed:
                    put(f"   echoed {x.echoed}x", "hval")
                    put(f" via {', '.join(x.echo_names)}", "dim")
                if x.self_confirmed:
                    put("   SELF-CONFIRMED", "audio")
                put(f"   [{x.confidence}]" + NL, "dim")
        else:
            put("   none seen. Terminals behind carrier NAT only ever show "
                "their private address" + NL
                + "   on the link; a public one appears when the terminal "
                  "asks for it, or when it" + NL
                + "   is not NATed at all." + NL, "dim")
        put(NL)

        if pairs:
            put(f"NAT MAPPINGS  ({len(pairs)}){NL}", "head")
            for k, v in pairs.items():
                put(f"   {k:16s}", "uri")
                put("  ->  ", "dim")
                put(", ".join(f"{a} x{n}" for a, n in v.items()) + NL, "ok")
            put(NL)

        put(f"PRIVATE ADDRESSES  ({len(priv)}){NL}", "head")
        for x in priv[:40]:
            put(f"   {x.addr:16s}", "uri")
            put(f"  {x.packets:5d} pkts   [{x.confidence}]" + NL, "dim")
        if len(priv) > 40:
            put(f"   ... {len(priv)-40} more{NL}", "dim")
        put(NL)

        if weak:
            put(f"WEAK  ({len(weak)}, fewer than "
                f"{terminals.MIN_PACKETS} packets){NL}", "head")
            put("   Below the evidence bar. A carved packet can pass its "
                "header checksum by chance," + NL
                + "   and correspondents leak in here rather than being "
                  "dropped silently." + NL, "dim")
            put("   " + ", ".join(x.addr for x in weak[:24]) + NL, "flag")
            put(NL)

        put(f"ICMP  ({len(msgs)} messages){NL}", "head")
        for k, v in tally.most_common():
            put(f"   {v:5d}  {k}{NL}", "hval" if "unreach" in k else "dim")
        if flows:
            put(f"   {len(flows)} flow(s) named by quoted headers -- an error "
                f"quotes the packet{NL}", "dim")
            put(f"   that caused it, so these were seen only indirectly:{NL}",
                "dim")
            P = {1: "ICMP", 6: "TCP", 17: "UDP", 47: "GRE", 50: "ESP"}
            for f in sorted(flows)[:24]:
                put(f"      {f[0]}:{f[3]} -> {f[1]}:{f[4]} "
                    f"{P.get(f[2], f[2])}{NL}", "hval")
            if len(flows) > 24:
                put(f"      ... {len(flows)-24} more{NL}", "dim")

    def _fill_files(self, r):
        """Whole files reconstructed from HTTP bodies, with a preview.

        Each is shown with whether it looks intact. That flag is not
        decoration: there is no Bearer Connection reassembly, so the bytes
        after a header may belong to a different flow, and the only available
        check is whether the body is self-consistent with its own declared
        type. Anything marked TRUNCATED should be read as a fragment.
        """
        t = self.tab_files
        docs = findings.documents(r.payload)
        r.documents = docs
        if not docs:
            t.insert("end", "No complete files found." + NL*2
                     + "A body needs a Content-Length or chunk framing to be "
                       "bounded at all, and needs to survive without a failed "
                       "block in the middle. Short or lossy decodes yield "
                       "none." + NL)
            return
        from collections import Counter
        tally = Counter(d.check for d in docs)
        t.insert("end", f"{len(docs)} file(s), "
                        + ", ".join(f"{v} {k}" for k, v in tally.most_common())
                        + f", {sum(len(d.data) for d in docs)} bytes total{NL}")
        t.insert("end", "Carved from HTTP bodies, not reassembled. "
                        '"Save found files" writes them to a folder.' + NL)
        t.insert("end", "intact = the body is self-consistent with its own "
                        "type; unverified = that type carries no check." + NL*2)
        for d in docs:
            t.insert("end", f"{d.name}   {d.ctype}   {len(d.data)} B   "
                            f"[{d.check}]{NL}")
            t.insert("end", f"   @{d.offset}  {d.status}"
                            + (f"   {d.note}" if d.note else "") + NL)
            body = d.data[:400]
            # decide text against hex by what the bytes actually are, not by
            # what the header claimed: a mislabelled or interrupted body
            # renders as mojibake otherwise
            printable = sum(1 for c in body
                            if 0x09 <= c <= 0x7e or c in (0x0a, 0x0d))
            if body and printable/len(body) > 0.9:
                for line in body.decode("utf-8", "replace").splitlines()[:8]:
                    t.insert("end", f"      {line[:140]}{NL}")
            else:
                for k in range(0, min(len(body), 96), 32):
                    t.insert("end", f"      {body[k:k+32].hex(' ')}{NL}")
            t.insert("end", NL)

    def _status_tag(self, code):
        """Colour class for a SIP response code: 2xx good, 1xx provisional,
        4xx/5xx/6xx bad. Matches the outcome phrasing sip.Dialog.outcome uses.
        """
        if 200 <= code < 300:
            return "ok"
        if 100 <= code < 200:
            return "prov"
        return "bad"

    def _fill_sip(self, r):
        """SIP dialogs, and any RTP audio the same payload carried.

        Grouping is the only "assembly" claimed here. There is no Bearer
        Connection reassembly, so a dialog is the messages that carried the
        same Call-ID and survived, in wire order -- not a complete call.
        Anything missing is missing silently, so read a short dialog as
        "this is what got through", never as "this is what happened".

        Colour is by role: request methods, response classes (2xx good, 1xx
        provisional, else bad), header names vs values, and decoded audio.
        """
        t = self.tab_sip

        def put(text, *tags):
            t.insert("end", text, tags or ())

        msgs, dls = sip.scan(r.payload)
        streams = rtp.streams(r.payload)
        r.sip_messages, r.sip_dialogs, r.rtp_streams = msgs, dls, streams

        if not msgs and not streams:
            put("No SIP or RTP found." + NL*2
                + "SIP is text, so it only appears where the traffic is not "
                  "inside TLS -- signalling to a gateway, typically -- and "
                  "RTP audio only exists once a call is actually set up. It "
                  "also needs a long enough decode: the 1534.499 capture "
                  "shows nothing in its first 30 s and, over the full 224 s, "
                  "five OPTIONS keepalives and no media (a keepalive sets up "
                  "no call, so there is no audio to find)." + NL)
            return

        # --- audio first: it is the thing people came for -------------------
        self._sip_audio_section(put, streams)

        if not msgs:
            return
        dmg = sum(1 for m in msgs if m.damaged)
        cut = sum(1 for m in msgs if m.check != "intact")
        put(f"{len(msgs)} message(s) in {len(dls)} dialog(s)"
            + (f", {cut} truncated" if cut else "")
            + (f", {dmg} with a lost first byte" if dmg else "") + NL, "dim")
        put("Grouped by Call-ID in wire order. Authorization and "
            "WWW-Authenticate values are redacted." + NL*2, "dim")

        for d in dls:
            put(f"CALL-ID {d.call_id or '(none recovered)'}{NL}", "head")
            put("   ")
            put(d.from_uri, "uri")
            put("  ->  ", "dim")
            put(d.to_uri + NL, "uri")
            put("   ")
            put(d.outcome, self._outcome_tag(d))
            put((f"   methods: {', '.join(d.methods)}" if d.methods else "")
                + f"   ({len(d.messages)} message(s)){NL}", "dim")
            for md in d.media:
                put(f"   media: {md.kind} {md.address}:{md.port} "
                    f"{md.proto}  {', '.join(md.formats)}{NL}", "audio")
            for m in d.messages:
                put(f"   @{m.offset}  ", "dim")
                if m.kind == "request":
                    put(m.method + " ", "method")
                    put(m.uri, "uri")
                else:
                    put(f"{m.status} ", self._status_tag(m.status))
                    put(m.reason, self._status_tag(m.status))
                flags = [] if m.check == "intact" else [m.check]
                if m.damaged:
                    flags.append("lost 1st byte")
                if flags:
                    put(f"   [{', '.join(flags)}]", "flag")
                put(NL)
                for k, v in m.headers:
                    put(f"        {k}: ", "hname")
                    put(v + NL, "hval")
                if m.body:
                    for line in m.body.decode("utf-8", "replace"
                                              ).splitlines()[:12]:
                        put(f"        | {line[:150]}{NL}", "dim")
            put(NL)

    def _outcome_tag(self, d):
        codes = [m.status for m in d.messages if m.kind == "response"]
        if any(200 <= c < 300 for c in codes):
            return "ok"
        if any(c >= 400 for c in codes):
            return "bad"
        return "prov"

    def _sip_audio_section(self, put, streams):
        if not streams:
            put("AUDIO (RTP)" + NL, "head")
            put("   none -- no RTP stream in this payload. Signalling can be "
                "present without any call being set up." + NL*2, "dim")
            return
        dec = [s for s in streams if s.decodable]
        put(f"AUDIO (RTP) -- {len(streams)} stream(s), "
            f"{len(dec)} decodable{NL}", "head")
        for s in streams:
            put("   ")
            put(s.codec + "  ", "audio" if s.decodable else "flag")
            put(f"{s.src}:{s.sport} -> {s.dst}:{s.dport}  "
                f"ssrc {s.ssrc:#010x}  {len(s.packets)} pkts", "dim")
            if s.decodable:
                put(f"  {s.seconds:.1f}s", "audio")
            if s.lost:
                put(f"  {s.lost} lost", "flag")
            put(NL)
        if dec:
            put('   "Save call audio" writes each decodable stream to an '
                "8 kHz WAV." + NL, "dim")
        put(NL)

    def _exp_audio(self):
        r = self._need()
        if not r:
            return
        streams = getattr(r, "rtp_streams", None)
        if streams is None:
            streams = rtp.streams(r.payload)
        dec = [s for s in streams if s.decodable]
        if not dec:
            messagebox.showinfo(
                "No audio",
                "No decodable RTP stream in this payload.\n\n"
                "G.711 (PCMU/PCMA) is decoded; a call must be set up for any "
                "RTP to exist at all. The captures here carry OPTIONS "
                "keepalives, which establish no media.")
            return
        d = filedialog.askdirectory(title="Folder for the call audio")
        if not d:
            return
        stem = safe_stem(Path(r.info.get("path", "capture")).stem)
        n = 0
        for s in dec:
            name = f"{stem}_rtp_{s.ssrc:08x}_{s.codec}.wav"
            rtp.write_wav(s, Path(d)/name)
            n += 1
        self.expvar.set(f"wrote {n} WAV file(s) to {d}")

    def _exp_files(self):
        r = self._need()
        if not r:
            return
        docs = getattr(r, "documents", None)
        if docs is None:
            docs = findings.documents(r.payload)
        if not docs:
            messagebox.showinfo("Nothing to save",
                                "No complete files were reconstructed.")
            return
        d = filedialog.askdirectory(title="Folder for the reconstructed files")
        if not d:
            return
        stem = safe_stem(r.info.get("path"))
        out = Path(d)/f"{stem}_files"
        out.mkdir(parents=True, exist_ok=True)
        for doc in docs:
            # truncated bodies are still worth keeping, but say so in the name
            nm = (doc.name if doc.check != "truncated"
                  else doc.name.replace(".", ".partial.", 1))
            (out/nm).write_bytes(doc.data)
        self.expvar.set(f"wrote {len(docs)} file(s) -> {out.name}")

    def _fill_interesting(self, r):
        """Recognisable artefacts: certificates, DNS, HTTP, TLS, URLs.

        Leads with the hostnames, because "who was this terminal talking to"
        is the question the rest of the tab only answers piecemeal.
        """
        t = self.tab_int
        fs = findings.scan(r.payload)
        by = {}
        for f in fs:
            by.setdefault(f.kind, []).append(f)
        if not fs:
            t.insert("end", "Nothing recognisable found." + NL*2
                     + "The extractors need an artefact to sit inside the "
                       "decoded bytes intact. On a short or lossy decode "
                       "there may be none." + NL)
            return

        hosts = findings.hosts(fs)
        t.insert("end", f"{len(fs)} findings in {len(r.payload)} bytes: "
                        + ", ".join(f"{len(v)} {k}"
                                    for k, v in sorted(by.items())) + NL)
        t.insert("end",
                 "Carved from decoded blocks, not reassembled: anything "
                 "spanning a failed block is lost." + NL
                 + "Certificates rarely parse whole for that reason and are "
                   "mostly recovered from their validity block." + NL
                 + "0 false accepts per 8 MB of random bytes, except URLs "
                   "which are a plain regex." + NL*2)

        if hosts:
            t.insert("end", f"HOSTS SEEN  ({len(hosts)} distinct){NL}")
            for h, n in hosts.most_common(60):
                t.insert("end", f"   {n:3d}  {h}{NL}")
            if len(hosts) > 60:
                t.insert("end", f"   ... {len(hosts)-60} more{NL}")
            t.insert("end", NL)

        titles = dict(cert="CERTIFICATES", dns="DNS", tls="TLS HANDSHAKES",
                      http="HTTP", url="URLS (regex, expect fragments)")
        for kind in ("cert", "dns", "tls", "http", "url"):
            sel = by.get(kind)
            if not sel:
                continue
            t.insert("end", f"{titles[kind]}  ({len(sel)}){NL}")
            for f in sel:
                t.insert("end", f"   @{f.offset:8d}  {f.summary}{NL}")
                for d in f.detail:
                    t.insert("end", f"                 {d}{NL}")
            t.insert("end", NL)

    def _fill_strings(self, r):
        runs = [(m.start(), m.group()) for m in
                re.finditer(rb"[\x20-\x7e]{8,}", r.payload)]
        self.tab_str.insert("end",
                            f"{len(runs)} printable runs of 8+ chars in "
                            f"{len(r.payload)} bytes\n"
                            f"(payload is decoded blocks concatenated in "
                            f"order; gaps where blocks failed)\n\n")
        for off, s in runs:
            self.tab_str.insert("end",
                                f"{off:8d}  {s.decode('ascii','replace')}\n")

    def _fill_bb(self, r):
        t = self.tab_bb
        if not r.bulletins:
            t.insert("end", "No BulletinBoard found.\n\n"
                     "It is broadcast only every Nth frame (17 on the "
                     "captures seen so far), so a short or lossy decode may "
                     "contain none.\n")
            return
        t.insert("end", f"frame-no offset {r.bb_offset}, "
                        f"{len(r.bulletins)} BulletinBoard(s)"
                        + (f", period {r.bb_period} frames\n\n"
                           if r.bb_period else "\n\n"))
        for f, bb in r.bulletins:
            t.insert("end", f"frame {f:5d}  {bb}\n")
            for a in bb.avps[:4]:
                t.insert("end", f"           avp 0x{a.type:02x} "
                                f"len {a.param_len}  {a.value.hex()}\n")
        t.insert("end", "\nNote: only the leading AVPs are trustworthy. The "
                        "walk runs past the end of\nthe real list because a "
                        "garbage octet still resolves to a defined type\n"
                        "about 37% of the time.\n")

    def _fill_pkts(self, r):
        got = list(pcapout.carve_ipv4(r.payload))
        t = self.tab_pkt
        t.insert("end",
                 f"{len(got)} IPv4 packets carved (header checksum valid)\n"
                 f"Carve, not demux: packets spanning failed blocks are lost.\n"
                 f"False-accept rate measured at 0 per 2 MB of random bytes.\n\n")
        for off, pk in got:
            src = ".".join(str(x) for x in pk[12:16])
            dst = ".".join(str(x) for x in pk[16:20])
            pr = {1: "ICMP", 6: "TCP", 17: "UDP"}.get(pk[9], str(pk[9]))
            t.insert("end", f"@{off:7d}  {pr:5} {len(pk):5d} B  "
                            f"{src:>15} -> {dst}\n")

    # -- export ----------------------------------------------------------

    def _need(self):
        if not self.result or not self.result.recs:
            messagebox.showinfo("Nothing to export", "Decode a capture first.")
            return None
        return self.result

    def _exp_blocks(self):
        r = self._need()
        if not r:
            return
        p = filedialog.asksaveasfilename(
            defaultextension=".pcap",
            initialfile=f"{safe_stem(r.info.get('path'))}_blocks.pcap")
        if not p:
            return
        n = pcapout.write_blocks(
            p, ((f, b, bits) for f, b, l, ag, bits in r.recs))
        self.expvar.set(f"wrote {n} block records -> {Path(p).name}")
        self._log(f"pcap (blocks): {n} records, DLT_USER0 -> {p}")

    def _exp_ipv4(self):
        r = self._need()
        if not r:
            return
        p = filedialog.asksaveasfilename(
            defaultextension=".pcap",
            initialfile=f"{safe_stem(r.info.get('path'))}_ipv4.pcap")
        if not p:
            return
        n = pcapout.write_ipv4(p, r.payload)
        self.expvar.set(f"wrote {n} carved IPv4 packets -> {Path(p).name}")
        self._log(f"pcap (IPv4 carve): {n} packets, DLT_RAW -> {p}\n"
                  f"  timestamps are synthetic; a carved packet has no "
                  f"recoverable arrival time")

    def _exp_bin(self):
        r = self._need()
        if not r:
            return
        p = filedialog.asksaveasfilename(
            defaultextension=".bin",
            initialfile=f"{safe_stem(r.info.get('path'))}_payload.bin")
        if not p:
            return
        Path(p).write_bytes(r.payload)
        self.expvar.set(f"wrote {len(r.payload)} bytes -> {Path(p).name}")


def _plmn(bb):
    for a in bb.avps[:6]:
        if a.type == 0xF2 or (a.type >> 3) == 0x1E:      # plmn-info-len-3/4
            v = a.value
            if len(v) >= 3:
                d = [v[0] >> 4, v[0] & 0xF, v[1] >> 4,
                     v[1] & 0xF, v[2] >> 4, v[2] & 0xF]
                mcc = "".join(str(x) for x in d[:3])
                mnc = "".join(str(x) for x in d[3:] if x != 0xF)
                return f"{mcc}-{mnc}" + (" (Inmarsat)"
                                         if mcc == "901" and mnc == "11"
                                         else "")
    return None


if __name__ == "__main__":
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()
