import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prism import battery as b


def bat_tree(base: Path, status="Discharging", energy=50_000_000) -> Path:
    d = base / "BAT0"
    d.mkdir(parents=True, exist_ok=True)
    for k, v in {"type": "Battery", "status": status, "capacity": "73", "energy_now": str(energy),
                 "energy_full": "60000000", "energy_full_design": "68000000", "cycle_count": "95"}.items():
        (d / k).write_text(v + "\n")
    return base


class InfoTest(unittest.TestCase):
    def test_info_health_and_time_left(self):
        with tempfile.TemporaryDirectory() as t:
            i = b.info(bat_tree(Path(t)), rate=10.0)
        self.assertEqual((i.percent, i.status, i.cycles), (73.0, "Discharging", 95))
        self.assertAlmostEqual(i.time_left_h, 5.0)
        self.assertAlmostEqual(i.health, 100 * 60 / 68)

    def test_app_names_from_gnome_scopes(self):
        cases = {
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-org.mozilla.firefox-14604.scope": "firefox",
            "0::/user.slice/user@1000.service/app.slice/app-flatpak-com.spotify.Client-9.scope": "spotify",
            "0::/user.slice/user@1000.service/app.slice/app-gnome-steam-2231.scope": "steam",
            "0::/user.slice/user@1000.service/session.slice/org.gnome.Shell@user.service": None,
        }
        for cg, want in cases.items():
            self.assertEqual(b.app_from_cgroup(cg), want, cg)


class UsageTest(unittest.TestCase):
    def test_energy_above_the_floor_is_shared_by_activity(self):
        recs = [{"t": 0, "wh": 0.2, "w": 12.0, "apps": {"firefox": 3.0, "steam": 1.0}}]
        total, apps = b.usage(recs)  # few records: floor IDLE_FLOOR_W (6 W) -> half is "active"
        got = dict(apps)
        self.assertAlmostEqual(total, 0.2)
        self.assertAlmostEqual(got["Screen and system"], 0.1)
        self.assertAlmostEqual(got["firefox"], 0.075)
        self.assertAlmostEqual(got["steam"], 0.025)
        self.assertEqual(apps[0][0], "Screen and system")

    def test_idle_minutes_go_to_the_system(self):
        total, apps = b.usage([{"t": 0, "wh": 0.1, "w": 5.0, "apps": {}}])
        self.assertEqual(apps, [("Screen and system", 0.1)])


class RecorderTest(unittest.TestCase):
    def test_records_on_battery_only(self):
        with tempfile.TemporaryDirectory() as t:
            root = bat_tree(Path(t) / "ps")
            log = b.UsageLog(Path(t) / "usage.jsonl")
            snaps = iter([{"firefox": [10.0, 1.0, False]}, {"firefox": [40.0, 6.0, True]}])
            with mock.patch.object(b, "snapshot", lambda dgpu: next(snaps)), \
                    mock.patch.object(b, "discrete_gpu", lambda: None), \
                    mock.patch.object(b, "dgpu_state", lambda addr=None: None):
                r = b.UsageRecorder(log, root)
                self.assertIsNone(r.step(1000.0))  # first look: nothing to compare yet
                bat_tree(root, energy=49_800_000)
                rec = r.step(1060.0)
            self.assertAlmostEqual(rec["wh"], 0.2)
            self.assertAlmostEqual(rec["w"], 12.0)
            self.assertEqual(rec["apps"], {"firefox": 30.0 + 2 * 5.0})
            self.assertEqual(rec["dgpu"], ["firefox"])
            self.assertEqual(len(log.records()), 1)
            bat_tree(root, status="Charging")
            with mock.patch.object(b, "snapshot", lambda dgpu: {}):
                self.assertIsNone(r.step(1120.0))


if __name__ == "__main__":
    unittest.main()
