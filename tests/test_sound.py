import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prism import sound
from prism.sound import (OFF, OFF_PRESET, PRISM_PRESET, SoundEngine, SoundSettings, SoundStore,
                         base_bass, base_presets, compose)

SPEAKER = ("alsa_output.pci-0000_08_00.6.HiFi__Speaker__sink", "Speaker", "[Out] Speaker")
HEADPHONES = "alsa_output.pci-0000_08_00.6.HiFi__Headphones__sink"

BO = {"output": {
    "blocklist": [],
    "plugins_order": ["stereo_tools#0", "convolver#0", "bass_enhancer#0", "compressor#0", "limiter#0"],
    "stereo_tools#0": {"stereo-base": -0.38}, "convolver#0": {"kernel-name": "OMEN16 BO Music"},
    "bass_enhancer#0": {"amount": 2.0, "bypass": False}, "compressor#0": {}, "limiter#0": {},
}}
EQ = {"bands_hz": [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000],
      "presets_db": {"Club": [0, 0, 2, 5, 5, 5, 2, 0, 0, 0], "Flat": [0] * 10, "Broken": [1, 2]}}


class ComposeTests(unittest.TestCase):
    def test_eq_goes_before_dynamics_and_base_is_untouched(self):
        out = compose(BO, EQ["presets_db"]["Club"], EQ["bands_hz"], None)["output"]
        order = out["plugins_order"]
        self.assertEqual(order.index("equalizer#0"), order.index("compressor#0") - 1)
        eq = out["equalizer#0"]
        self.assertEqual(eq["num-bands"], 10)
        self.assertEqual(eq["left"]["band3"]["gain"], 5.0)
        self.assertEqual(eq["input-gain"], -2.5)          # headroom for +5 dB
        self.assertNotIn("equalizer#0", BO["output"])  # deep copy

    def test_ids_are_numeric_and_do_not_clash(self):
        # EasyEffects crashes on non-numeric instance ids such as "equalizer#prism".
        club = compose(BO, EQ["presets_db"]["Club"], EQ["bands_hz"], None)
        again = compose(club, EQ["presets_db"]["Club"], EQ["bands_hz"], None)["output"]
        eqs = [k for k in again["plugins_order"] if k.startswith("equalizer#")]
        self.assertEqual(eqs, ["equalizer#0", "equalizer#1"])
        for out in (club["output"], again):
            for k in out["plugins_order"]:
                self.assertTrue(k.split("#")[1].isdigit(), k)

    def test_flat_eq_adds_nothing(self):
        flat = compose(BO, EQ["presets_db"]["Flat"], EQ["bands_hz"], None)["output"]
        self.assertEqual(flat["plugins_order"], BO["output"]["plugins_order"])

    def test_bass(self):
        out = compose(BO, None, EQ["bands_hz"], 5.5)["output"]
        self.assertEqual(out["bass_enhancer#0"]["amount"], 5.5)
        off = compose(BO, None, EQ["bands_hz"], 0)["output"]
        self.assertTrue(off["bass_enhancer#0"]["bypass"])
        self.assertEqual(base_bass(BO), 2.0)
        keep = compose(BO, None, EQ["bands_hz"], None)["output"]
        self.assertEqual(keep["bass_enhancer#0"]["amount"], 2.0)

    def test_bass_added_when_base_has_none(self):
        bare = {"output": {"blocklist": [], "plugins_order": ["limiter#0"], "limiter#0": {}}}
        out = compose(bare, None, EQ["bands_hz"], 3)["output"]
        self.assertEqual(out["plugins_order"], ["bass_enhancer#0", "limiter#0"])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.ee = base / "easyeffects"
        (self.ee / "output").mkdir(parents=True)
        (self.ee / "autoload" / "output").mkdir(parents=True)
        (self.ee / "output" / "OMEN16 BO Music.json").write_text(json.dumps(BO))
        (self.ee / "output" / "beoplaxex.json").write_text(json.dumps(BO))
        data = base / "data"
        (data / "prism-sound").mkdir(parents=True)
        (data / "prism-sound" / "eq_presets.json").write_text(json.dumps(EQ))
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data)})
        self.env.start()
        self.loaded = []
        self.default = SPEAKER[0]
        self.patches = [
            mock.patch.object(sound, "speaker_sink", lambda: SPEAKER),
            mock.patch.object(sound, "default_sink", lambda: self.default),
            mock.patch.object(sound, "easyeffects_available", lambda: True),
            mock.patch.object(sound, "easyeffects_running", lambda: True),
            mock.patch.object(sound, "_run", lambda cmd, timeout=10: self.loaded.append(cmd) or ""),
        ]
        for p in self.patches:
            p.start()
        self.engine = SoundEngine(self.ee, SoundStore(base / "sound.json"))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self._tmp.cleanup()

    def autoload(self):
        files = list((self.ee / "autoload" / "output").glob("*.json"))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    def test_lists_known_tunings_first_and_hides_eq_aliases(self):
        (self.ee / "output" / "OMEN16 BO Music + Club.json").write_text(json.dumps(BO))
        self.assertEqual(base_presets(self.ee), ["OMEN16 BO Music", "beoplaxex"])
        f = self.ee / "autoload" / "output" / "x.json"
        f.write_text(json.dumps({"device": SPEAKER[0], "preset-name": "OMEN16 BO Music + Club"}))
        cur = self.engine.current()
        self.assertEqual((cur.base, cur.eq), ("OMEN16 BO Music", "Club"))
        self.assertEqual(sound.load_eq_presets().keys(), {"Club", "Flat"})  # malformed one skipped

    def test_apply_on_speakers_loads_and_autoloads(self):
        msg = self.engine.apply(SoundSettings("OMEN16 BO Music", "Club", 4))
        self.assertEqual(msg, "applied to the speakers")
        self.assertEqual(self.loaded[-1], ["easyeffects", "-w", "-l", PRISM_PRESET])  # -w: no window
        a = self.autoload()
        self.assertEqual((a["device"], a["device-profile"], a["preset-name"]),
                         (SPEAKER[0], "[Out] Speaker", PRISM_PRESET))
        built = json.loads((self.ee / "output" / f"{PRISM_PRESET}.json").read_text())["output"]
        self.assertIn("equalizer#0", built["plugins_order"])
        self.assertEqual(built["bass_enhancer#0"]["amount"], 4.0)
        self.assertEqual(self.engine.current().eq, "Club")

    def test_headphones_active_only_changes_speaker_autoload(self):
        self.default = HEADPHONES
        msg = self.engine.apply(SoundSettings("OMEN16 BO Music"))
        self.assertIn("when the speakers", msg)
        self.assertEqual(self.loaded, [])                 # nothing loaded onto headphones
        self.assertEqual(self.autoload()["preset-name"], PRISM_PRESET)

    def test_does_not_start_easyeffects_over_another_processor(self):
        self.default = "my_filter_chain"
        started = []
        with mock.patch.object(sound, "easyeffects_running", lambda: False), \
                mock.patch.object(sound, "start_easyeffects", lambda: started.append(1) or True):
            msg = self.engine.apply(SoundSettings("OMEN16 BO Music"))
        self.assertIn("EasyEffects left off", msg)
        self.assertEqual(started, [])
        self.assertEqual(self.autoload()["preset-name"], PRISM_PRESET)  # still saved for later

    def test_off_uses_empty_preset_and_keeps_existing_autoload_file(self):
        f = self.ee / "autoload" / "output" / f"{SPEAKER[0]}:[Out] Speaker.json"
        f.write_text(json.dumps({"device": SPEAKER[0], "device-description": "x",
                                 "device-profile": "[Out] Speaker", "preset-name": "beoplaxex"}))
        self.assertEqual(self.engine.current().base, "beoplaxex")  # first run: adopt autoload
        self.engine.apply(SoundSettings(OFF))
        self.assertEqual(self.autoload()["preset-name"], OFF_PRESET)
        off = json.loads((self.ee / "output" / f"{OFF_PRESET}.json").read_text())
        self.assertEqual(off["output"]["plugins_order"], [])

    def test_unknown_eq_is_an_error(self):
        with self.assertRaises(ValueError):
            self.engine.apply(SoundSettings("OMEN16 BO Music", "Disco"))


if __name__ == "__main__":
    unittest.main()
