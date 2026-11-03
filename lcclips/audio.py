"""Audio capture: game/desktop audio (WASAPI loopback) + microphone, mixed into
one 48 kHz stereo float32 stream that is fed to FFmpeg.

Design notes
- Each device is read by its own thread into a small ring buffer.
- A mixer thread is clocked by the system clock (perf_counter), not by any
  sound card.  Every tick it emits exactly the number of samples that real
  time says it should, padding with silence if a source has nothing.
  This matters because WASAPI loopback delivers *no data at all* while the
  game is silent, and different devices drift slightly from each other.
  Clocking off the system clock keeps audio locked to ddagrab's video
  timestamps for hours.
"""
from __future__ import annotations

import collections
import ctypes
import logging
import threading
import time
from typing import Callable

import numpy as np

from .config import DEVICE_DEFAULT, DEVICE_NONE

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
BLOCK = 480  # 10 ms
MAX_BUFFERED = SAMPLE_RATE // 5  # 200 ms; older audio is dropped (drift guard)


def _sc():
    import soundcard  # imported lazily: Windows-only in practice

    return soundcard


def _com_init() -> None:
    # soundcard uses COM; worker threads must join the MTA themselves.
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    except Exception:
        pass


def list_output_devices() -> list[str]:
    try:
        return [s.name for s in _sc().all_speakers()]
    except Exception as e:  # pragma: no cover
        log.warning("Could not list output devices: %s", e)
        return []


def list_input_devices() -> list[str]:
    try:
        return [m.name for m in _sc().all_microphones(include_loopback=False)]
    except Exception as e:  # pragma: no cover
        log.warning("Could not list input devices: %s", e)
        return []


def _to_stereo(data: np.ndarray) -> np.ndarray:
    if data.ndim == 1:
        data = data[:, None]
    ch = data.shape[1]
    if ch == 1:
        return np.repeat(data, 2, axis=1)
    if ch == 2:
        return data
    # Surround loopback (5.1/7.1): FL, FR, C, LFE, ... -> fold centre into L/R.
    out = data[:, :2].copy()
    out += 0.707 * data[:, 2:3]
    return out


class Source(threading.Thread):
    """Continuously reads one device into a bounded buffer."""

    def __init__(self, kind: str, device_name: str, volume: float):
        super().__init__(daemon=True, name=f"audio-{kind}")
        self.kind = kind  # "loopback" | "mic"
        self.device_name = device_name
        self.volume = float(volume)
        self.error: str | None = None
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._buf: collections.deque[np.ndarray] = collections.deque()
        self._frames = 0

    # ---- device resolution -------------------------------------------------
    def _resolve(self):
        sc = _sc()
        if self.kind == "loopback":
            if self.device_name in ("", DEVICE_DEFAULT):
                spk = sc.default_speaker()
            else:
                spk = sc.get_speaker(self.device_name)
            return sc.get_microphone(id=str(spk.name), include_loopback=True), spk.name
        if self.device_name in ("", DEVICE_DEFAULT):
            mic = sc.default_microphone()
        else:
            mic = sc.get_microphone(self.device_name)
        return mic, mic.name

    def _default_changed(self, current: str) -> bool:
        if self.device_name not in ("", DEVICE_DEFAULT):
            return False
        try:
            sc = _sc()
            dev = sc.default_speaker() if self.kind == "loopback" else sc.default_microphone()
            return dev.name != current
        except Exception:
            return False

    # ---- thread ------------------------------------------------------------
    def run(self) -> None:
        _com_init()
        while not self._halt.is_set():
            try:
                dev, name = self._resolve()
                log.info("%s capture: %s", self.kind, name)
                self.error = None
                last_check = time.monotonic()
                with dev.recorder(samplerate=SAMPLE_RATE, blocksize=BLOCK) as rec:
                    while not self._halt.is_set():
                        self._push(_to_stereo(rec.record(numframes=BLOCK)))
                        # Follow Windows when the user switches default device.
                        if time.monotonic() - last_check > 3:
                            last_check = time.monotonic()
                            if self._default_changed(name):
                                log.info("%s default device changed, reopening", self.kind)
                                break
            except Exception as e:
                self.error = f"{self.kind}: {e}"
                log.warning("Audio source error (%s), retrying in 2s", self.error)
                self._halt.wait(2)

    def _push(self, data: np.ndarray) -> None:
        data = data.astype(np.float32, copy=False)
        with self._lock:
            self._buf.append(data)
            self._frames += len(data)
            while self._frames > MAX_BUFFERED and self._buf:
                self._frames -= len(self._buf.popleft())

    def take(self, n: int) -> np.ndarray:
        """Return exactly n frames (silence-padded if the device is behind)."""
        out = np.zeros((n, CHANNELS), dtype=np.float32)
        pos = 0
        with self._lock:
            while pos < n and self._buf:
                chunk = self._buf[0]
                k = min(n - pos, len(chunk))
                out[pos : pos + k] = chunk[:k]
                pos += k
                if k == len(chunk):
                    self._buf.popleft()
                else:
                    self._buf[0] = chunk[k:]
                self._frames -= k
        return out

    def stop(self) -> None:
        self._halt.set()


class AudioMixer:
    """Mixes all sources and pushes raw f32le stereo PCM into `sink`."""

    def __init__(self, sources: list[Source], sink: Callable[[bytes], None]):
        self.sources = sources
        self.sink = sink
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="audio-mixer")
        self.on_broken: Callable[[], None] | None = None

    def start(self) -> None:
        for s in self.sources:
            s.start()
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        for s in self.sources:
            s.stop()

    def errors(self) -> list[str]:
        return [s.error for s in self.sources if s.error]

    def _run(self) -> None:
        t0 = time.perf_counter()
        written = 0
        startup_until = t0 + 3.0
        while not self._halt.is_set():
            now = time.perf_counter()
            target = int((now - t0) * SAMPLE_RATE)
            n = target - written
            if n < BLOCK:
                time.sleep(0.004)
                continue
            # While FFmpeg is still opening its inputs our writes block.  Don't
            # burst the backlog afterwards (it would shift audio vs video);
            # skip it instead.  After startup we never skip, so short stalls
            # are caught up sample-accurately.
            if now < startup_until and n > SAMPLE_RATE // 10:
                written = target - BLOCK
                n = BLOCK
            mix = np.zeros((n, CHANNELS), dtype=np.float32)
            for s in self.sources:
                mix += s.take(n) * s.volume
            np.clip(mix, -1.0, 1.0, out=mix)
            try:
                self.sink(mix.tobytes())
            except (BrokenPipeError, OSError, ValueError):
                log.warning("Audio sink closed")
                if self.on_broken:
                    self.on_broken()
                return
            written += n


def build_sources(game_device: str, game_volume: float, mic_device: str, mic_volume: float) -> list[Source]:
    sources = []
    if game_device != DEVICE_NONE:
        sources.append(Source("loopback", game_device, game_volume))
    if mic_device != DEVICE_NONE:
        sources.append(Source("mic", mic_device, mic_volume))
    return sources
