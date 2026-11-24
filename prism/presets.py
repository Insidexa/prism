"""Named lighting presets stored in ~/.config/prism/presets.json."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .backend import ANIMATIONS, ZONE_COUNT, OmenDevice, normalize_color


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "prism"


@dataclass
class Preset:
    name: str
    zones: list[str]
    # None keeps whatever brightness the keyboard has; a number sets it.
    # Only presets whose point is the level (Night, Battery Saver) carry one.
    brightness: int | None = None
    animation: str = "static"
    speed: int = 3

    def validate(self) -> None:
        if len(self.zones) != ZONE_COUNT:
            raise ValueError(f"preset {self.name!r} needs {ZONE_COUNT} colours")
        self.zones = [normalize_color(c) for c in self.zones]
        if self.animation not in ANIMATIONS:
            raise ValueError(f"preset {self.name!r} has unknown effect {self.animation!r}")
        if self.brightness is not None and not 0 <= int(self.brightness) <= 100:
            raise ValueError(f"preset {self.name!r} brightness must be 0-100")

    def apply(self, dev: OmenDevice) -> None:
        # Colours first: effects such as breathing animate the stored colours.
        dev.set_animation("static")
        dev.set_zones(self.zones)
        if self.brightness is not None:
            dev.set_brightness(self.brightness)
        dev.set_speed(self.speed)
        if self.animation != "static":
            dev.set_animation(self.animation)

    @classmethod
    def capture(cls, name: str, dev: OmenDevice, with_brightness: bool = False) -> "Preset":
        return cls(name, dev.zones(), dev.brightness() if with_brightness else None,
                   dev.animation(), dev.speed())


BUILTIN = [
    Preset("Ember", ["#ff3300", "#ff1a00", "#ff1a00", "#ff3300"]),
    Preset("Ice", ["#00c8ff", "#3a7bff", "#3a7bff", "#00c8ff"]),
    Preset("Toxic", ["#39ff14", "#00ff88", "#00ff88", "#39ff14"]),
    Preset("Synthwave", ["#ff00c8", "#a000ff", "#3c00ff", "#00c8ff"]),
    Preset("Focus", ["#ffffff", "#ffffff", "#ffffff", "#ffffff"]),
    Preset("Spectrum", ["#ff0000", "#00ff00", "#0000ff", "#ff00ff"], animation="spectrum", speed=2),
    Preset("Fireplace", ["#ff6a00", "#ff4500", "#ff4500", "#ff6a00"], animation="candle", speed=4),
    Preset("Northern Lights", ["#00ff99", "#00ccff", "#7a00ff", "#00ff99"], animation="aurora", speed=3),
    # Made for automation: dim, static, easy on the eyes and the battery.
    Preset("Night", ["#ff5a00", "#ff5a00", "#ff5a00", "#ff8c1a"], brightness=25),
    Preset("Battery Saver", ["#3a7bff", "#3a7bff", "#3a7bff", "#00c8ff"], brightness=20),
]


class PresetStore:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "presets.json"

    def user_presets(self) -> list[Preset]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        out = []
        for item in raw.get("presets", []):
            p = Preset(**item)
            p.validate()
            out.append(p)
        return out

    def all(self) -> list[Preset]:
        user = self.user_presets()
        names = {p.name for p in user}
        return [p for p in BUILTIN if p.name not in names] + user

    def get(self, name: str) -> Preset:
        for p in self.all():
            if p.name.lower() == name.lower():
                return p
        raise KeyError(f"no preset named {name!r}")

    def save(self, preset: Preset) -> None:
        preset.validate()
        presets = [p for p in self.user_presets() if p.name != preset.name] + [preset]
        self._write(presets)

    def delete(self, name: str) -> None:
        self._write([p for p in self.user_presets() if p.name != name])

    def is_builtin(self, name: str) -> bool:
        return any(p.name == name for p in BUILTIN) and not any(
            p.name == name for p in self.user_presets())

    def _write(self, presets: list[Preset]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"presets": [asdict(p) for p in presets]}, indent=2))
        tmp.replace(self.path)
