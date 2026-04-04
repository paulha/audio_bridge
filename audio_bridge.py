#!/usr/bin/env python3
"""
audio_bridge.py  ─  Bidirectional radio audio bridge over UDP + Opus

Two computers share radio audio in real time:
  remote  (Win10, radio machine) ─ captures USB Codec Mic → Opus → UDP → local
                                    receives UDP → Opus → USB Codec Speaker
  local   (Win11, operator desk) ─ captures mic → Opus → UDP → remote
                                    receives UDP → Opus → speaker/headphones

Usage:
    python audio_bridge.py [config.toml]
    python audio_bridge.py --list-devices        # print audio device names/indices

Requires:
    pip install sounddevice numpy opuslib
    + opus.dll on Windows PATH (see INSTALL.md)
    Python ≥ 3.11 for built-in tomllib; else: pip install tomli
"""

import argparse
import logging
import queue
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

# ── TOML parser ───────────────────────────────────────────────────────────────
try:
    import tomllib                          # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib             # pip install tomli
    except ImportError:
        sys.exit(
            "ERROR: TOML library not found.\n"
            "  Python < 3.11: pip install tomli\n"
            "  Python >= 3.11 should have tomllib built-in."
        )

# ── sounddevice ───────────────────────────────────────────────────────────────
try:
    import sounddevice as sd
except ImportError:
    sys.exit("ERROR: sounddevice not installed.\n  pip install sounddevice")

# ── Opus codec ────────────────────────────────────────────────────────────────
# Strategy (tried in order):
#   1. ctypes  – find libopus DLL via PATH or PyOgg's package directory
#   2. opuslib – if already installed and its own DLL search succeeds
#   3. pyogg   – only if its OpusEncoder attribute is actually exposed (needs DLL)
#   4. pcm     – raw uncompressed fallback; fine on a local WiFi network
import ctypes as _ct
import ctypes.util as _ct_util


def _find_opus_lib():
    """
    Search for libopus and return a ctypes.CDLL, or None if not found.
    Checks system PATH first, then PyOgg's package directory (in case a future
    PyOgg wheel bundles the DLL).
    """
    import glob
    import importlib.util
    import os

    def _try(path):
        try:
            lib = _ct.CDLL(path)
            lib.opus_get_version_string   # verify it's actually libopus
            return lib
        except (OSError, AttributeError):
            return None

    # 1. Standard system / PATH locations
    for name in ("opus", "libopus", "libopus-0"):
        found = _ct_util.find_library(name)
        if found:
            lib = _try(found)
            if lib:
                return lib

    # 2. PyOgg's package directory
    spec = importlib.util.find_spec("pyogg")
    if spec and spec.origin:
        pyogg_dir = os.path.dirname(spec.origin)
        for dll in glob.glob(os.path.join(pyogg_dir, "**", "*.dll"), recursive=True):
            if "opus" in os.path.basename(dll).lower():
                lib = _try(dll)
                if lib:
                    return lib

    return None


_opus_lib = _find_opus_lib()

if _opus_lib:
    _CODEC_BACKEND = "ctypes"
else:
    try:
        import opuslib as _opuslib_mod
        _CODEC_BACKEND = "opuslib"
    except (ImportError, OSError):
        _opuslib_mod = None
        try:
            import pyogg as _pyogg_mod
            # PyOgg only exposes OpusEncoder when the native DLL loaded successfully
            if hasattr(_pyogg_mod, "OpusEncoder") and hasattr(_pyogg_mod, "OpusDecoder"):
                _CODEC_BACKEND = "pyogg"
            else:
                _CODEC_BACKEND = "pcm"
        except (ImportError, OSError):
            _pyogg_mod = None
            _CODEC_BACKEND = "pcm"


# ─────────────────────────────────────────────────────────────────────────────
# Protocol constants
# ─────────────────────────────────────────────────────────────────────────────

# 8-byte packet header: [seq u32 big-endian][timestamp_ms u16][payload_len u16]
_HDR    = struct.Struct("!IHH")
_HDR_SZ = _HDR.size          # 8

# Heartbeat packets: 4-byte magic, ignored by the audio receiver
_HEARTBEAT = b"\xDE\xAD\xBE\xEF"


# ─────────────────────────────────────────────────────────────────────────────
# Codec
# ─────────────────────────────────────────────────────────────────────────────

class Codec:
    """Abstract base – Opus or raw PCM fallback."""

    def __init__(self, cfg: dict):
        self.sr  = cfg["sample_rate"]           # Hz
        self.ch  = cfg["channels"]              # 1 = mono
        self.fps = int(self.sr * cfg["frame_ms"] / 1000)   # samples per frame
        self.br  = cfg.get("bitrate", 24_000)

    @property
    def frame_ms(self) -> int:
        return int(self.fps * 1000 / self.sr)

    def encode(self, pcm: bytes) -> bytes:
        raise NotImplementedError

    def decode(self, data: bytes) -> bytes:
        raise NotImplementedError

    def silence(self) -> bytes:
        """One frame of silent PCM."""
        return bytes(self.fps * self.ch * 2)    # 16-bit samples


class CtypesOpusCodec(Codec):
    """
    Opus via ctypes – calls libopus directly.
    Works with any opus.dll/libopus.so found on the system.
    """

    _APP   = 2051   # OPUS_APPLICATION_RESTRICTED_LOWDELAY
    _OK    = 0      # OPUS_OK
    _BRATE = 4002   # OPUS_SET_BITRATE_REQUEST
    _MXPKT = 4000   # max encoded bytes per Opus packet

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        lib = _opus_lib

        # ── encoder ───────────────────────────────────────────────────────────
        lib.opus_encoder_get_size.restype  = _ct.c_int
        lib.opus_encoder_get_size.argtypes = [_ct.c_int]
        lib.opus_encoder_init.restype  = _ct.c_int
        lib.opus_encoder_init.argtypes = [_ct.c_void_p, _ct.c_int, _ct.c_int, _ct.c_int]
        lib.opus_encode.restype  = _ct.c_int
        lib.opus_encode.argtypes = [_ct.c_void_p, _ct.c_char_p, _ct.c_int,
                                     _ct.c_char_p, _ct.c_int]

        enc_sz = lib.opus_encoder_get_size(self.ch)
        self._enc = _ct.create_string_buffer(enc_sz)
        err = lib.opus_encoder_init(self._enc, self.sr, self.ch, self._APP)
        if err != self._OK:
            raise RuntimeError(f"opus_encoder_init returned error {err}")

        # Set bitrate (opus_encoder_ctl is variadic – fix argtypes for this call)
        lib.opus_encoder_ctl.restype  = _ct.c_int
        lib.opus_encoder_ctl.argtypes = [_ct.c_void_p, _ct.c_int, _ct.c_int]
        lib.opus_encoder_ctl(self._enc, self._BRATE, self.br)

        # ── decoder ───────────────────────────────────────────────────────────
        lib.opus_decoder_get_size.restype  = _ct.c_int
        lib.opus_decoder_get_size.argtypes = [_ct.c_int]
        lib.opus_decoder_init.restype  = _ct.c_int
        lib.opus_decoder_init.argtypes = [_ct.c_void_p, _ct.c_int, _ct.c_int]
        lib.opus_decode.restype  = _ct.c_int
        lib.opus_decode.argtypes = [_ct.c_void_p, _ct.c_char_p, _ct.c_int,
                                     _ct.c_char_p, _ct.c_int, _ct.c_int]

        dec_sz = lib.opus_decoder_get_size(self.ch)
        self._dec = _ct.create_string_buffer(dec_sz)
        err = lib.opus_decoder_init(self._dec, self.sr, self.ch)
        if err != self._OK:
            raise RuntimeError(f"opus_decoder_init returned error {err}")

        self._enc_buf = _ct.create_string_buffer(self._MXPKT)
        self._dec_buf = _ct.create_string_buffer(self.fps * self.ch * 2)
        self._lib = lib

    def encode(self, pcm: bytes) -> bytes:
        n = self._lib.opus_encode(self._enc, pcm, self.fps,
                                   self._enc_buf, self._MXPKT)
        if n < 0:
            raise RuntimeError(f"opus_encode error {n}")
        return bytes(self._enc_buf[:n])

    def decode(self, data: bytes) -> bytes:
        n = self._lib.opus_decode(self._dec, data, len(data),
                                   self._dec_buf, self.fps, 0)
        if n < 0:
            raise RuntimeError(f"opus_decode error {n}")
        return bytes(self._dec_buf[:n * self.ch * 2])


class OpusLibCodec(Codec):
    """Opus via opuslib – requires opus.dll on PATH (see INSTALL.md)."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._enc = _opuslib_mod.Encoder(self.sr, self.ch,
                                         _opuslib_mod.APPLICATION_RESTRICTED_LOWDELAY)
        self._enc.bitrate = self.br
        self._dec = _opuslib_mod.Decoder(self.sr, self.ch)

    def encode(self, pcm: bytes) -> bytes:
        return self._enc.encode(pcm, self.fps)

    def decode(self, data: bytes) -> bytes:
        return self._dec.decode(data, self.fps)


class PyOggCodec(Codec):
    """Opus via PyOgg – used only when PyOgg successfully exposes OpusEncoder."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._enc = _pyogg_mod.OpusEncoder()
        self._enc.set_application("restricted_lowdelay")
        self._enc.set_sampling_frequency(self.sr)
        self._enc.set_channels(self.ch)
        self._dec = _pyogg_mod.OpusDecoder()
        self._dec.set_sampling_frequency(self.sr)
        self._dec.set_channels(self.ch)

    def encode(self, pcm: bytes) -> bytes:
        return bytes(self._enc.encode(pcm))

    def decode(self, data: bytes) -> bytes:
        return bytes(self._dec.decode(data, self.fps))


class PCMCodec(Codec):
    """No compression – passthrough.  Fine on a local WiFi network (~256 kbps)."""

    def encode(self, pcm: bytes) -> bytes:
        return pcm

    def decode(self, data: bytes) -> bytes:
        return data


def build_codec(cfg: dict) -> Codec:
    log = logging.getLogger("codec")
    if _CODEC_BACKEND != "pcm":
        log.info("Codec: Opus (%s)  sr=%d  ch=%d  frame=%dms  bitrate=%d",
                 _CODEC_BACKEND, cfg["sample_rate"], cfg["channels"],
                 cfg["frame_ms"], cfg.get("bitrate", 24_000))
        if _CODEC_BACKEND == "ctypes":  return CtypesOpusCodec(cfg)
        if _CODEC_BACKEND == "opuslib": return OpusLibCodec(cfg)
        if _CODEC_BACKEND == "pyogg":   return PyOggCodec(cfg)
    log.warning("Opus library not found – using raw PCM (~256 kbps, fine on local WiFi).")
    log.warning("To enable Opus: place opus.dll in the same folder as audio_bridge.py")
    log.warning("See INSTALL.md Step 2 for details.")
    return PCMCodec(cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Packet statistics
# ─────────────────────────────────────────────────────────────────────────────

class Stats:
    """Thread-safe packet loss / out-of-order counter."""

    def __init__(self, name: str):
        self.name = name
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self._rx = self._lost = self._ooo = 0
        self._last_seq = None
        self._t0 = time.monotonic()

    def record(self, seq: int):
        with self._lock:
            if self._last_seq is not None:
                diff = (seq - self._last_seq) & 0xFFFF_FFFF
                if diff == 1:
                    pass                            # perfect in-order
                elif diff < 0x8000_0000:            # forward jump → gap
                    self._lost += diff - 1
                else:                               # backward wrap / out-of-order
                    self._ooo += 1
                    return                          # don't advance last_seq
            self._last_seq = seq
            self._rx += 1

    def report(self) -> dict:
        with self._lock:
            elapsed = max(time.monotonic() - self._t0, 1e-9)
            total   = self._rx + self._lost
            d = dict(
                name  = self.name,
                rx    = self._rx,
                lost  = self._lost,
                ooo   = self._ooo,
                loss  = self._lost / total * 100 if total else 0.0,
                pps   = self._rx / elapsed,
            )
            self._reset()
            return d


# ─────────────────────────────────────────────────────────────────────────────
# Hostname resolution helper
# ─────────────────────────────────────────────────────────────────────────────

def resolve_host(name: str) -> str:
    """
    Resolve *name* to an IP address string.  If *name* is already a dotted-quad
    IP it is returned unchanged.  Exits with a clear error message if resolution
    fails so the user knows immediately rather than seeing a cryptic send error.
    """
    try:
        ip = socket.gethostbyname(name)
        if ip != name:
            logging.getLogger("network").info("Resolved %r → %s", name, ip)
        return ip
    except socket.gaierror as exc:
        logging.getLogger("network").error(
            "Cannot resolve host %r: %s\n"
            "  Check the name is correct and that both machines are on the same network.\n"
            "  You can use an IP address instead (e.g. 192.168.1.100).",
            name, exc
        )
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Audio device helper
# ─────────────────────────────────────────────────────────────────────────────

def find_device(name: str, kind: str):
    """
    Return sounddevice device index matching *name* for the given *kind*
    ('input' or 'output').  Returns None (system default) if name is 'default'
    or empty.  Matches case-insensitively on substring of device name.
    """
    if not name or name.strip().lower() == "default":
        return None
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d[f"max_{kind}_channels"] > 0:
            return i
    logging.getLogger("audio").warning(
        "Device %r not found for %s – using system default.", name, kind
    )
    return None


def list_devices():
    """Print a table of all audio devices for the user to identify device names."""
    try:
        di = sd.default.device          # (input_index, output_index)
        default_in_idx  = di[0] if di[0] is not None else -1
        default_out_idx = di[1] if di[1] is not None else -1
    except Exception:
        default_in_idx = default_out_idx = -1

    print("\n── Audio Devices ──────────────────────────────────────────────────")
    print(f"  {'idx':>3}  {'in':>3}  {'out':>3}  name")
    print(f"  {'───':>3}  {'──':>3}  {'───':>3}  ────")
    for i, d in enumerate(sd.query_devices()):
        tags = []
        if i == default_in_idx:  tags.append("◀ default-in")
        if i == default_out_idx: tags.append("▶ default-out")
        tag = "  " + "  ".join(tags) if tags else ""
        print(f"  {i:3d}  {d['max_input_channels']:3d}  {d['max_output_channels']:3d}"
              f"  {d['name']}{tag}")
    print("───────────────────────────────────────────────────────────────────\n")


# ─────────────────────────────────────────────────────────────────────────────
# Sender  (capture → encode → UDP)
# ─────────────────────────────────────────────────────────────────────────────

class Sender:
    """
    Opens an audio input stream on *device*, encodes each frame with *codec*,
    and sends it as a UDP datagram to *dest* = (ip, port).
    """

    def __init__(self, device: str, dest: tuple, codec: Codec):
        self.device = device
        self.dest   = dest
        self.codec  = codec

        self._capture_q = queue.Queue(maxsize=64)
        self._seq       = 0
        self._stop      = threading.Event()
        self._sock      = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._thread    = None
        self.log        = logging.getLogger(f"Sender→{dest[0]}:{dest[1]}")

    # ── sounddevice calls this from its real-time thread ──────────────────────

    def _capture_cb(self, indata, frames, time_info, status):
        if status:
            self.log.debug("capture status: %s", status)
        try:
            # indata shape: (frames, channels) – take channel 0 and copy
            self._capture_q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass    # drop frame under back-pressure

    # ── encoding + send thread ────────────────────────────────────────────────

    def _run(self):
        dev = find_device(self.device, "input")
        sr, ch, fps = self.codec.sr, self.codec.ch, self.codec.fps

        try:
            with sd.InputStream(
                device     = dev,
                samplerate = sr,
                channels   = ch,
                dtype      = "int16",
                blocksize  = fps,
                callback   = self._capture_cb,
            ):
                self.log.info("Capturing from %r → %s:%d", self.device, *self.dest)
                while not self._stop.is_set():
                    try:
                        arr = self._capture_q.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    pcm = arr.tobytes()
                    try:
                        payload = self.codec.encode(pcm)
                    except Exception as exc:
                        self.log.error("encode error: %s", exc)
                        continue

                    ts  = int(time.monotonic() * 1000) & 0xFFFF
                    hdr = _HDR.pack(self._seq & 0xFFFF_FFFF, ts, len(payload))
                    self._seq += 1

                    try:
                        self._sock.sendto(hdr + payload, self.dest)
                    except OSError as exc:
                        self.log.warning("send error: %s", exc)

        except Exception as exc:
            self.log.error("stream error: %s", exc)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sender")
        self._thread.start()

    def stop(self):
        self._stop.set()


# ─────────────────────────────────────────────────────────────────────────────
# Receiver  (UDP → decode → playback)
# ─────────────────────────────────────────────────────────────────────────────

class Receiver:
    """
    Listens for UDP datagrams on *port*, decodes each frame with *codec*,
    and plays it on *device*.  A jitter queue absorbs network timing variation.
    """

    def __init__(self, device: str, port: int, codec: Codec,
                 stats: Stats, jitter_ms: int):
        self.device   = device
        self.port     = port
        self.codec    = codec
        self.stats    = stats
        self.last_pkt = 0.0         # wall-clock of most recent received packet

        fps           = codec.fps
        frame_ms      = codec.frame_ms
        jitter_frames = max(2, jitter_ms // max(frame_ms, 1))

        # Queue holds decoded numpy arrays ready for playback
        # maxsize = jitter_frames * 4 gives room to absorb bursts
        self._audio_q  = queue.Queue(maxsize=jitter_frames * 4)
        self._silence  = np.zeros((fps, 1), dtype="int16")
        self._stop     = threading.Event()
        self.log       = logging.getLogger(f"Receiver:{port}")

    # ── sounddevice output callback (real-time thread) ────────────────────────

    def _playback_cb(self, outdata, frames, time_info, status):
        if status:
            self.log.debug("playback status: %s", status)
        try:
            outdata[:] = self._audio_q.get_nowait()
        except queue.Empty:
            outdata[:] = self._silence  # output silence on underrun

    # ── UDP receive thread ────────────────────────────────────────────────────

    def _recv_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)

        try:
            sock.bind(("", self.port))
        except OSError as exc:
            self.log.error("Cannot bind to port %d: %s", self.port, exc)
            return

        self.log.info("Listening on UDP port %d", self.port)

        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.log.error("recv error: %s", exc)
                break

            # Filter heartbeats
            if data == _HEARTBEAT:
                continue

            if len(data) < _HDR_SZ:
                continue

            seq, _ts, plen = _HDR.unpack(data[:_HDR_SZ])
            payload = data[_HDR_SZ: _HDR_SZ + plen]

            self.stats.record(seq)
            self.last_pkt = time.monotonic()

            try:
                pcm = self.codec.decode(payload)
            except Exception as exc:
                self.log.debug("decode error: %s", exc)
                continue

            arr = np.frombuffer(pcm, dtype="int16").reshape(-1, 1)

            # Drop oldest frame if queue is full (prefer recency over completeness)
            if self._audio_q.full():
                try:
                    self._audio_q.get_nowait()
                except queue.Empty:
                    pass
            try:
                self._audio_q.put_nowait(arr)
            except queue.Full:
                pass

        sock.close()

    # ── playback thread ───────────────────────────────────────────────────────

    def _play_loop(self):
        dev = find_device(self.device, "output")
        sr, fps = self.codec.sr, self.codec.fps

        try:
            with sd.OutputStream(
                device     = dev,
                samplerate = sr,
                channels   = 1,
                dtype      = "int16",
                blocksize  = fps,
                callback   = self._playback_cb,
            ):
                self.log.info("Playing to %r", self.device)
                while not self._stop.is_set():
                    time.sleep(0.1)
        except Exception as exc:
            self.log.error("playback error: %s", exc)

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._recv_loop, daemon=True, name="recv").start()
        threading.Thread(target=self._play_loop, daemon=True, name="play").start()

    def stop(self):
        self._stop.set()


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat sender
# ─────────────────────────────────────────────────────────────────────────────

class Heartbeat:
    """
    Sends _HEARTBEAT datagrams to *dest* every *interval* seconds.
    This lets the remote receiver detect "sender is alive" even during
    radio silence (no audio to transmit).
    """

    def __init__(self, dest: tuple, interval: float):
        self._dest  = dest
        self._iv    = interval
        self._stop  = threading.Event()
        self._sock  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._loop, daemon=True, name="heartbeat").start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._sock.sendto(_HEARTBEAT, self._dest)
            except OSError:
                pass
            time.sleep(self._iv)

    def stop(self):
        self._stop.set()


# ─────────────────────────────────────────────────────────────────────────────
# Connection monitor + stats printer
# ─────────────────────────────────────────────────────────────────────────────

class Monitor:
    """
    Periodically prints packet-loss statistics and warns when a stream
    goes silent for longer than *timeout* seconds.
    """

    def __init__(self, stats_list: list, receivers: list,
                 interval: float, timeout: float):
        self._stats   = stats_list
        self._recvs   = receivers
        self._iv      = interval
        self._timeout = timeout
        self._stop    = threading.Event()
        self._was_ok: dict = {}

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._loop, daemon=True, name="monitor").start()

    def _loop(self):
        log = logging.getLogger("monitor")
        while not self._stop.is_set():
            time.sleep(self._iv)
            print("\n── Stream Statistics ───────────────────────────────────────────")
            for s in self._stats:
                r = s.report()
                print(f"  {r['name']:<22}  "
                      f"rx={r['rx']:5d}  lost={r['lost']:4d}  "
                      f"({r['loss']:5.1f}%)  ooo={r['ooo']:3d}  "
                      f"{r['pps']:.1f} pkt/s")
            for rv in self._recvs:
                age    = time.monotonic() - rv.last_pkt if rv.last_pkt else float("inf")
                ok     = age < self._timeout
                label  = f"{age:.1f}s ago" if rv.last_pkt else "no packets yet"
                status = "OK" if ok else "SILENT"
                # _was_ok is None until we have seen at least one stats cycle;
                # this suppresses a false "SILENT" warning on the very first tick.
                was = self._was_ok.get(id(rv))
                if was is not None:
                    if ok and not was:
                        log.info("%s: stream restored", rv.log.name)
                    elif not ok and was:
                        log.warning("%s: no packets for %.0fs", rv.log.name, age)
                self._was_ok[id(rv)] = ok
                print(f"  {rv.log.name:<22}  last packet: {label:<18}  [{status}]")
            print("────────────────────────────────────────────────────────────────")

    def stop(self):
        self._stop.set()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Audio Bridge – bidirectional radio audio over UDP + Opus"
    )
    parser.add_argument(
        "config", nargs="?", default="config.toml",
        help="Path to TOML config file (default: config.toml)"
    )
    parser.add_argument(
        "--list-devices", action="store_true",
        help="Print available audio devices and exit"
    )
    parser.add_argument(
        "--remote", action="store_true",
        help="Run as remote machine (not local)"
    )
    parser.add_argument(
        "--local", action="store_true",
        help="Run as local machine (not remote)"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
        datefmt = "%H:%M:%S",
    )
    log = logging.getLogger("bridge")

    if args.list_devices:
        list_devices()
        return

    # ── Load config ───────────────────────────────────────────────────────────

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        log.error("Config file not found: %s", cfg_path)
        log.error("Copy config.toml to the same folder and edit it.")
        sys.exit(1)

    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)

    if args.remote:
        role = "remote"
    elif args.local:
        role = "local"
    else:
        role = cfg["mode"]["role"].strip().lower()

    if role not in ("remote", "local"):
        log.error("mode.role must be 'remote' or 'local', got %r", role)
        sys.exit(1)

    net        = cfg["network"]
    remote_ip  = resolve_host(net["remote_ip"])
    local_ip   = resolve_host(net["local_ip"])
    rx_port    = int(net["rx_port"])    # remote sends here → local receives
    tx_port    = int(net["tx_port"])    # local sends here  → remote receives

    jitter_ms  = int(cfg.get("buffer",    {}).get("jitter_ms",    40))
    stats_iv   = float(cfg.get("stats",   {}).get("interval_s",   10))
    conn_tout  = float(cfg.get("reconnect", {}).get("timeout_s",  5.0))
    hb_iv      = float(cfg.get("reconnect", {}).get("heartbeat_s", 1.0))

    log.info("Role: %s  |  Codec backend: %s", role, _CODEC_BACKEND)

    # ── Build codec ───────────────────────────────────────────────────────────

    codec = build_codec(cfg["codec"])

    # ── Print devices so user can verify names ────────────────────────────────

    list_devices()

    # ── Wire up sender / receiver based on role ───────────────────────────────

    if role == "remote":
        # Remote machine (radio side, Win10)
        #   capture: USB Codec Mic (radio RX output)  → encode → UDP → local:rx_port
        #   playback: receive from local:tx_port → decode → USB Codec Speaker (TX input)
        audio = cfg["audio"]["remote"]

        sender = Sender(
            device = audio["capture_device"],
            dest   = (local_ip, rx_port),
            codec  = codec,
        )
        rx_stats = Stats("Radio-RX → local")
        receiver = Receiver(
            device    = audio["playback_device"],
            port      = tx_port,
            codec     = codec,
            stats     = rx_stats,
            jitter_ms = jitter_ms,
        )
        # Heartbeats go to local's rx_port so the local receiver knows we're alive
        heartbeat = Heartbeat(dest=(local_ip, rx_port), interval=hb_iv)

    else:
        # Local machine (operator desk, Win11)
        #   capture: local mic → encode → UDP → remote:tx_port
        #   playback: receive from remote:rx_port → decode → local speaker
        audio = cfg["audio"]["local"]

        sender = Sender(
            device = audio["capture_device"],
            dest   = (remote_ip, tx_port),
            codec  = codec,
        )
        rx_stats = Stats("Radio-RX → speaker")
        receiver = Receiver(
            device    = audio["playback_device"],
            port      = rx_port,
            codec     = codec,
            stats     = rx_stats,
            jitter_ms = jitter_ms,
        )
        # Heartbeats go to remote's tx_port so remote receiver knows we're alive
        heartbeat = Heartbeat(dest=(remote_ip, tx_port), interval=hb_iv)

    monitor = Monitor(
        stats_list = [rx_stats],
        receivers  = [receiver],
        interval   = stats_iv,
        timeout    = conn_tout,
    )

    # ── Run ───────────────────────────────────────────────────────────────────

    try:
        receiver.start()
        sender.start()
        heartbeat.start()
        monitor.start()
        log.info("Bridge running – Ctrl+C to stop.")
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        log.info("Shutting down…")

    finally:
        heartbeat.stop()
        sender.stop()
        receiver.stop()
        monitor.stop()
        log.info("Stopped.")


if __name__ == "__main__":
    main()
