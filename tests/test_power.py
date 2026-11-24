import tempfile
import unittest
from pathlib import Path

from prism.power import KEEP, PlanStore, PowerAuto


class PowerAutoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = PlanStore(Path(self._tmp.name) / "power.json")
        self.ac = True
        self.applied = []
        self.auto = PowerAuto(self.store, power=lambda: self.ac,
                              apply=lambda n: self.applied.append(n) or [], log=lambda m: None)

    def tearDown(self):
        self._tmp.cleanup()

    def test_applies_on_change_only_and_leaves_login_alone_when_plugged_in(self):
        self.assertIsNone(self.auto.step())          # started plugged in: keep the user's choice
        self.ac = False
        self.assertEqual(self.auto.step(), "battery")
        self.assertIsNone(self.auto.step())          # no change, nothing again
        self.ac = True
        self.assertEqual(self.auto.step(), "balanced")
        self.assertEqual(self.applied, ["battery", "balanced"])

    def test_starting_on_battery_applies_battery_saver(self):
        self.ac = False
        self.assertEqual(self.auto.step(), "battery")

    def test_keep_and_bad_values(self):
        self.store.save({"battery": KEEP, "ac": "nonsense"})
        self.assertEqual(self.store.load(), {"battery": KEEP, "ac": "balanced"})
        self.ac = False
        self.assertIsNone(self.auto.step())
        self.assertEqual(self.applied, [])


if __name__ == "__main__":
    unittest.main()
