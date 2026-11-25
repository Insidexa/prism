import tempfile
import unittest
from pathlib import Path

from prism.charge import ChargeLimiter, ChargeSettings, ChargeStore, should_hold


class FakeDev:
    def __init__(self, state="1 255 1 0"):
        self.state, self.writes, self.root = state, [], Path(tempfile.mkdtemp())
        (self.root / "battery").mkdir()
        (self.root / "battery" / "charge_control").write_text(state)

    def _path(self, name):
        return self.root / name

    def read(self, name):
        return self.state

    def write(self, name, value):
        self.writes.append(int(value))
        self.state = "4 255 1 0" if value == "5" else "1 255 1 0"


class ChargeTest(unittest.TestCase):
    def test_should_hold(self):
        self.assertFalse(should_hold(None, 5, 95, True, False))
        self.assertFalse(should_hold(80, 5, 85, False, False))   # on battery: never
        self.assertTrue(should_hold(80, 5, 80, True, False))
        self.assertFalse(should_hold(80, 5, 79, True, False))
        self.assertTrue(should_hold(80, 5, 76, True, True))      # hysteresis
        self.assertFalse(should_hold(80, 5, 75, True, True))

    def test_limiter_holds_and_releases(self):
        d = Path(tempfile.mkdtemp())
        store = ChargeStore(d / "charge.json")
        store.save(ChargeSettings(limit=80))
        dev, pct, ac = FakeDev(), [85.0], [True]
        lim = ChargeLimiter(dev, store, on_ac=lambda: ac[0], percent=lambda: pct[0], log=lambda m: None)
        self.assertTrue(lim.step())
        self.assertEqual(dev.writes, [5])
        lim.step()                      # already held: no second write
        self.assertEqual(dev.writes, [5])
        ac[0] = False
        self.assertFalse(lim.step())
        self.assertEqual(dev.writes, [5, 0])

    def test_release_on_exit(self):
        d = Path(tempfile.mkdtemp())
        store = ChargeStore(d / "charge.json")
        store.save(ChargeSettings(limit=60))
        dev = FakeDev()
        lim = ChargeLimiter(dev, store, on_ac=lambda: True, percent=lambda: 70.0, log=lambda m: None)
        lim.step()
        lim.release()
        self.assertEqual(dev.writes, [5, 0])

    def test_store_rejects_bad_limit(self):
        d = Path(tempfile.mkdtemp())
        (d / "charge.json").write_text('{"limit": 20, "margin": 99}')
        s = ChargeStore(d / "charge.json").load()
        self.assertIsNone(s.limit)
        self.assertEqual(s.margin, 20)
