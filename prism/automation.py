"""Switch lighting presets automatically by time of day and power source.

Rules are checked top to bottom; the first that matches picks the preset. The
daemon applies a preset only when the winning rule changes, so a colour you
pick by hand stays until the next time window or a plug/unplug.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .backend import OmenDevice, OmenError
from .presets import PresetStore, config_dir

POWER_CHOICES = ("any", "ac", "battery")
POWER_LABELS = {"any": "Any power", "ac": "Plugged in", "battery": "On battery"}
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
POWER_SUPPLY = Path("/sys/class/power_supply")


def parse_time(text: str) -> int:
    """'HH:MM' -> minutes after midnight."""
    m = _TIME.match(text.strip())
    if not m:
        raise ValueError(f"not a time: {text!r} (use HH:MM, e.g. 22:30)")
    return int(m.group(1)) * 60 + int(m.group(2))


@dataclass
class Rule:
    preset: str
    power: str = "any"          # any | ac | battery
    start: str | None = None    # "HH:MM"; both None means all day
    end: str | None = None

    def validate(self) -> None:
        if self.power not in POWER_CHOICES:
            raise ValueError(f"power must be one of {', '.join(POWER_CHOICES)}")
        if (self.start is None) != (self.end is None):
            raise ValueError("a time window needs both a start and an end")
        if self.start is not None:
            if parse_time(self.start) == parse_time(self.end):
                raise ValueError("start and end time are the same")

    def matches(self, minute: int, on_ac: bool) -> bool:
        if self.power == "ac" and not on_ac:
            return False
        if self.power == "battery" and on_ac:
            return False
        if self.start is None:
            return True
        a, b = parse_time(self.start), parse_time(self.end)
        # A window like 22:00-07:00 wraps past midnight.
        return a <= minute < b if a < b else minute >= a or minute < b

    def describe(self) -> str:
        parts = []
        if self.start is not None:
            parts.append(f"{self.start}–{self.end}")
        if self.power != "any":
            parts.append(POWER_LABELS[self.power])
        return " · ".join(parts) or "Always"


def pick(rules: list[Rule], minute: int, on_ac: bool) -> tuple[int, Rule] | None:
    for i, r in enumerate(rules):
        if r.matches(minute, on_ac):
            return i, r
    return None


def on_ac_power(root: Path = POWER_SUPPLY) -> bool:
    """True when a mains adapter is online. No adapter found counts as plugged in."""
    found = False
    for supply in root.glob("*"):
        try:
            if (supply / "type").read_text().strip() != "Mains":
                continue
            found = True
            if (supply / "online").read_text().strip() == "1":
                return True
        except OSError:
            continue
    return not found


EXAMPLE_RULES = [
    Rule("Battery Saver", power="battery"),
    Rule("Night", start="22:00", end="07:00"),
]


class AutomationStore:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "automation.json"

    def load(self) -> tuple[bool, list[Rule]]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return False, []
        rules = [Rule(**r) for r in raw.get("rules", [])]
        for r in rules:
            r.validate()
        return bool(raw.get("enabled", False)), rules

    def save(self, enabled: bool, rules: list[Rule]) -> None:
        for r in rules:
            r.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"enabled": enabled, "rules": [asdict(r) for r in rules]}, indent=2))
        tmp.replace(self.path)

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return 0.0


class Automation:
    """One step of the daemon; separated from the loop so it can be tested."""

    def __init__(self, dev: OmenDevice, store: AutomationStore, presets: PresetStore,
                 power=on_ac_power, log=print, music=None):
        self.dev, self.store, self.presets = dev, store, presets
        self.music = music  # MusicStore; None = the user's
        self.power, self.log = power, log
        self._active: tuple | None = None  # (rule index, rule) last applied
        self._mtime = -1.0

    def step(self, now: datetime) -> str | None:
        """Apply the matching preset if the winning rule changed. Returns its name."""
        mtime = self.store.mtime()
        if mtime != self._mtime:
            self._mtime = mtime
            self._active = None  # rules edited: re-evaluate from scratch
        try:
            enabled, rules = self.store.load()
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            self.log(f"automation.json is invalid: {e}")
            return None
        if not enabled:
            self._active = None
            return None
        hit = pick(rules, now.hour * 60 + now.minute, self.power())
        key = None if hit is None else (hit[0], hit[1].preset, hit[1].power, hit[1].start, hit[1].end)
        if key == self._active:
            return None
        self._active = key
        if hit is None:
            return None
        try:
            preset = self.presets.get(hit[1].preset)
            from .music import MusicStore
            music = self.music or MusicStore()
            ms = music.load()
            if ms.enabled:
                # Music lighting stays on and pulses the preset's colours instead.
                ms.colors = list(preset.zones)
                music.save(ms)
                if preset.brightness is not None:
                    self.dev.set_brightness(preset.brightness)
            else:
                preset.apply(self.dev)
        except (KeyError, OmenError, ValueError) as e:
            self.log(f"could not apply {hit[1].preset!r}: {e}")
            return None
        self.log(f"applied {hit[1].preset!r} ({hit[1].describe()})")
        return hit[1].preset


def run_daemon(interval: float = 5.0) -> int:
    import threading

    from .chain import ChainWatcher
    log = lambda m: print(f"prism: {m}", flush=True)  # noqa: E731
    # Keeps the Prism chain on the speakers (after login, headphones, restarts).
    threading.Thread(target=ChainWatcher(log=log).run, daemon=True, name="sound").start()
    from .music import MusicWatcher
    # Music lighting runs here too, so it keeps going with the app closed.
    threading.Thread(target=MusicWatcher(OmenDevice(), log=log, on_ac=on_ac_power).run,
                     daemon=True, name="music").start()
    from .power import PowerAuto
    power_auto = PowerAuto(log=log)
    from .battery import UsageRecorder
    # Estimated battery use per app (prism battery, Battery page).
    threading.Thread(target=UsageRecorder(log=log).run, daemon=True, name="battery").start()
    auto = Automation(OmenDevice(), AutomationStore(), PresetStore(), log=log)
    while True:
        try:
            power_auto.step()
        except Exception as e:  # a missing GNOME session must not stop the lighting rules
            log(f"power plans: {e}")
        auto.step(datetime.now())
        time.sleep(interval)
