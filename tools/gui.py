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
import threading
import wave
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tkinter as tk                                          # noqa: E402
from tkinter import ttk, filedialog, messagebox               # noqa: E402

import matplotlib                                             # noqa: E402
matplotlib.use("TkAgg")
from matplotlib.figure import Figure                          # noqa: E402
from matplotlib.backends.backend_tkagg import (                # noqa: E402
    FigureCanvasTkAgg)
from scipy.signal import welch                                # noqa: E402

from bgan import spec, mod, recv, bulletin, pcapout, findings  # noqa: E402
from tools.decode_wav import (decode_capture, survey, NoCarrier,  # noqa: E402
                              channelise, safe_stem, MOS)

NL = chr(10)
BG = "#1c1f26"
FG = "#d8dee9"
ACC = "#5fb3d4"
WARN = "#e0a33e"
GOOD = "#8fbf6f"


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
        r.info["occ_bw"] = _occupied_bw(fr[i], P[i])
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


def _bulletins(r):
    uw = "L3"
    sel = [(f, bits) for (f, b, lv, ag, bits) in r.recs if b == 0 and lv == uw]
    if len(sel) < 3:
        return
    frames = [f for f, _ in sel]
    pays = [bits for _, bits in sel]
    try:
        off, mask, period = bulletin.confirm(pays, frames)
    except Exception:
        return
    r.bb_offset, r.bb_period = off, period
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


def _occupied_bw(f, p, frac=0.99):
    """99% power bandwidth, in Hz."""
    p = np.maximum(p - np.median(np.sort(p)[:len(p)//5]), 0)
    c = np.cumsum(p)
    if c[-1] <= 0:
        return float("nan")
    c = c/c[-1]
    lo = f[np.searchsorted(c, (1 - frac)/2)]
    hi = f[np.searchsorted(c, 1 - (1 - frac)/2)]
    return float(hi - lo)


# --------------------------------------------------------------------------
# app


class App(tk.Tk):
    def __init__(self, initial=None):
        super().__init__()
        self.title("BGAN forward-link decoder")
        self.geometry("1280x900")
        self.configure(bg=BG)
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.result = None
        self._style()
        self._build()
        if initial:
            self.pathvar.set(initial)
        self.after(80, self._poll)

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

    def _build(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self.pathvar = tk.StringVar()
        ttk.Label(top, text="Capture").pack(side="left")
        ttk.Entry(top, textvariable=self.pathvar, width=70).pack(
            side="left", padx=6)
        ttk.Button(top, text="Browse", command=self._browse).pack(side="left")
        ttk.Label(top, text="  seconds").pack(side="left")
        self.secsvar = tk.StringVar(value="20")
        ttk.Entry(top, textvariable=self.secsvar, width=6).pack(side="left")
        ttk.Button(top, text="Max", command=self._use_max,
                   width=5).pack(side="left", padx=(2, 0))
        self.searchvar = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="search levels (10x slower, needed for "
                                  "blocks 1-7)",
                        variable=self.searchvar,
                        command=lambda: self._probe_path()).pack(
                            side="left", padx=8)
        ttk.Button(top, text="Scan",
                   command=self._scan).pack(side="left", padx=(4, 0))
        self.btn = ttk.Button(top, text="Decode", command=self._start)
        self.btn.pack(side="left", padx=4)
        ttk.Button(top, text="Stop", command=self.stop.set).pack(side="left")

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

        mid = ttk.Frame(self, padding=8)
        mid.pack(fill="both", expand=True)

        self.fig = Figure(figsize=(9, 3.4), facecolor=BG, tight_layout=True)
        self.ax_psd = self.fig.add_subplot(121)
        self.ax_con = self.fig.add_subplot(122)
        for a in (self.ax_psd, self.ax_con):
            a.set_facecolor("#11141a")
            a.tick_params(colors=FG, labelsize=8)
            for sp in a.spines.values():
                sp.set_color("#39404d")
        self.canvas = FigureCanvasTkAgg(self.fig, master=mid)
        self.canvas.get_tk_widget().pack(side="left", fill="both", expand=True)

        self.infobox = tk.Text(mid, width=42, bg="#11141a", fg=FG,
                               insertbackground=FG, relief="flat",
                               font=("Consolas", 9))
        self.infobox.pack(side="left", fill="both", padx=(8, 0))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tab_int = self._textpage(nb, "Interesting")
        self.tab_files = self._textpage(nb, "Files")
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

        extra = NL.join([
            "", "", "SCAN (no decoding)",
            f"  unique words    {uw}",
            f"  framing runs    {len(info['runs'])}",
            f"  UW metric       median {info['metric_med']:.1f}, "
            f"p90 {info['metric_p90']:.1f}",
            f"  forecast        >= {100*info['est_yield']:.0f}% of blocks",
        ])
        self._show_info({**info, "path": p,
                         "file_hz": _freq_from_name(p),
                         "occ_bw": float("nan")}, extra)

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
        found = []
        try:
            from tools.scan_bearers import detect_carriers, identify
            x, sr = recv.load_wav_iq(path, secs=20)
            x = x - x.mean()
            for c in detect_carriers(x, sr):
                best, _, _m4 = identify(x, sr, c)
                if best:
                    found.append(f"    {c['centre']/1e3:+9.1f} kHz  "
                                 f"bw {c['bw']/1e3:5.1f} kHz  -> "
                                 f"{best[0]/1e3:.1f} kBd  {best[1]}")
        except Exception as exc:
            found.append(f"    (bearer scan failed: {exc})")
        if found:
            lines += ["", "  what this capture actually contains:"] + found
        lines += ["", "  This decoder handles F80T4.5X-8B (151.2 kBd) only.", ""]
        self.tab_log.insert("end", NL.join(lines) + NL)
        self.tab_log.see("end")
        self._show_info(
            {"path": path, "file_hz": _freq_from_name(path), "secs": 0.0,
             "raw_sr": 0, "centre": 0.0, "occ_bw": float("nan"), "rs": 0.0,
             "ppm": 0.0, "nframes": 0, "m4": float("nan"),
             "esn0": float("nan")},
            NL + NL + "NO USABLE CARRIER" + NL
            + NL.join(x.strip() and "  " + x.strip() or "" for x in found))

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
        for t in (self.tab_int, self.tab_files, self.tab_str, self.tab_bb,
                  self.tab_pkt, self.tab_log):
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

    def _show_info(self, i, extra=""):
        hz = i.get("file_hz")
        cen = (f"{(hz + i['centre'])/1e6:.6f} MHz"
               if hz else f"{i['centre']:+.1f} Hz from capture centre")
        txt = [
            "CARRIER",
            f"  file            {Path(i['path']).name[:34]}",
            f"  capture         {i['secs']:.1f} s @ {i['raw_sr']/1e3:.0f} kHz",
            f"  centre          {cen}",
            f"  carrier offset  {i['centre']:+.1f} Hz",
            f"  occupied BW     {i['occ_bw']/1e3:.1f} kHz (99% power)",
            "",
            "BEARER",
            "  type            F80T4.5X-8B (16-QAM)",
            f"  symbol rate     {i['rs']:.3f} Bd  ({i['ppm']:+.2f} ppm)",
            "  nominal         151200 Bd, 80 ms frame = 12096 sym",
            f"  frames in file  {i['nframes']}",
            "",
            "QUALITY",
            f"  M4/M2^2         {i['m4']:.3f}   "
            f"(16QAM 1.32 / QPSK 1.00 / noise 2.00)",
            f"  Es/N0 (est)     {i['esn0']:.1f} dB",
        ]
        # Unique-word EVM is the only figure here that predicts a residual
        # carrier offset. The UW correlation metric does not -- being
        # differential, it stays at 60-72 on captures that decode nothing.
        if np.isfinite(i.get("uw_evm", float("nan"))):
            txt.append(f"  UW EVM          {i['uw_evm']:.3f}   "
                       f"(~0.17 decodes, ~0.45 does not)")
        if i.get("jobs"):
            txt.append(f"  decode threads  {i['jobs']}")
        if i.get("cfo_applied"):
            txt.append(f"  carrier resid   {i['cfo_hz']:+.1f} Hz removed"
                       + ("  (past pilot-unwrap limit)"
                          if abs(i["cfo_hz"]) > 552.0 else ""))
        self.infobox.delete("1.0", "end")
        self.infobox.insert("end", "\n".join(txt) + extra)

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
        extra = [
            "", "DECODE",
            f"  blocks          {n}/{tot}  ({100*n/max(tot,1):.1f}%)",
            "  per block idx   " + " ".join(str(v) for v in byb),
            f"  median agree    {np.median(ags):.3f}" if ags else "",
            "  levels          " + ", ".join(
                f"{k}:{v}" for k, v in sorted(lv.items(), key=lambda kv: -kv[1])
            )[:60],
            f"  payload         {len(r.payload)} bytes",
        ]
        if r.payload:
            z = r.payload.count(0)/len(r.payload)
            extra.append(f"  zero bytes      {100*z:.1f}%")
        if r.bulletins:
            bb = r.bulletins[0][1]
            extra += [
                "", "BEARER CONTROL",
                f"  BulletinBoards  {len(r.bulletins)}"
                + (f" every {r.bb_period} frames" if r.bb_period else ""),
                f"  rnc-id / bct-id {bb.rnc_id} / {bb.bct_id}",
                f"  f-bearer        {bb.f_bearer}   net-ver {bb.net_ver}",
                f"  spot-beam-id    {bb.spot_beam_id}",
            ]
            plmn = _plmn(bb)
            if plmn:
                extra.append(f"  PLMN            {plmn}")
        self._show_info(r.info, "\n" + "\n".join(x for x in extra if x != ""))

        self._fill_interesting(r)
        self._fill_files(r)
        self._fill_strings(r)
        self._fill_bb(r)
        self._fill_pkts(r)
        self._log(f"done: {n} blocks, {len(r.payload)} bytes payload")

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
