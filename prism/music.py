"""Music lighting: the keyboard zones follow the sound that is playing.

Like OMEN Light Studio's audio mode. Prism listens to the default output (the
Prism chain's sink when it is in use, so it sees the music, not the speaker
tuning) with pw-record, splits it into four frequency bands and drives one
zone per band, left to right like the keyboard:

    Left (zone 2): bass   WASD (3): low mids   Middle (1): mids   Right (0): highs

Each zone keeps its own colour (the preset that was showing, or colours you
choose) and its brightness follows its band. When nothing plays, the colours
settle back to full and the keyboard looks like the static preset.

Frames go to the driver's rgb_zones/frame file: all four zones in one firmware
call. Runs inside `prism daemon`; switched with `prism music on|off`.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .backend import ZONE_COUNT, OmenDevice, OmenError, normalize_color
from .presets import config_dir

RATE = 24000
BLOCK = 1024              # analysis window (43 ms), hopped by HOP
HOP = 512                 # 21 ms per analysis step
BANDS = {2: (30, 160), 3: (160, 600), 1: (600, 2500), 0: (2500, 10000)}  # zone: Hz
FLOOR = 0.10              # dimmest a zone goes while music plays
SILENCE_DB = -60.0        # below this for SILENCE_S, show the plain colours
SILENCE_S = 1.5
FPS = 30
FPS_BATTERY = 15


@dataclass
class MusicSettings:
    enabled: bool = False
    colors: list[str] = field(default_factory=list)  # zone 0..3; empty = the preset's
    sensitivity: float = 1.0                          # 0.5 (calm) .. 2.0 (lively)


class MusicStore:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "music.json"

    def load(self) -> MusicSettings:
        try:
            d = json.loads(self.path.read_text())
            s = MusicSettings(**d)
            s.colors = [normalize_color(c) for c in s.colors][:ZONE_COUNT]
            return s
        except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError):
            return MusicSettings()

    def save(self, s: MusicSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(s), indent=2))
        tmp.replace(self.path)

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0


def available() -> str | None:
    """None if music lighting can run here, else why not."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        return "needs numpy: sudo dnf install python3-numpy"
    if not _which("pw-record"):
        return "needs PipeWire's pw-record"
    return None


def _which(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def scale(color: str, k: float) -> str:
    c = normalize_color(color)
    r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
    k = min(1.0, max(0.0, k))
    return f"{round(r * k):02x}{round(g * k):02x}{round(b * k):02x}"


class Analyzer:
    """Band levels 0..1 from audio blocks, self-adjusting to the volume."""

    def __init__(self, sensitivity: float = 1.0):
        import numpy as np
        self.np = np
        self.window = np.hanning(BLOCK).astype(np.float32)
        freqs = np.fft.rfftfreq(BLOCK, 1 / RATE)
        self.bins = {z: (freqs >= lo) & (freqs < hi) for z, (lo, hi) in BANDS.items()}
        self.peak = {z: 1e-6 for z in BANDS}   # slowly falling running maximum per band
        self.level = {z: 0.0 for z in BANDS}
        self.sensitivity = sensitivity
        self.quiet_since: float | None = None

    def step(self, block, now: float) -> dict[int, float] | None:
        """Levels per zone, or None when it has been silent for a while."""
        np = self.np
        rms_db = 10 * np.log10(float(np.mean(block * block)) + 1e-12)
        if rms_db < SILENCE_DB:
            if self.quiet_since is None:
                self.quiet_since = now
            if now - self.quiet_since > SILENCE_S:
                return None
        else:
            self.quiet_since = None
        spec = np.abs(np.fft.rfft(block * self.window)) ** 2
        energy = {z: float(np.sqrt(np.mean(spec[sel]))) if sel.any() else 0.0
                  for z, sel in self.bins.items()}
        for z, e in energy.items():
            # The running maximum falls ~6 dB per 3 s, so quiet songs still move.
            self.peak[z] = max(e, self.peak[z] * 0.9965, 1e-6)
        # Each band against its own peak, so the highs move as much as the bass, but
        # never against less than 30 dB under the loudest band: spill-over stays dark.
        floor = 0.03 * max(self.peak.values())
        out = {}
        for z, e in energy.items():
            target = min(1.0, e / max(self.peak[z], floor)) ** (1.6 / self.sensitivity)
            # Fast attack, ~150 ms release: pulses, not flicker.
            a = 0.6 if target > self.level[z] else 0.13
            self.level[z] += a * (target - self.level[z])
            out[z] = self.level[z]
        return out


def frame_colors(base: list[str], levels: dict[int, float] | None) -> list[str]:
    if levels is None:
        return [normalize_color(c)[1:] for c in base]
    return [scale(base[z], FLOOR + (1 - FLOOR) * levels.get(z, 0.0)) for z in range(ZONE_COUNT)]


class MusicLights:
    """The loop run by the daemon thread while music lighting is on."""

    def __init__(self, dev: OmenDevice, store: MusicStore | None = None, log=print, on_ac=None):
        self.dev = dev
        self.store = store or MusicStore()
        self.log = log
        self.on_ac = on_ac

    def base_colors(self, s: MusicSettings) -> list[str]:
        return s.colors if len(s.colors) == ZONE_COUNT else self.dev.base_zones()

    def run(self, stop: threading.Event) -> None:
        import numpy as np
        s = self.store.load()
        base = self.base_colors(s)
        if not s.colors:  # remember them, so they come back when music lighting stops
            s.colors = base
            self.store.save(s)
        an = Analyzer(s.sensitivity)
        proc = subprocess.Popen(
            ["pw-record", "--raw", "-P", "stream.capture.sink=true", "-P", "node.name=prism-music-lights",
             "-P", "media.role=Production", "--rate", str(RATE), "--channels", "1",
             "--format", "f32", "--latency", "20ms", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        buf = np.zeros(BLOCK, dtype=np.float32)
        last_write, last_sent, mtime = 0.0, None, self.store.mtime()
        ac = True
        try:
            while not stop.is_set():
                raw = proc.stdout.read(HOP * 4)
                if not raw or len(raw) < HOP * 4:
                    break
                buf = np.concatenate([buf[HOP:], np.frombuffer(raw, dtype=np.float32)])
                now = time.monotonic()
                levels = an.step(buf, now)
                if self.store.mtime() != mtime:  # colours or sensitivity changed
                    mtime = self.store.mtime()
                    s = self.store.load()
                    if not s.enabled:
                        break
                    base = self.base_colors(s)
                    an.sensitivity = s.sensitivity
                    if self.on_ac:
                        ac = self.on_ac()
                fps = FPS if ac else FPS_BATTERY
                if now - last_write < 1 / fps:
                    continue
                cols = frame_colors(base, levels)
                if cols == last_sent:
                    continue
                try:
                    self.dev.set_frame(cols)
                except OmenError as e:
                    self.log(f"music lighting: {e}")
                    break
                last_write, last_sent = now, cols
        finally:
            proc.terminate()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()
            try:  # back to the plain colours, saved as usual
                self.dev.set_zones(base)
            except OmenError:
                pass


class MusicWatcher:
    """Starts and stops MusicLights as music.json says (prism daemon)."""

    def __init__(self, dev: OmenDevice, store: MusicStore | None = None, log=print, on_ac=None):
        self.dev, self.store, self.log, self.on_ac = dev, store or MusicStore(), log, on_ac

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            if self.store.load().enabled and available() is None:
                halt = threading.Event()
                lights = MusicLights(self.dev, self.store, self.log, self.on_ac)
                t = threading.Thread(target=lights.run, args=(halt,), daemon=True, name="music")
                t.start()
                while t.is_alive() and not stop.is_set() and self.store.load().enabled:
                    t.join(1.0)
                halt.set()
                t.join(5)
                if self.store.load().enabled and not stop.is_set():
                    time.sleep(3)  # capture ended (PipeWire restart?): try again shortly
                continue
            stop.wait(1.0)


def set_enabled(on: bool, store: MusicStore | None = None, colors: list[str] | None = None) -> None:
    store = store or MusicStore()
    s = store.load()
    s.enabled = on
    if colors is not None:
        s.colors = [normalize_color(c) for c in colors]
    elif on:
        s.colors = []  # take whatever is showing when it starts
    store.save(s)


def stop_for_manual_change() -> None:
    """Any other lighting change from Prism ends music lighting."""
    store = MusicStore()
    if store.load().enabled:
        set_enabled(False, store)
        # Wait for the daemon to restore the colours, so the new change wins.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _music_capture_running():
            time.sleep(0.1)


def _music_capture_running() -> bool:
    r = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", "node.name=prism-music-lights"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())
