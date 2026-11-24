import tempfile
import unittest
from pathlib import Path

import numpy as np

from prism import music
from prism.music import (BLOCK, RATE, Analyzer, MusicSettings, MusicStore, frame_colors, scale,
                         set_enabled)

from tests.test_backend import make_tree  # noqa: F401  (fake sysfs helper)


def tone(freq, db=-20.0, n=BLOCK):
    t = np.arange(n) / RATE
    return (10 ** (db / 20) * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class AnalyzerTest(unittest.TestCase):
    def test_each_band_drives_its_zone(self):
        for freq, zone in ((60, 2), (300, 3), (1200, 1), (5000, 0)):
            an = Analyzer()
            for k in range(20):
                lv = an.step(tone(freq), k * 0.02)
            self.assertEqual(max(lv, key=lv.get), zone, f"{freq} Hz")
            self.assertGreater(lv[zone], 0.8)

    def test_silence_returns_to_plain_colours_after_a_while(self):
        an = Analyzer()
        quiet = np.zeros(BLOCK, dtype=np.float32)
        self.assertIsNotNone(an.step(quiet, 0.0))
        self.assertIsNone(an.step(quiet, 2.0))
        self.assertIsNotNone(an.step(tone(100), 2.1))

    def test_adapts_to_quiet_music(self):
        an = Analyzer()
        for k in range(60):
            lv = an.step(tone(100, db=-45), k * 0.02)
        self.assertGreater(lv[2], 0.8)  # a quiet song still reaches full brightness


class ColourTest(unittest.TestCase):
    def test_scale_and_frames(self):
        self.assertEqual(scale("#ff8000", 0.5), "804000")
        base = ["#ff0000", "#00ff00", "#0000ff", "#ffffff"]
        self.assertEqual(frame_colors(base, None), ["ff0000", "00ff00", "0000ff", "ffffff"])
        f = frame_colors(base, {0: 1.0, 1: 0.0, 2: 0.5, 3: 1.0})
        self.assertEqual(f[0], "ff0000")
        self.assertEqual(f[1], scale("#00ff00", music.FLOOR))  # never fully dark
        self.assertEqual(f[2], scale("#0000ff", music.FLOOR + (1 - music.FLOOR) * 0.5))


class StoreTest(unittest.TestCase):
    def test_round_trip_and_enable_takes_the_showing_colours(self):
        with tempfile.TemporaryDirectory() as d:
            st = MusicStore(Path(d) / "music.json")
            self.assertEqual(st.load(), MusicSettings())
            set_enabled(True, st, colors=["#FF0000"] * 4)
            self.assertEqual(st.load().colors, ["#ff0000"] * 4)
            set_enabled(False, st)
            set_enabled(True, st)
            self.assertEqual(st.load().colors, [])  # empty: read them when it starts
            (Path(d) / "music.json").write_text("{broken")
            self.assertFalse(st.load().enabled)


if __name__ == "__main__":
    unittest.main()
