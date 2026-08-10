"""Decode a live ZMQ IQ feed in rolling capture windows.

This is deliberately a rolling offline decoder, not a stateful sample-by-sample
receiver.  ZMQ fills a bounded IQ buffer, each window is decoded with the same
path used by tools/decode_wav.py, and overlapping windows are deduplicated by
absolute frame/block time.  That keeps the first live-input path small and
honest while the DSP remains file-oriented.
"""
from __future__ import annotations

import argparse
import tempfile
import threading
import time
import wave
from collections import deque
from pathlib import Path
import sys

import numpy as np

try:
    import zmq
except ImportError as exc:  # pragma: no cover - exercised by users
    raise SystemExit(
        "pyzmq is required for live ZMQ input; install it with "
        "`pip install pyzmq`."
    ) from exc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.decode_wav import (  # noqa: E402
    ACCEPT_AGREEMENT,
    NoCarrier,
    decode_capture,
    report_no_carrier,
    safe_stem,
)


FRAME_SECS = 0.080


class SampleBuffer:
    """Thread-safe complex IQ FIFO with optional oldest-sample dropping."""

    def __init__(self, max_samples: int):
        self.max_samples = int(max_samples)
        self.parts: deque[np.ndarray] = deque()
        self.samples = 0
        self.start_sample = 0
        self.dropped = 0
        self.closed = False
        self.cond = threading.Condition()

    def append(self, x: np.ndarray) -> None:
        if not len(x):
            return
        x = np.asarray(x, dtype=np.complex64)
        with self.cond:
            self.parts.append(x)
            self.samples += len(x)
            overflow = self.samples - self.max_samples
            if overflow > 0:
                self._discard_locked(overflow)
                self.dropped += overflow
            self.cond.notify_all()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def wait_for(self, n: int, timeout: float | None = None) -> bool:
        end = None if timeout is None else time.monotonic() + timeout
        with self.cond:
            while self.samples < n and not self.closed:
                remaining = None if end is None else end - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self.cond.wait(remaining)
            return self.samples >= n

    def peek(self, n: int) -> np.ndarray:
        with self.cond:
            if self.samples < n:
                raise ValueError("not enough samples buffered")
            return self._peek_locked(n)

    def peek_with_start(self, n: int) -> tuple[int, np.ndarray]:
        with self.cond:
            if self.samples < n:
                raise ValueError("not enough samples buffered")
            return self.start_sample, self._peek_locked(n)

    def _peek_locked(self, n: int) -> np.ndarray:
            out = np.empty(n, dtype=np.complex64)
            pos = 0
            for part in self.parts:
                take = min(len(part), n - pos)
                out[pos:pos + take] = part[:take]
                pos += take
                if pos == n:
                    break
            return out

    def discard(self, n: int) -> None:
        with self.cond:
            self._discard_locked(n)
            self.cond.notify_all()

    def _discard_locked(self, n: int) -> None:
        n = max(0, min(int(n), self.samples))
        while n and self.parts:
            first = self.parts[0]
            if len(first) <= n:
                n -= len(first)
                self.samples -= len(first)
                self.start_sample += len(first)
                self.parts.popleft()
            else:
                self.parts[0] = first[n:]
                self.samples -= n
                self.start_sample += n
                n = 0


def decode_iq_payload(payload: bytes, fmt: str) -> np.ndarray:
    """Convert one ZMQ payload to complex64 IQ."""
    if fmt == "cf32":
        n = len(payload) // np.dtype("<c8").itemsize
        return np.frombuffer(payload[:n * 8], dtype="<c8").astype(np.complex64)
    if fmt == "f32iq":
        d = np.frombuffer(payload, dtype="<f4")
        d = d[:len(d) // 2 * 2].reshape(-1, 2)
        return (d[:, 0] + 1j * d[:, 1]).astype(np.complex64)
    if fmt == "cs16":
        d = np.frombuffer(payload, dtype="<i2")
        d = d[:len(d) // 2 * 2].reshape(-1, 2).astype(np.float32)
        return ((d[:, 0] + 1j * d[:, 1]) / 32768.0).astype(np.complex64)
    if fmt == "cu8":
        d = np.frombuffer(payload, dtype=np.uint8)
        d = d[:len(d) // 2 * 2].reshape(-1, 2).astype(np.float32)
        return (((d[:, 0] - 127.5) + 1j * (d[:, 1] - 127.5)) / 127.5
                ).astype(np.complex64)
    raise ValueError(f"unsupported sample format {fmt!r}")


def write_iq_wav(path: Path, iq: np.ndarray, sample_rate: int) -> None:
    """Write complex IQ as 16-bit stereo PCM WAV for the existing decoder."""
    iq = np.asarray(iq, dtype=np.complex64)
    scale = float(np.max(np.abs([iq.real, iq.imag])))
    if not np.isfinite(scale) or scale < 1e-9:
        scale = 1.0
    stereo = np.empty(len(iq) * 2, dtype="<i2")
    stereo[0::2] = np.clip(iq.real / scale, -1.0, 1.0) * 32767
    stereo[1::2] = np.clip(iq.imag / scale, -1.0, 1.0) * 32767
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(int(sample_rate))
        f.writeframes(stereo.tobytes())


class ZmqIqReceiver(threading.Thread):
    """Background SUB receiver so decoding one chunk does not stop ingest."""

    def __init__(self, endpoint: str, topic: bytes, part: int, fmt: str,
                 buf: SampleBuffer, recv_hwm: int):
        super().__init__(daemon=True)
        self.endpoint = endpoint
        self.topic = topic
        self.part = part
        self.fmt = fmt
        self.buf = buf
        self.recv_hwm = recv_hwm
        self.messages = 0
        self.bytes = 0
        self.errors = 0
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # pragma: no cover - integration path
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.RCVHWM, self.recv_hwm)
        sock.setsockopt(zmq.SUBSCRIBE, self.topic)
        sock.connect(self.endpoint)
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self._stop_event.is_set():
                events = dict(poller.poll(250))
                if sock not in events:
                    continue
                msg = sock.recv_multipart()
                try:
                    payload = msg[self.part]
                    samples = decode_iq_payload(payload, self.fmt)
                except Exception:
                    self.errors += 1
                    continue
                self.messages += 1
                self.bytes += len(payload)
                self.buf.append(samples)
        finally:
            sock.close(0)
            self.buf.close()


def append_payload(path: Path, records) -> tuple[int, int]:
    """Append payload bytes from decoded records. Returns blocks, bytes."""
    bits = [r[4] for r in records]
    if not bits:
        return 0, 0
    payload = np.packbits(np.concatenate(bits)).tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as f:
        f.write(payload)
    return len(bits), len(payload)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Decode a live ZMQ IQ stream in overlapping windows.")
    ap.add_argument("endpoint", help="ZMQ SUB endpoint, e.g. tcp://127.0.0.1:5555")
    ap.add_argument("--sample-rate", type=int, required=True,
                    help="incoming IQ sample rate in Hz")
    ap.add_argument("--format", choices=("cf32", "f32iq", "cs16", "cu8"),
                    default="cf32",
                    help="ZMQ payload sample format (default: cf32)")
    ap.add_argument("--topic", default="",
                    help="SUB topic prefix; default subscribes to everything")
    ap.add_argument("--part", type=int, default=-1,
                    help="multipart message part containing IQ bytes")
    ap.add_argument("--chunk-secs", type=float, default=20.0,
                    help="seconds decoded per rolling window")
    ap.add_argument("--overlap-secs", type=float, default=2.0,
                    help="seconds retained between windows for boundary safety")
    ap.add_argument("--max-buffer-secs", type=float, default=90.0,
                    help="drop oldest IQ once this much is buffered")
    ap.add_argument("--max-chunks", type=int, default=0,
                    help="stop after N chunks; 0 runs until interrupted")
    ap.add_argument("--recv-hwm", type=int, default=200,
                    help="ZMQ receive high-water mark")
    ap.add_argument("--thr", type=float, default=ACCEPT_AGREEMENT)
    ap.add_argument("--level", default=None)
    ap.add_argument("--ntau", type=int, default=8)
    ap.add_argument("--jobs", type=int, default=None)
    ap.add_argument("--no-cfo", dest="cfo", action="store_false")
    ap.add_argument("--no-search-levels", dest="search_levels",
                    action="store_false")
    ap.add_argument("--out", default=None,
                    help="payload path; default work/zmq_<endpoint>_payload.bin")
    a = ap.parse_args()

    if a.overlap_secs < 0 or a.overlap_secs >= a.chunk_secs:
        ap.error("--overlap-secs must be >= 0 and smaller than --chunk-secs")
    if a.max_buffer_secs < a.chunk_secs:
        ap.error("--max-buffer-secs must be at least --chunk-secs")

    chunk_samples = int(round(a.chunk_secs * a.sample_rate))
    step_samples = int(round((a.chunk_secs - a.overlap_secs) * a.sample_rate))
    max_samples = int(round(a.max_buffer_secs * a.sample_rate))
    topic = a.topic.encode()
    stem = safe_stem(a.endpoint.replace("://", "_").replace("/", "_"))
    out = Path(a.out) if a.out else Path("work") / f"zmq_{stem}_payload.bin"
    if out.exists():
        out.unlink()

    buf = SampleBuffer(max_samples)
    rx = ZmqIqReceiver(a.endpoint, topic, a.part, a.format, buf, a.recv_hwm)
    rx.start()
    print(f"listening on {a.endpoint} ({a.format}, {a.sample_rate} Hz)")
    print(f"windows: {a.chunk_secs:.1f} s with {a.overlap_secs:.1f} s overlap")

    seen: set[tuple[int, int]] = set()
    chunks = 0
    total_blocks = 0
    total_bytes = 0

    try:
        while not a.max_chunks or chunks < a.max_chunks:
            if not buf.wait_for(chunk_samples, timeout=1.0):
                print("\rwaiting for IQ... "
                      f"{buf.samples / a.sample_rate:.1f} s buffered",
                      end="", flush=True)
                continue
            print()
            start_sample, iq = buf.peek_with_start(chunk_samples)
            start_secs = start_sample / a.sample_rate
            base_frame = int(round(start_secs / FRAME_SECS))

            with tempfile.TemporaryDirectory(prefix="decbgan-zmq-") as td:
                wav_path = Path(td) / f"chunk_{chunks:06d}.wav"
                write_iq_wav(wav_path, iq, a.sample_rate)

                def prog(frac, text, const=None):
                    print(f"\rchunk {chunks + 1}: [{100 * frac:5.1f}%] "
                          f"{text}          ", end="", flush=True)

                try:
                    recs, info, _ = decode_capture(
                        wav_path, thr=a.thr, level=a.level,
                        search_levels=a.search_levels, ntau=a.ntau,
                        progress=prog, cfo_enabled=a.cfo, jobs=a.jobs)
                except NoCarrier as exc:
                    print()
                    report_no_carrier(wav_path, exc.rows, a.chunk_secs)
                    recs = []
                    info = {}

            fresh = []
            for f, b, lv, ag, bits in recs:
                key = (base_frame + int(f), int(b))
                if key in seen:
                    continue
                seen.add(key)
                fresh.append((key[0], b, lv, ag, bits))
            blocks, nbytes = append_payload(out, fresh)
            total_blocks += blocks
            total_bytes += nbytes
            chunks += 1
            centre = info.get("centre", float("nan"))
            print(f"\nchunk {chunks}: {blocks} new block(s), "
                  f"{nbytes} byte(s), centre {centre:+.1f} Hz")
            print(f"totals: {total_blocks} blocks, {total_bytes} bytes -> {out}")
            if buf.dropped:
                late = buf.dropped / a.sample_rate
                print(f"warning: dropped {late:.1f} s of old IQ "
                      "because decoding fell behind")
                buf.dropped = 0
            buf.discard(step_samples)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        rx.stop()
        buf.close()
        rx.join(timeout=2.0)
        print(f"received {rx.messages} message(s), {rx.bytes} byte(s); "
              f"{rx.errors} malformed message(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
