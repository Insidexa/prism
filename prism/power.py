"""Power plans: battery saver, balanced, performance, as one choice.

A plan sets, in one go:
  - GNOME's Power Mode (power-profiles-daemon or tuned-ppd), which on this
    laptop also moves the OMEN thermal profile (fans, CPU/GPU power);
  - the built-in screen's refresh rate (60 Hz saves about a watt on battery,
    144 Hz plugged in), through GNOME's display settings, temporarily;
  - music lighting off on battery saver (it keeps the keyboard firmware busy).
The discrete GPU sleeps by itself when nothing uses it (see prism battery);
battery saver reports the apps that keep it awake.

Automation rules can carry a plan (e.g. battery saver when unplugged).
"""
from __future__ import annotations

from dataclasses import dataclass

PPD_BUS = "org.freedesktop.UPower.PowerProfiles"
PPD_PATH = "/org/freedesktop/UPower/PowerProfiles"


@dataclass(frozen=True)
class Plan:
    label: str
    power_mode: str            # power-saver | balanced | performance
    refresh: str               # "min" | "max": lowest or highest rate of the current resolution
    music: bool | None = None  # False: music lighting off; None: leave it


PLANS = {
    "battery": Plan("Battery saver", "power-saver", "min", music=False),
    "balanced": Plan("Balanced", "balanced", "max"),
    "performance": Plan("Performance", "performance", "max"),
}


# --------------------------------------------------------------------------- GNOME Power Mode
def _bus(system: bool):
    from gi.repository import Gio
    return Gio.bus_get_sync(Gio.BusType.SYSTEM if system else Gio.BusType.SESSION, None)


def power_mode() -> str | None:
    from gi.repository import GLib
    try:
        r = _bus(True).call_sync(PPD_BUS, PPD_PATH, "org.freedesktop.DBus.Properties", "Get",
                                 GLib.Variant("(ss)", (PPD_BUS, "ActiveProfile")), None, 0, 3000, None)
        return r.unpack()[0]
    except Exception:
        return None


def set_power_mode(name: str) -> bool:
    from gi.repository import GLib
    try:
        _bus(True).call_sync(PPD_BUS, PPD_PATH, "org.freedesktop.DBus.Properties", "Set",
                             GLib.Variant("(ssv)", (PPD_BUS, "ActiveProfile", GLib.Variant("s", name))),
                             None, 0, 3000, None)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- refresh rate
MUTTER = ("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig", "org.gnome.Mutter.DisplayConfig")
VERIFY, TEMPORARY = 0, 1


def _state():
    bus = _bus(False)
    r = bus.call_sync(*MUTTER, "GetCurrentState", None, None, 0, 5000, None)
    return bus, r.unpack()


def builtin_rates() -> tuple[float, list[float]] | None:
    """(current Hz, available Hz at the current resolution) of the laptop screen."""
    try:
        _bus_, (_serial, monitors, _logical, _props) = _state()
    except Exception:
        return None
    for (conn, *_ids), modes, props in monitors:
        if not props.get("is-builtin", conn.startswith("eDP")):
            continue
        cur = next((m for m in modes if m[6].get("is-current")), None)
        if cur is None:
            return None
        rates = sorted({round(m[3], 3) for m in modes if (m[1], m[2]) == (cur[1], cur[2])})
        return round(cur[3], 3), rates
    return None


def set_builtin_refresh(which: str, method: int = TEMPORARY) -> float | None:
    """Switch the laptop screen to its lowest ('min') or highest ('max') rate at the
    current resolution, every other setting kept. Returns the new rate, or None.
    Temporary: GNOME's saved display settings come back at the next login."""
    from gi.repository import GLib
    try:
        bus, (serial, monitors, logical, _props) = _state()
    except Exception:
        return None
    current = {}
    target = None
    for (conn, vendor, product, mserial), modes, props in monitors:
        cur = next((m for m in modes if m[6].get("is-current")), None)
        if cur is None:
            continue
        current[conn] = cur[0]
        if props.get("is-builtin", conn.startswith("eDP")):
            same = [m for m in modes if (m[1], m[2]) == (cur[1], cur[2])]
            pick = (min if which == "min" else max)(same, key=lambda m: m[3])
            current[conn] = pick[0]
            target = (pick[0] != cur[0], round(pick[3], 3))
    if target is None:
        return None
    if not target[0]:
        return target[1]  # already there
    lms = []
    for x, y, scale, transform, primary, mons, _p in logical:
        lms.append((x, y, scale, transform, primary,
                    [(m[0], current[m[0]], {}) for m in mons if m[0] in current]))
    try:
        bus.call_sync(*MUTTER, "ApplyMonitorsConfig",
                      GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, method, lms, {})),
                      None, 0, 5000, None)
    except Exception:
        return None
    return target[1]


# --------------------------------------------------------------------------- plans
def apply_plan(name: str, log=print) -> list[str]:
    """Apply a plan; returns what it did, for logs and the app."""
    plan = PLANS[name]
    done = []
    if set_power_mode(plan.power_mode):
        done.append(f"power mode {plan.power_mode}")
    hz = set_builtin_refresh(plan.refresh)
    if hz:
        done.append(f"screen {hz:g} Hz")
    if plan.music is False:
        from .music import MusicStore, set_enabled
        if MusicStore().load().enabled:
            set_enabled(False)
            done.append("music lighting off")
    if name == "battery":
        from .battery import dgpu_state, dgpu_users
        if dgpu_state() == "active":
            users = dgpu_users()
            done.append("discrete GPU awake" + (f" (used by {', '.join(users)})" if users else ""))
    return done


# --------------------------------------------------------------------------- automatic
import json  # noqa: E402
from pathlib import Path  # noqa: E402

KEEP = "keep"


class PlanStore:
    """Which plan to use on battery and plugged in (power.json)."""

    DEFAULT = {"battery": "battery", "ac": "balanced"}

    def __init__(self, path: Path | None = None):
        from .presets import config_dir
        self.path = path or config_dir() / "power.json"

    def load(self) -> dict[str, str]:
        try:
            d = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return dict(self.DEFAULT)
        out = dict(self.DEFAULT)
        for k in out:
            if d.get(k) in PLANS or d.get(k) == KEEP:
                out[k] = d[k]
        return out

    def save(self, d: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        tmp.replace(self.path)


class PowerAuto:
    """Run by prism daemon: apply the plan for the power source when it changes."""

    def __init__(self, store: PlanStore | None = None, power=None, apply=None, log=print):
        from .automation import on_ac_power
        self.store = store or PlanStore()
        self.power = power or on_ac_power
        self.apply = apply or apply_plan
        self.log = log
        self.last: bool | None = None

    def step(self) -> str | None:
        ac = self.power()
        if ac == self.last:
            return None
        first = self.last is None
        self.last = ac
        name = self.store.load()["ac" if ac else "battery"]
        if name == KEEP or (first and ac):
            return None  # don't override what was chosen by hand at login when plugged in
        done = self.apply(name)
        self.log(f"{PLANS[name].label} ({'plugged in' if ac else 'on battery'}): " + (", ".join(done) or "no change"))
        return name
