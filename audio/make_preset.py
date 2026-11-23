"""Build the 'Omen 16 Speakers' EasyEffects 8 preset.

Voicing for the two small B&O-branded drivers in the HP Omen 16-c0xxx, imitating
what the Windows DTS chain does: keep energy out of the range the drivers cannot
play, add psychoacoustic bass instead, tame chassis resonance and harshness,
then compress and limit so it plays louder without distortion.
"""
import json
import sys
from pathlib import Path


def band(freq, gain, q, type_="Bell", slope="x1"):
    return {"frequency": freq, "gain": gain, "mode": "RLC (BT)", "mute": False, "q": q,
            "slope": slope, "solo": False, "type": type_, "width": 4.0}


BANDS = [
    band(140.0, 0.0, 0.71, "Hi-pass", "x2"),  # nothing below the drivers' range
    band(180.0, 4.0, 1.2),                     # punch just above the cut-off
    band(320.0, -1.5, 1.4),                    # boxiness
    band(550.0, -2.5, 2.0),                    # chassis resonance
    band(1200.0, 1.0, 1.0),                    # vocal presence
    band(3200.0, -1.5, 2.0),                   # harshness (measured bright highs)
    band(6500.0, -1.0, 3.0),                   # sibilance
    band(10000.0, 2.0, 0.71, "Hi-shelf"),      # air
]

eq = {"balance": 0.0, "bypass": False, "input-gain": -7.0, "mode": "IIR",
      "num-bands": len(BANDS), "output-gain": 0.0, "pitch-left": 0.0, "pitch-right": 0.0,
      "split-channels": False,
      "left": {f"band{i}": b for i, b in enumerate(BANDS)},
      "right": {f"band{i}": b for i, b in enumerate(BANDS)}}

preset = {"output": {
    "blocklist": [],
    "plugins_order": ["equalizer#0", "bass_enhancer#0", "compressor#0", "loudness#0", "limiter#0"],
    "equalizer#0": eq,
    # Harmonics of the bass the high-pass removed, so the ear still hears it.
    "bass_enhancer#0": {"amount": 4.0, "blend": 0.0, "bypass": False, "floor": 40.0,
                        "floor-active": True, "harmonics": 6.0, "input-gain": 0.0,
                        "listen": False, "output-gain": 0.0, "scope": 190.0},
    # Gentle downward compression with makeup: fuller and louder at the same peak.
    "compressor#0": {"attack": 15.0, "boost-amount": 6.0, "boost-threshold": -72.0, "bypass": False,
                     "dry": -100.0, "hpf-frequency": 10.0, "hpf-mode": "off", "input-gain": 0.0,
                     "knee": -6.0, "lpf-frequency": 20000.0, "lpf-mode": "off", "makeup": 5.0,
                     "mode": "Downward", "output-gain": 0.0, "ratio": 2.5, "release": 150.0,
                     "release-threshold": -100.0,
                     "sidechain": {"lookahead": 0.0, "mode": "RMS", "preamp": 0.0, "reactivity": 10.0,
                                   "source": "Middle", "stereo-split-source": "Left/Right",
                                   "type": "Feed-forward"},
                     "stereo-split": False, "threshold": -22.0, "wet": 0.0},
    "loudness#0": {"bypass": False, "clipping": False, "clipping-range": 6.0, "fft": "4096",
                   "input-gain": 0.0, "output-gain": 0.0, "std": "ISO226-2003", "volume": 0.0},
    # Always last: protects the small drivers and prevents clipping.
    "limiter#0": {"alr": False, "alr-attack": 5.0, "alr-knee": 0.0, "alr-release": 50.0, "attack": 5.0,
                  "bypass": False, "dithering": "None", "external-sidechain": False, "gain-boost": False,
                  "input-gain": 0.0, "lookahead": 5.0, "mode": "Herm Thin", "oversampling": "None",
                  "output-gain": 0.0, "release": 20.0, "sidechain-preamp": 0.0, "stereo-link": 100.0,
                  "threshold": -1.0},
}}

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".local/share/easyeffects/output/Omen 16 Speakers.json"
out.write_text(json.dumps(preset, indent=4))
print(out)
