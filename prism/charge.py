"""Battery charge limit: stop charging at, say, 80 %, like Windows' battery care.

The OMEN's firmware can hold the battery while the adapter powers the laptop
(HP WMI command 0x2B, mode 5; mode 0 lets it charge normally). The Omen driver
exposes that as battery/charge_control. prism daemon decides: plugged in and at
or above the limit, hold; once the charge falls `margin` points below the
limit, or the limit is off, charge normally again. Unplugging, a reboot or
stopping the daemon all end with normal charging.

Keeping a Li-ion battery around 80 % instead of 100 % slows its wear a lot when
the laptop is mostly plugged in.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .backend import OmenDevice, OmenError
from .presets import config_dir

ATTR = "battery/charge_control"
MODE_NORMAL = 0
MODE_HOLD = 5
STATE_HELD = 4          # first number of charge_control while the firmware holds
LIMITS = (60, 70, 80, 90)
BATTERY = Path("/sys/class/power_supply/BAT0/capacity")


@dataclass
class ChargeSettings:
    limit: int | None = None   # None: charge to 100 %
    margin: int = 5            # charge again below limit - margin


class ChargeStore:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "charge.json"

    def load(self) -> ChargeSettings:
        try:
            s = ChargeSettings(**json.loads(self.path.read_text()))
        except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError):
            return ChargeSettings()
        if s.limit is not None and not 50 <= s.limit <= 100:
            s.limit = None
        s.margin = min(max(int(s.margin), 1), 20)
        return s

    def save(self, s: ChargeSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(s), indent=2))
        tmp.replace(self.path)


def available(dev: OmenDevice | None = None) -> bool:
    dev = dev or OmenDevice()
    return dev._path(ATTR).exists()


def should_hold(limit: int | None, margin: int, percent: float, on_ac: bool, holding: bool) -> bool:
    if limit is None or limit >= 100 or not on_ac:
        return False
    if holding:
        return percent > limit - margin
    return percent >= limit


def battery_percent(path: Path = BATTERY) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


class ChargeLimiter:
    """prism daemon: keep the battery under the limit while plugged in."""

    INTERVAL = 30

    def __init__(self, dev: OmenDevice | None = None, store: ChargeStore | None = None,
                 on_ac=None, percent=battery_percent, log=print):
        from .automation import on_ac_power
        self.dev = dev or OmenDevice()
        self.store = store or ChargeStore()
        self.on_ac = on_ac or on_ac_power
        self.percent = percent
        self.log = log
        self.holding = False

    def _held_now(self) -> bool:
        try:
            return int(self.dev.read(ATTR).split()[0]) == STATE_HELD
        except (OmenError, ValueError, IndexError):
            return self.holding

    def _set(self, mode: int) -> bool:
        try:
            self.dev.write(ATTR, str(mode))
            return True
        except OmenError as e:
            self.log(f"charge limit: {e}")
            return False

    def step(self) -> bool | None:
        """Apply the limit once; returns whether charging is held (None: unknown)."""
        if not available(self.dev):
            return None
        s = self.store.load()
        pct = self.percent()
        if pct is None:
            return None
        ac = self.on_ac()
        want = should_hold(s.limit, s.margin, pct, ac, self.holding)
        if want and not self._held_now():
            if self._set(MODE_HOLD):
                if not self.holding:
                    self.log(f"charge limit: holding at {pct:.0f}% (limit {s.limit}%)")
                self.holding = True
        elif not want and self.holding:
            if self._set(MODE_NORMAL):
                self.log(f"charge limit: charging normally ({pct:.0f}%)")
                self.holding = False
        return self.holding

    def release(self) -> None:
        if self.holding:
            self._set(MODE_NORMAL)
            self.holding = False

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        # A hold left by a previous daemon is taken over, not trusted blindly.
        self.holding = self._held_now() if available(self.dev) else False
        try:
            while not stop.is_set():
                self.step()
                stop.wait(self.INTERVAL)
        finally:
            self.release()
