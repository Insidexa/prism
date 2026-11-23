import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from prism.automation import (Automation, AutomationStore, Rule, on_ac_power,
                              parse_time, pick)
from prism.backend import OmenDevice
from prism.presets import PresetStore
from tests.test_backend import make_tree


def at(hh, mm=0):
    return datetime(2026, 11, 23, hh, mm)


class RuleTests(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(parse_time("07:05"), 425)
        self.assertEqual(parse_time("7:05"), 425)
        for bad in ("24:00", "12:60", "noon", "1230"):
            with self.assertRaises(ValueError):
                parse_time(bad)

    def test_window_wraps_midnight(self):
        night = Rule("Night", start="22:00", end="07:00")
        self.assertTrue(night.matches(23 * 60, True))
        self.assertTrue(night.matches(3 * 60, False))
        self.assertFalse(night.matches(7 * 60, True))   # end is exclusive
        self.assertFalse(night.matches(12 * 60, True))

    def test_daytime_window(self):
        work = Rule("Focus", start="09:00", end="18:00")
        self.assertTrue(work.matches(9 * 60, True))
        self.assertFalse(work.matches(18 * 60, True))

    def test_power(self):
        self.assertTrue(Rule("x", power="battery").matches(0, on_ac=False))
        self.assertFalse(Rule("x", power="battery").matches(0, on_ac=True))
        self.assertFalse(Rule("x", power="ac").matches(0, on_ac=False))
        self.assertTrue(Rule("x").matches(0, on_ac=False))

    def test_first_match_wins(self):
        rules = [Rule("Battery Saver", power="battery"), Rule("Night", start="22:00", end="07:00")]
        self.assertEqual(pick(rules, 23 * 60, on_ac=False)[1].preset, "Battery Saver")
        self.assertEqual(pick(rules, 23 * 60, on_ac=True)[1].preset, "Night")
        self.assertIsNone(pick(rules, 12 * 60, on_ac=True))

    def test_validation(self):
        for bad in (Rule("x", power="usb"), Rule("x", start="10:00"),
                    Rule("x", start="10:00", end="10:00"), Rule("x", start="10", end="11:00")):
            with self.assertRaises(ValueError):
                bad.validate()

    def test_describe(self):
        self.assertEqual(Rule("x").describe(), "Always")
        self.assertEqual(Rule("x", "battery", "22:00", "07:00").describe(), "22:00–07:00 · On battery")


class PowerTests(unittest.TestCase):
    def supply(self, root, name, type_, online=None):
        d = root / name
        d.mkdir()
        (d / "type").write_text(type_ + "\n")
        if online is not None:
            (d / "online").write_text(online + "\n")

    def test_reads_mains_adapter(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.supply(root, "BAT0", "Battery")
            self.supply(root, "ACAD", "Mains", "0")
            self.assertFalse(on_ac_power(root))
            (root / "ACAD/online").write_text("1\n")
            self.assertTrue(on_ac_power(root))

    def test_desktop_without_adapter_counts_as_plugged_in(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(on_ac_power(Path(t)))


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.dev = OmenDevice(make_tree(base / "dev"))
        self.store = AutomationStore(base / "automation.json")
        self.ac = True
        self.logs = []
        self.auto = Automation(self.dev, self.store, PresetStore(base / "presets.json"),
                               power=lambda: self.ac, log=self.logs.append)
        self.store.save(True, [Rule("Battery Saver", power="battery"),
                               Rule("Night", start="22:00", end="07:00")])

    def tearDown(self):
        self._tmp.cleanup()

    def test_applies_once_and_respects_manual_changes(self):
        self.assertEqual(self.auto.step(at(23)), "Night")
        self.assertEqual(self.dev.brightness(), 25)
        self.dev.set_brightness(80)                      # user changes it by hand
        self.assertIsNone(self.auto.step(at(23, 30)))    # same rule: left alone
        self.assertEqual(self.dev.brightness(), 80)
        self.ac = False                                  # unplugged: new rule wins
        self.assertEqual(self.auto.step(at(23, 31)), "Battery Saver")
        self.assertEqual(self.dev.brightness(), 20)
        self.ac = True
        self.assertEqual(self.auto.step(at(23, 32)), "Night")

    def test_no_match_leaves_lighting_alone(self):
        self.assertIsNone(self.auto.step(at(12)))
        self.assertEqual(self.dev.brightness(), 100)

    def test_disabled_does_nothing(self):
        self.store.save(False, [Rule("Night")])
        self.assertIsNone(self.auto.step(at(23)))
        self.assertEqual(self.dev.brightness(), 100)

    def test_editing_rules_reapplies(self):
        self.auto.step(at(23))
        self.dev.set_brightness(80)
        self.store.save(True, [Rule("Night", start="22:00", end="07:00")])
        # Force a different mtime even on coarse filesystems.
        self.auto._mtime = -2
        self.assertEqual(self.auto.step(at(23)), "Night")
        self.assertEqual(self.dev.brightness(), 25)

    def test_missing_preset_is_logged_not_fatal(self):
        self.store.save(True, [Rule("Deleted preset")])
        self.assertIsNone(self.auto.step(at(12)))
        self.assertIn("Deleted preset", self.logs[-1])


if __name__ == "__main__":
    unittest.main()
