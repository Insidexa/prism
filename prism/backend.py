"""Access to the omen-rgb-keyboard driver's sysfs controls.

Every read and write of the driver goes through OmenDevice, so the GUI, the
CLI and the GNOME extension (which calls the CLI) share one set of rules for
what a valid value is. Set PRISM_SYSFS to point at a fake tree for testing.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = "/sys/devices/platform/omen-rgb-keyboard"

ZONE_COUNT = 4
ANIMATIONS = (
    "static", "breathing", "rainbow", "wave", "pulse", "chase",
    "sparkle", "candle", "aurora", "disco", "gradient", "spectrum",
)
THERMAL_PROFILES = ("silent", "normal", "performance")
SPEED_MIN, SPEED_MAX = 1, 10
CURVE_MIN_POINTS, CURVE_MAX_POINTS = 2, 8

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


class OmenError(Exception):
    """A driver control could not be read or written."""


class NoDriver(OmenError):
    pass


class NoAccess(OmenError):
    pass


def normalize_color(value: str) -> str:
    """Return a colour as lowercase '#rrggbb', accepting 'RRGGBB' or '#RRGGBB'."""
    m = _HEX.match(value.strip())
    if not m:
        raise ValueError(f"not a hex colour: {value!r} (use RRGGBB)")
    return "#" + m.group(1).lower()


CurvePoint = tuple[int, int]  # (temperature °C, fan percent)


def parse_curve(text: str) -> list[CurvePoint] | None:
    """Parse 'T:P T:P ...'. Returns None when the curve follows another ('(cpu)')."""
    text = text.strip()
    if not text or text.startswith("("):
        return None
    points = []
    for pair in text.split():
        t, _, p = pair.partition(":")
        points.append((int(t), 100 if p == "max" else int(p)))
    return sorted(points)


def format_curve(points: list[CurvePoint]) -> str:
    validate_curve(points)
    return " ".join(f"{t}:{p}" for t, p in sorted(points))


def validate_curve(points: list[CurvePoint]) -> None:
    if not CURVE_MIN_POINTS <= len(points) <= CURVE_MAX_POINTS:
        raise ValueError(f"a fan curve needs {CURVE_MIN_POINTS}-{CURVE_MAX_POINTS} points")
    temps = [t for t, _ in points]
    if len(set(temps)) != len(temps):
        raise ValueError("two points share a temperature")
    for t, p in points:
        if not 0 <= t <= 110:
            raise ValueError(f"temperature {t} °C out of range 0-110")
        if not 0 <= p <= 100:
            raise ValueError(f"fan speed {p}% out of range 0-100")


def curve_is_risky(points: list[CurvePoint]) -> bool:
    """True when the fans would stay slow while the chip is hot (≥85 °C below 50%)."""
    pts = sorted(points)
    return any(t >= 85 and p < 50 for t, p in pts) or (pts[-1][0] < 85 and pts[-1][1] < 50)


@dataclass
class PowerLimits:
    pl1: int | None  # None means BIOS default
    pl2: int | None


class OmenDevice:
    def __init__(self, root: str | os.PathLike | None = None):
        self.root = Path(root or os.environ.get("PRISM_SYSFS") or DEFAULT_ROOT)

    # -- low level ---------------------------------------------------------
    def present(self) -> bool:
        return (self.root / "rgb_zones").is_dir()

    def writable(self) -> bool:
        return os.access(self.root / "rgb_zones" / "brightness", os.W_OK)

    def has_fan(self) -> bool:
        return (self.root / "fan").is_dir()

    def _path(self, name: str) -> Path:
        return self.root / name

    def read(self, name: str) -> str:
        try:
            return self._path(name).read_text().strip()
        except FileNotFoundError:
            if not self.present():
                raise NoDriver("the Omen keyboard driver is not loaded") from None
            raise OmenError(f"{name} is not available on this laptop") from None
        except OSError as e:
            raise OmenError(f"reading {name}: {e.strerror}") from None

    def write(self, name: str, value: str) -> None:
        path = self._path(name)
        try:
            # sysfs attributes must be written in place, never via temp+rename.
            with open(path, "w") as f:
                f.write(value + "\n")
        except PermissionError:
            raise NoAccess(
                "no permission to change Omen settings: run "
                "sudo system/install.sh once (from the Prism folder)"
            ) from None
        except FileNotFoundError:
            if not self.present():
                raise NoDriver("the Omen keyboard driver is not loaded") from None
            raise OmenError(f"{name} is not available on this laptop") from None
        except OSError as e:
            raise OmenError(f"the driver rejected {value!r} for {name}: {e.strerror}") from None

    # -- lighting ------------------------------------------------------------
    def zone(self, index: int) -> str:
        return normalize_color(self.read(f"rgb_zones/zone{index:02X}"))

    def zones(self) -> list[str]:
        return [self.zone(i) for i in range(ZONE_COUNT)]

    def set_zone(self, index: int, color: str) -> None:
        if not 0 <= index < ZONE_COUNT:
            raise ValueError(f"zone must be 0-{ZONE_COUNT - 1}")
        self.write(f"rgb_zones/zone{index:02X}", normalize_color(color)[1:])

    def set_zones(self, colors: list[str]) -> None:
        if len(colors) != ZONE_COUNT:
            raise ValueError(f"need {ZONE_COUNT} colours")
        if len(set(map(normalize_color, colors))) == 1:
            self.set_all(colors[0])
            return
        for i, c in enumerate(colors):
            self.set_zone(i, c)

    def set_all(self, color: str) -> None:
        self.write("rgb_zones/all", normalize_color(color)[1:])

    def brightness(self) -> int:
        return int(self.read("rgb_zones/brightness"))

    def set_brightness(self, value: int) -> None:
        self.write("rgb_zones/brightness", str(max(0, min(100, int(value)))))

    def animation(self) -> str:
        return self.read("rgb_zones/animation_mode")

    def set_animation(self, mode: str) -> None:
        if mode not in ANIMATIONS:
            raise ValueError(f"unknown effect {mode!r}; choose from {', '.join(ANIMATIONS)}")
        self.write("rgb_zones/animation_mode", mode)

    def speed(self) -> int:
        return int(self.read("rgb_zones/animation_speed"))

    def set_speed(self, value: int) -> None:
        self.write("rgb_zones/animation_speed", str(max(SPEED_MIN, min(SPEED_MAX, int(value)))))

    # -- performance -----------------------------------------------------------
    def thermal_profile(self) -> str:
        return self.read("fan/thermal_profile")

    def set_thermal_profile(self, name: str) -> None:
        if name not in THERMAL_PROFILES:
            raise ValueError(f"profile must be one of {', '.join(THERMAL_PROFILES)}")
        self.write("fan/thermal_profile", name)

    def fan_rpm(self) -> tuple[int, int]:
        return int(self.read("fan/cpu_fan_rpm")), int(self.read("fan/gpu_fan_rpm"))

    def max_rpm(self) -> tuple[int, int]:
        cpu, gpu = self.read("fan/max_rpm").split()[:2]
        return int(cpu), int(gpu)

    def max_fan(self) -> bool:
        return self.read("fan/max_fan") == "1"

    def set_max_fan(self, on: bool) -> None:
        self.write("fan/max_fan", "1" if on else "0")

    def fan_curve(self) -> list[CurvePoint]:
        return parse_curve(self.read("fan/fan_curve")) or []

    def gpu_fan_curve(self) -> list[CurvePoint] | None:
        """None while the GPU fan follows the CPU curve."""
        return parse_curve(self.read("fan/gpu_fan_curve"))

    def set_fan_curve(self, points: list[CurvePoint]) -> None:
        """Save a CPU curve; the driver switches custom control on."""
        self.write("fan/fan_curve", format_curve(points))

    def set_gpu_fan_curve(self, points: list[CurvePoint] | None) -> None:
        self.write("fan/gpu_fan_curve", "cpu" if points is None else format_curve(points))

    def curve_enabled(self) -> bool:
        return self.read("fan/fan_curve_enable") == "1"

    def set_curve_enabled(self, on: bool) -> None:
        self.write("fan/fan_curve_enable", "1" if on else "0")

    def power_limits(self) -> PowerLimits:
        text = self.read("fan/power_limits")
        parts = text.split()
        if len(parts) >= 2 and all(p.isdigit() for p in parts[:2]) and parts[:2] != ["0", "0"]:
            return PowerLimits(int(parts[0]), int(parts[1]))
        return PowerLimits(None, None)

    def set_power_limits(self, pl1: int | None, pl2: int | None) -> None:
        if pl1 is None or pl2 is None:
            self.write("fan/power_limits", "default")
            return
        if not (5 <= pl1 <= 200 and 5 <= pl2 <= 200):
            raise ValueError("power limits must be 5-200 W")
        if pl2 < pl1:
            raise ValueError("PL2 (boost) must be at least PL1 (sustained)")
        self.write("fan/power_limits", f"{pl1} {pl2}")
