"""Speaker sound: build and apply an EasyEffects preset for the built-in speakers.

Prism does not ship audio tuning itself. It starts from any EasyEffects output
preset installed on the machine (for example the B&O presets rebuilt from the
Windows driver, or Prism's own "Omen 16 Speakers"), optionally adds one of the
OMEN Audio Control equalizer curves and a bass amount, writes the result as
the "Prism Speakers" preset, and makes EasyEffects autoload it for the speaker
output only. Headphones, HDMI and Bluetooth are never touched.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .presets import config_dir

PRISM_PRESET = "Prism Speakers"
OFF_PRESET = "Prism Off"
OFF = "off"
EQ_Q = 1.41  # OMEN Audio Control's 10-band graphic EQ, one octave per band

FRIENDLY = {
    "OMEN16 BO Music (Windows level)": "B&O (Windows, full level)",
    "OMEN16 BO Music": "B&O (Windows)",
    "Omen 16 Speakers": "Prism tuning",
}
# Presets that are just another preset plus an OMEN equalizer curve. They are
# not offered separately: the Equalizer choice gives the same result.
ALIASES = {"OMEN16 BO Music + Club": ("OMEN16 BO Music", "Club")}


def ee_dir() -> Path:
    base = os.environ.get("PRISM_EE_DIR")
    if base:
        return Path(base)
    native = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "easyeffects"
    flatpak = Path.home() / ".var/app/com.github.wwmm.easyeffects/data/easyeffects"
    return native if native.exists() or not flatpak.exists() else flatpak


def sound_data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "prism-sound"


def load_eq_presets() -> dict[str, list[float]]:
    """OMEN equalizer curves (dB per band), if a sound pack is installed."""
    try:
        raw = json.loads((sound_data_dir() / "eq_presets.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    bands = raw.get("bands_hz", [])
    out = {}
    for name, gains in raw.get("presets_db", {}).items():
        if len(gains) == len(bands):
            out[name] = [float(g) for g in gains]
    return out


def eq_bands_hz() -> list[int]:
    try:
        return json.loads((sound_data_dir() / "eq_presets.json").read_text())["bands_hz"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


@dataclass
class SoundSettings:
    base: str = OFF            # an EasyEffects output preset name, or "off"
    eq: str | None = None      # an OMEN EQ preset name
    bass: float | None = None  # bass enhancer amount; None keeps the base preset's


class SoundStore:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "sound.json"

    def load(self) -> SoundSettings:
        try:
            return SoundSettings(**json.loads(self.path.read_text()))
        except (FileNotFoundError, TypeError, json.JSONDecodeError):
            return SoundSettings()

    def save(self, s: SoundSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(s), indent=2))
        tmp.replace(self.path)


# --------------------------------------------------------------------------- presets
def base_presets(root: Path | None = None) -> list[str]:
    out_dir = (root or ee_dir()) / "output"
    names = sorted(p.stem for p in out_dir.glob("*.json")) if out_dir.exists() else []
    names = [n for n in names if n not in (PRISM_PRESET, OFF_PRESET)
             and not (n in ALIASES and ALIASES[n][0] in names)]
    # Known speaker tunings first, in a sensible order.
    known = [n for n in FRIENDLY if n in names]
    return known + [n for n in names if n not in known]


def label(name: str) -> str:
    return FRIENDLY.get(name, name)


def _band(freq, gain):
    return {"frequency": float(freq), "gain": float(gain), "mode": "RLC (BT)", "mute": False,
            "q": EQ_Q, "slope": "x1", "solo": False, "type": "Bell", "width": 4.0}


def make_equalizer(gains: list[float], bands_hz: list[int]) -> dict:
    bands = {f"band{i}": _band(f, g) for i, (f, g) in enumerate(zip(bands_hz, gains))}
    boost = max([0.0] + gains)
    return {"balance": 0.0, "bypass": False,
            # Headroom for the boosts; the limiter at the end catches the rest.
            "input-gain": -round(min(6.0, boost * 0.5), 1),
            "mode": "IIR", "num-bands": len(bands), "output-gain": 0.0,
            "pitch-left": 0.0, "pitch-right": 0.0, "split-channels": False,
            "left": bands, "right": copy.deepcopy(bands)}


def _free_id(out: dict, kind: str) -> str:
    """Next unused instance id: EasyEffects only accepts numeric ones ("equalizer#1")."""
    used = {int(k.split("#")[1]) for k in out.get("plugins_order", [])
            if k.split("#")[0] == kind and k.split("#")[1].isdigit()}
    n = 0
    while n in used:
        n += 1
    return f"{kind}#{n}"


def compose(base_preset: dict, eq_gains: list[float] | None, bands_hz: list[int],
            bass: float | None) -> dict:
    """Return a copy of an EasyEffects output preset with the EQ and bass applied.

    Always start from the base preset as installed, never from a previous result.
    """
    preset = copy.deepcopy(base_preset)
    out = preset["output"]
    order: list[str] = out["plugins_order"]
    if eq_gains is not None and any(abs(g) > 0.01 for g in eq_gains):
        eq_key = _free_id(out, "equalizer")
        out[eq_key] = make_equalizer(eq_gains, bands_hz)
        # After bass enhancement, before dynamics: like OMEN Audio Control.
        for anchor in ("compressor", "multiband_compressor", "limiter"):
            idx = next((i for i, k in enumerate(order) if k.split("#")[0] == anchor), None)
            if idx is not None:
                order.insert(idx, eq_key)
                break
        else:
            order.append(eq_key)
    if bass is not None:
        key = next((k for k in order if k.startswith("bass_enhancer#")), None)
        if key is None and bass > 0:
            key = _free_id(out, "bass_enhancer")
            out[key] = {"amount": 0.0, "blend": 0.0, "bypass": False, "floor": 40.0,
                        "floor-active": True, "harmonics": 6.0, "input-gain": 0.0, "listen": False,
                        "output-gain": 0.0, "scope": 250.0}
            idx = next((i for i, k in enumerate(order) if k.startswith(("compressor", "limiter"))), len(order))
            order.insert(idx, key)
        if key is not None:
            out[key]["amount"] = float(bass)
            out[key]["bypass"] = bass <= 0
    return preset


def base_bass(preset: dict) -> float | None:
    out = preset.get("output", {})
    key = next((k for k in out.get("plugins_order", []) if k.startswith("bass_enhancer#")), None)
    return None if key is None else float(out[key].get("amount", 0.0))


# --------------------------------------------------------------------------- system
def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def easyeffects_available() -> bool:
    return shutil.which("easyeffects") is not None


def speaker_sink() -> tuple[str, str, str] | None:
    """(node name, description, active port) of the built-in speaker output."""
    try:
        sinks = json.loads(_run(["pactl", "-f", "json", "list", "sinks"]) or "[]")
    except json.JSONDecodeError:
        return None
    for s in sinks:
        name = s.get("name", "")
        port = s.get("active_port") or ""
        if name.startswith("easyeffects") or not name.startswith("alsa_output"):
            continue
        ports = {p.get("name"): p.get("description", "") for p in s.get("ports", [])}
        if "speaker" in name.lower() or "speaker" in port.lower() or \
                "speaker" in ports.get(port, "").lower():
            return name, s.get("description", name), port
    return None


def easyeffects_running() -> bool:
    return bool(_run(["pgrep", "-u", str(os.getuid()), "-x", "easyeffects"]).strip())


def start_easyeffects() -> bool:
    """Start the EasyEffects background service detached from us; wait until it is up."""
    import time
    _run(["systemd-run", "--user", "--collect", "--quiet", "--unit",
          f"easyeffects-service-{int(time.time())}", "easyeffects", "--service-mode"])
    for _ in range(25):
        if easyeffects_running():
            time.sleep(1.0)  # let it finish loading presets
            return True
        time.sleep(0.2)
    return False


def default_sink() -> str:
    return _run(["pactl", "get-default-sink"]).strip()


def other_processor_active(speaker: str) -> bool:
    """True when the default output is a virtual sink other than EasyEffects' own,
    e.g. a PipeWire filter-chain in front of the speakers."""
    d = default_sink()
    return bool(d) and d != speaker and not d.startswith(("alsa_", "bluez_", "easyeffects"))


class SoundEngine:
    def __init__(self, root: Path | None = None, store: SoundStore | None = None):
        self.root = root or ee_dir()
        self.store = store or SoundStore()

    def current(self) -> SoundSettings:
        """Saved settings, or (first run) whatever EasyEffects autoloads for the speakers."""
        if self.store.path.exists():
            return _unalias(self.store.load())
        sink = speaker_sink()
        if sink:
            for f in (self.root / "autoload" / "output").glob("*.json"):
                d = _json(f)
                if d.get("device") == sink[0] and d.get("preset-name") not in (None, PRISM_PRESET, OFF_PRESET):
                    return _unalias(SoundSettings(base=d["preset-name"]))
        return SoundSettings()

    def read_preset(self, name: str) -> dict:
        return json.loads((self.root / "output" / f"{name}.json").read_text())

    def _write_preset(self, name: str, preset: dict) -> None:
        d = self.root / "output"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{name}.tmp"
        tmp.write_text(json.dumps(preset, indent=4))
        tmp.replace(d / f"{name}.json")

    def build(self, s: SoundSettings) -> str:
        """Write the preset for these settings; returns the preset name to use."""
        if s.base == OFF:
            self._write_preset(OFF_PRESET, {"output": {"blocklist": [], "plugins_order": []}})
            return OFF_PRESET
        base = self.read_preset(s.base)
        gains = load_eq_presets().get(s.eq) if s.eq else None
        if s.eq and gains is None:
            raise ValueError(f"unknown equalizer preset {s.eq!r}")
        self._write_preset(PRISM_PRESET, compose(base, gains, eq_bands_hz(), s.bass))
        return PRISM_PRESET

    def set_autoload(self, preset: str, sink: tuple[str, str, str]) -> Path:
        node, desc, port = sink
        d = self.root / "autoload" / "output"
        d.mkdir(parents=True, exist_ok=True)
        existing = [p for p in d.glob("*.json") if _json(p).get("device") == node]
        path = existing[0] if existing else d / f"{node}:{port}.json"
        data = _json(path) if existing else {"device": node, "device-description": desc,
                                             "device-profile": port}
        data["preset-name"] = preset
        path.write_text(json.dumps(data, indent=4))
        return path

    def apply(self, s: SoundSettings) -> str:
        """Build, autoload for the speakers and, if they are playing now, load it.

        Returns a short human description of what happened.
        """
        if not easyeffects_available():
            raise ValueError("EasyEffects is not installed: sudo dnf install easyeffects")
        preset = self.build(s)
        sink = speaker_sink()
        if sink is None:
            raise ValueError("no built-in speaker output found")
        self.set_autoload(preset, sink)
        self.store.save(s)
        if not easyeffects_running() and other_processor_active(sink[0]):
            # Starting EasyEffects now would grab every stream, including the other
            # processor's own output, and break it. Keep the preset for later.
            return "saved; another sound processor is the output, EasyEffects left off"
        if not easyeffects_running() and not start_easyeffects():
            raise ValueError("EasyEffects is not running and could not be started")
        if default_sink() == sink[0]:
            _run(["easyeffects", "-l", preset])
            return "applied to the speakers"
        return "saved; it applies when the speakers are the output again"


def _unalias(s: SoundSettings) -> SoundSettings:
    if s.base in ALIASES:
        s.base, eq = ALIASES[s.base]
        s.eq = s.eq or eq
    return s


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
