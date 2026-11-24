"""Speaker sound as a PipeWire filter-chain: the "Prism chain".

EasyEffects is a separate app that takes over every audio stream; it can
stutter, and it crashes whenever PipeWire restarts. A filter-chain runs inside
PipeWire's own filter-chain service instead. Prism writes one chain for the
built-in speakers:

    Prism Bass -> OMEN equalizer -> crossfeed -> convolver (tuning) -> limiter

The tuning is not part of Prism. It comes from a chain profile: a small JSON
file in ~/.local/share/prism-sound/chains/ (see docs in the README):

    {"label": "My tuning",
     "impulse": "my-speakers.irs",          # optional; relative to the file
     "impulse_gain_db": 0,
     "crossfeed": 0.0,                       # share of the opposite channel
     "bass": {"h2_db": -20, "h3_db": -30, "h4_db": -28,
              "top_hz": 200, "bottom_hz": 12},  # optional: Prism Bass
     "ceiling_db": -1.0, "release_ms": 50}

Volume on the chain's sink is applied before the processing, so quiet
listening is never squashed by the limiter; the speaker hardware stays at 100 %.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .presets import config_dir
from .sound import (EQ_Q, _run, easyeffects_running, eq_bands_hz, load_eq_presets,
                    sound_data_dir, speaker_sink)

CHAIN = "chain"
NODE = "prism_speakers"
BASS_MIN, BASS_MAX = -12.0, 6.0   # dB relative to the profile; at BASS_MIN the bass is off
SAFE_START = 0.5                  # a switch never starts louder than this


# --------------------------------------------------------------------------- profiles
def chains_dir() -> Path:
    return sound_data_dir() / "chains"


def conf_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "pipewire" / "filter-chain.conf.d" / "prism-chain.conf"


def plugin_path() -> Path | None:
    """Prism's LADSPA library (Prism Bass + Prism Limiter), built by make install."""
    dirs = [os.environ.get("PRISM_LADSPA_DIR"), Path.home() / ".local/lib/ladspa",
            "/usr/local/lib64/ladspa", "/usr/local/lib/ladspa", "/usr/lib64/ladspa", "/usr/lib/ladspa"]
    for d in dirs:
        if d and (Path(d) / "prism_bass.so").exists():
            return Path(d) / "prism_bass.so"
    return None


@dataclass
class ChainProfile:
    name: str
    label: str
    impulse: Path | None = None
    impulse_gain_db: float = 0.0
    crossfeed: float = 0.0
    bass: dict | None = None
    ceiling_db: float = -1.0
    release_ms: float = 50.0
    description: str = ""


def load_chains(root: Path | None = None) -> dict[str, ChainProfile]:
    root = root or chains_dir()
    out: dict[str, ChainProfile] = {}
    for f in sorted(root.glob("*.json")) if root.exists() else []:
        try:
            d = json.loads(f.read_text())
            imp = d.get("impulse")
            path = None
            if imp:
                path = Path(imp).expanduser()
                if not path.is_absolute():
                    path = f.parent / path
            bass = d.get("bass")
            if bass is not None:
                bass = {k: float(bass[k]) for k in ("h2_db", "h3_db", "h4_db")} | {
                    "top_hz": float(bass.get("top_hz", 200)), "bottom_hz": float(bass.get("bottom_hz", 12))}
            out[f.stem] = ChainProfile(
                name=f.stem, label=str(d.get("label", f.stem)), impulse=path,
                impulse_gain_db=float(d.get("impulse_gain_db", 0.0)),
                crossfeed=min(0.5, max(0.0, float(d.get("crossfeed", 0.0)))), bass=bass,
                ceiling_db=float(d.get("ceiling_db", -1.0)), release_ms=float(d.get("release_ms", 50.0)),
                description=str(d.get("description", "")))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return out


def wav_channels(path: Path) -> int:
    """Channel count from a WAV/IRS header (float WAVs included); 1 if unreadable."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return 1
            while True:
                ck = f.read(8)
                if len(ck) < 8:
                    return 1
                cid, size = ck[:4], struct.unpack("<I", ck[4:])[0]
                if cid == b"fmt ":
                    return max(1, struct.unpack("<H", f.read(4)[2:4])[0])
                f.seek(size + (size & 1), 1)
    except OSError:
        return 1


# --------------------------------------------------------------------------- the graph
def headroom_db(eq_gains: list[float] | None) -> float:
    return min(6.0, max([0.0] + list(eq_gains)) * 0.5) if eq_gains else 0.0


def controls(p: ChainProfile, eq_gains: list[float] | None, bands: list[int],
             bass_db: float) -> dict[str, float]:
    """Everything that can change while the chain runs, by "node:port" name."""
    c: dict[str, float] = {}
    if p.bass is not None:
        c["bass:Amount (dB)"] = float(bass_db) if bass_db > BASS_MIN else -120.0
    gains = list(eq_gains) if eq_gains else [0.0] * len(bands)
    for ch in "LR":
        for i, g in enumerate(gains):
            c[f"eq{ch}{i}:Gain"] = float(g)
    h = 10 ** (-headroom_db(eq_gains) / 20)
    for ch in "LR":
        c[f"mix{ch}:Gain 1"] = round((1 - p.crossfeed) * h, 6)
        c[f"mix{ch}:Gain 2"] = round(p.crossfeed * h, 6)
    return c


def graph(p: ChainProfile, eq_gains: list[float] | None, bands: list[int], bass_db: float,
          plugin: Path) -> dict:
    if not bands:
        raise ValueError("no equalizer bands")
    ctl = controls(p, eq_gains, bands, bass_db)
    nodes: list[dict] = []
    links: list[dict] = []
    inputs: dict[str, str] = {}
    tail: dict[str, str | None] = {"L": None, "R": None}

    def attach(ch: str, port_in: str, port_out: str) -> None:
        if tail[ch] is None:
            inputs[ch] = port_in
        else:
            links.append({"output": tail[ch], "input": port_in})
        tail[ch] = port_out

    if p.bass is not None:
        b = p.bass
        nodes.append({"type": "ladspa", "name": "bass", "plugin": str(plugin), "label": "prism_bass",
                      "control": {"2nd harmonic (dB)": b["h2_db"], "3rd harmonic (dB)": b["h3_db"],
                                  "4th harmonic (dB)": b["h4_db"], "Bass band top (Hz)": b["top_hz"],
                                  "Bass band bottom (Hz)": b["bottom_hz"],
                                  "Amount (dB)": ctl["bass:Amount (dB)"], "Enable": 1}})
        for ch in "LR":
            attach(ch, f"bass:In {ch}", f"bass:Out {ch}")
    for ch in "LR":
        for i, f in enumerate(bands):
            n = f"eq{ch}{i}"
            nodes.append({"type": "builtin", "name": n, "label": "bq_peaking",
                          "control": {"Freq": float(f), "Q": EQ_Q, "Gain": ctl[f"{n}:Gain"]}})
            attach(ch, f"{n}:In", f"{n}:Out")
    # Crossfeed: each output mixes its own channel and a share of the other one.
    pre = dict(tail)
    for ch, other in (("L", "R"), ("R", "L")):
        nodes.append({"type": "builtin", "name": f"mix{ch}", "label": "mixer",
                      "control": {"Gain 1": ctl[f"mix{ch}:Gain 1"], "Gain 2": ctl[f"mix{ch}:Gain 2"]}})
        links.append({"output": pre[ch], "input": f"mix{ch}:In 1"})
        links.append({"output": pre[other], "input": f"mix{ch}:In 2"})
        tail[ch] = f"mix{ch}:Out"
    if p.impulse is not None:
        stereo = wav_channels(p.impulse) > 1
        for i, ch in enumerate("LR"):
            nodes.append({"type": "builtin", "name": f"conv{ch}", "label": "convolver",
                          "config": {"filename": str(p.impulse), "channel": i if stereo else 0,
                                     "gain": round(10 ** (p.impulse_gain_db / 20), 6)}})
            attach(ch, f"conv{ch}:In", f"conv{ch}:Out")
    nodes.append({"type": "ladspa", "name": "lim", "plugin": str(plugin), "label": "prism_limiter",
                  "control": {"Threshold (dBFS)": p.ceiling_db, "Release (ms)": p.release_ms}})
    for ch in "LR":
        links.append({"output": tail[ch], "input": f"lim:In {ch}"})
    return {"nodes": nodes, "links": links, "inputs": [inputs["L"], inputs["R"]],
            "outputs": ["lim:Out L", "lim:Out R"]}


def render(p: ChainProfile, speaker: str, eq_gains: list[float] | None, bands: list[int],
           bass_db: float, plugin: Path) -> str:
    desc = f"Speakers (Prism {p.label})"
    module = {"name": "libpipewire-module-filter-chain", "args": {
        "node.description": desc, "media.name": desc,
        "filter.graph": graph(p, eq_gains, bands, bass_db, plugin),
        "audio.channels": 2, "audio.position": ["FL", "FR"],
        "capture.props": {"node.name": NODE, "media.class": "Audio/Sink"},
        "playback.props": {
            "node.name": f"{NODE}.out", "node.passive": True, "target.object": speaker,
            # Only ever the speakers, and nobody (EasyEffects) may move it elsewhere.
            "node.dont-fallback": True, "node.dont-move": True, "stream.dont-remix": True}}}
    return ("# Written by Prism (Sound page, or: prism sound set). Changes here are overwritten.\n"
            + json.dumps({"context.modules": [module]}, indent=2) + "\n")


# --------------------------------------------------------------------------- PipeWire
def node_ids() -> dict[str, int]:
    try:
        objs = json.loads(_run(["pw-dump"], timeout=10) or "[]")
    except json.JSONDecodeError:
        return {}
    return {(o.get("info", {}).get("props") or {}).get("node.name"): o["id"]
            for o in objs if o.get("type") == "PipeWire:Interface:Node"}


def node_id(name: str) -> int | None:
    return node_ids().get(name)


def get_volume(nid: int) -> float | None:
    out = _run(["wpctl", "get-volume", str(nid)]).split()
    try:
        return float(out[1])
    except (IndexError, ValueError):
        return None


def set_volume(nid: int, v: float) -> None:
    _run(["wpctl", "set-volume", str(nid), f"{max(0.0, v):.3f}"])


def set_controls(nid: int, ctl: dict[str, float]) -> None:
    body = " ".join(f"{json.dumps(k)} {v:.6g}" for k, v in ctl.items())
    _run(["pw-cli", "set-param", str(nid), "Props", f"{{ params = [ {body} ] }}"])


def sink_names() -> set[str]:
    return {line.split("\t")[1] for line in _run(["pactl", "list", "short", "sinks"]).splitlines()
            if "\t" in line}


def move_streams(from_sinks: set[str], to: str) -> None:
    idx = {line.split("\t")[0]: line.split("\t")[1]
           for line in _run(["pactl", "list", "short", "sinks"]).splitlines() if "\t" in line}
    for line in _run(["pactl", "list", "short", "sink-inputs"]).splitlines():
        f = line.split("\t")
        if len(f) > 1 and idx.get(f[1]) in from_sinks:
            _run(["pactl", "move-sink-input", f[0], to])


def systemctl(*args: str) -> str:
    return _run(["systemctl", "--user", *args], timeout=20)


def stop_easyeffects() -> None:
    if not easyeffects_running():
        return
    # Only a running EasyEffects may be asked: with none, "easyeffects -q" starts one and waits.
    if not _run(["timeout", "10", "easyeffects", "-q"], timeout=12) and easyeffects_running():
        _run(["pkill", "-u", str(os.getuid()), "-x", "easyeffects"])
    for _ in range(30):
        if not easyeffects_running():
            return
        time.sleep(0.2)
    _run(["pkill", "-9", "-u", str(os.getuid()), "-x", "easyeffects"])


AUTOSTART_MARK = "# Prism: EasyEffects stays off while the Prism chain is the speaker sound"


def autostart_files() -> list[Path]:
    d = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart"
    return sorted(d.glob("*asyeffects*.desktop")) + sorted(d.glob("com.github.wwmm.easyeffects*.desktop"))


def hide_easyeffects_autostart() -> None:
    for f in dict.fromkeys(autostart_files()):
        text = f.read_text()
        if AUTOSTART_MARK not in text:
            f.write_text(text.rstrip("\n") + f"\n{AUTOSTART_MARK}\nHidden=true\n")


def restore_easyeffects_autostart() -> None:
    for f in dict.fromkeys(autostart_files()):
        text = f.read_text()
        if AUTOSTART_MARK in text:
            f.write_text(text.replace(f"\n{AUTOSTART_MARK}\nHidden=true\n", "\n"))


# --------------------------------------------------------------------------- engine
@dataclass
class ChainState:
    volume: float = 0.4               # listening volume on the chain's sink
    enabled_service: bool = False     # we enabled filter-chain.service (undo on switch back)
    extra: dict = field(default_factory=dict)


class ChainEngine:
    def __init__(self, state_path: Path | None = None):
        self.state_path = state_path or config_dir() / "chain-state.json"

    # state
    def load_state(self) -> ChainState:
        try:
            return ChainState(**json.loads(self.state_path.read_text()))
        except (FileNotFoundError, TypeError, json.JSONDecodeError):
            return ChainState()

    def save_state(self, st: ChainState) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st.__dict__, indent=2))
        tmp.replace(self.state_path)

    # building
    def profile(self, name: str | None) -> ChainProfile:
        chains = load_chains()
        if not chains:
            raise ValueError(f"no chain profiles installed (put one in {chains_dir()})")
        if name is None:
            return next(iter(chains.values()))
        if name not in chains:
            raise ValueError(f"unknown chain profile {name!r} (see: prism sound list)")
        return chains[name]

    def build(self, s) -> tuple[ChainProfile, str, dict[str, float], str]:
        p = self.profile(s.chain)
        plugin = plugin_path()
        if plugin is None:
            raise ValueError("Prism's audio plug-in is missing: run make install in the Prism folder")
        spk = speaker_sink()
        if spk is None:
            raise ValueError("no built-in speaker output found")
        gains = load_eq_presets().get(s.eq) if s.eq else None
        if s.eq and gains is None:
            raise ValueError(f"unknown equalizer preset {s.eq!r}")
        bands = eq_bands_hz()
        bass = min(BASS_MAX, max(BASS_MIN, float(s.chain_bass)))
        # The graph without the live controls: only a change here needs a new chain.
        shape = hashlib.sha256(render(p, spk[0], None, bands, 0.0, plugin).encode()).hexdigest()
        return p, render(p, spk[0], gains, bands, bass, plugin), controls(p, gains, bands, bass), shape

    # switching
    def apply(self, s, switching: bool) -> str:
        """Write the chain for these settings and make it the speaker sound.

        switching: coming from EasyEffects (hand the volume over, stop EasyEffects).
        """
        p, text, ctl, shape = self.build(s)
        path = conf_path()
        changed = not path.exists() or path.read_text() != text
        st = self.load_state()
        new_shape = st.extra.get("shape") != shape
        st.extra["shape"] = shape
        if switching:
            stop_easyeffects()
            hide_easyeffects_autostart()
            if systemctl("is-enabled", "filter-chain").strip() != "enabled":
                systemctl("enable", "filter-chain")
                st.enabled_service = True
            spk = speaker_sink()
            sid = node_id(spk[0]) if spk else None
            v = get_volume(sid) if sid is not None else None
            st.volume = min(SAFE_START, v if v is not None else st.volume)
        self.save_state(st)
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text)
            tmp.replace(path)
        nid = node_id(NODE)
        if new_shape or nid is None:
            # Profile or structure changed: a fresh chain. Live controls alone don't need one
            # (the file is still rewritten, so they stay after a restart).
            if nid is not None and not switching:
                v = get_volume(nid)
                if v is not None:
                    st.volume = v
                    self.save_state(st)
            self.restart(st.volume)
        else:
            set_controls(nid, ctl)
            if switching:
                self.take_over(st.volume)
        return f"{p.label} on the speakers"

    def take_over(self, volume: float) -> None:
        """Chain at the listening volume, speakers at 100 %, chain the default output."""
        nid, spk = node_id(NODE), speaker_sink()
        if nid is None or spk is None:
            return
        set_volume(nid, volume)
        sid = node_id(spk[0])
        if sid is not None:
            set_volume(sid, 1.0)
            _run(["wpctl", "set-mute", str(sid), "0"])
        _run(["wpctl", "set-default", str(nid)])
        move_streams({spk[0], "easyeffects_sink"}, NODE)

    def restart(self, volume: float) -> None:
        """Recreate the chain. While it is gone, streams fall back to the bare speakers,
        so first bring those down to the listening volume: no loud burst."""
        spk = speaker_sink()
        sid = node_id(spk[0]) if spk else None
        if sid is not None:
            set_volume(sid, volume)
        systemctl("restart", "filter-chain")
        for _ in range(40):
            if node_id(NODE) is not None:
                break
            time.sleep(0.2)
        else:
            raise ValueError("the Prism chain did not start: journalctl --user -u filter-chain")
        self.take_over(volume)

    def leave(self) -> float:
        """Back to the bare speakers (for EasyEffects): returns the listening volume."""
        st = self.load_state()
        nid = node_id(NODE)
        v = get_volume(nid) if nid is not None else None
        if v is not None:
            st.volume = v
        spk = speaker_sink()
        sid = node_id(spk[0]) if spk else None
        if sid is not None:
            set_volume(sid, min(st.volume, 1.0))
            _run(["wpctl", "set-mute", str(sid), "0"])
            _run(["wpctl", "set-default", str(sid)])
        conf_path().unlink(missing_ok=True)
        if st.enabled_service:
            systemctl("disable", "--now", "filter-chain")
            st.enabled_service = False
        elif systemctl("is-active", "filter-chain").strip() == "active":
            systemctl("restart", "filter-chain")  # someone else's chains keep running
        restore_easyeffects_autostart()
        self.save_state(st)
        return st.volume


# --------------------------------------------------------------------------- watcher
class ChainWatcher:
    """Keeps the Prism chain on the speakers (run by `prism daemon`).

    The chain disappears when its speaker output does (headphones plugged in: the
    sound card switches profile) or when filter-chain restarts. When the speakers
    are back and the chain is not, bring the speaker hardware down to the
    listening volume at once, then recreate the chain.
    """

    def __init__(self, engine: ChainEngine | None = None, log=print, settings=None):
        self.engine = engine or ChainEngine()
        self.log = log
        self.settings = settings  # callable returning SoundSettings (tests)
        self.last_try = 0.0
        self.last_volume: float | None = None

    def _settings(self):
        if self.settings:
            return self.settings()
        from .sound import SoundStore
        return SoundStore().load()

    def step(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        s = self._settings()
        if s.engine != CHAIN or not conf_path().exists():
            return None
        try:  # Prism is switching or rebuilding right now: leave it alone
            if time.time() - self.engine.state_path.stat().st_mtime < 8:
                return "busy"
        except OSError:
            pass
        ids = node_ids()
        spk = speaker_sink()
        if NODE in ids:
            v = get_volume(ids[NODE])
            if v is not None and v != self.last_volume:
                self.last_volume = v
                st = self.engine.load_state()
                st.volume = v
                self.engine.save_state(st)
            return None
        if spk is None:
            return None  # headphones in: nothing to do until the speakers return
        vol = self.engine.load_state().volume
        sid = ids.get(spk[0])
        if sid is not None:
            set_volume(sid, vol)
        if now - self.last_try < 10:
            return "waiting"
        self.last_try = now
        try:
            self.engine.restart(vol)
            self.log("Prism chain restored on the speakers")
            return "restored"
        except ValueError as e:
            self.log(str(e))
            return "failed"

    def run(self, stop: threading.Event | None = None) -> None:
        """Event-driven: react to sink changes at once, and check every 5 s anyway."""
        stop = stop or threading.Event()
        wake = threading.Event()

        def listen():
            while not stop.is_set():
                try:
                    proc = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, text=True)
                except OSError:
                    return
                for line in proc.stdout:
                    if "sink" in line or "server" in line or "card" in line:
                        wake.set()
                proc.wait()
                time.sleep(2)

        threading.Thread(target=listen, daemon=True).start()
        while not stop.is_set():
            try:
                self.step()
            except Exception as e:  # never let the watcher die
                self.log(f"sound watcher: {e}")
            wake.wait(5)
            if wake.is_set():
                time.sleep(0.05)  # let the burst of events settle
                wake.clear()
