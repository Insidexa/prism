import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from prism import cli
from prism.backend import (NoAccess, NoDriver, OmenDevice, curve_is_risky,
                           format_curve, normalize_color, parse_curve)
from prism.presets import BUILTIN, Preset, PresetStore
from prism.sensors import Sensors

# Values as the real driver showed them on this laptop.
FAKE = {
    "rgb_zones/all": "#ff0000", "rgb_zones/zone00": "#ff0000", "rgb_zones/zone01": "#ff0000",
    "rgb_zones/zone02": "#ff0000", "rgb_zones/zone03": "#ff0000",
    "rgb_zones/brightness": "100", "rgb_zones/animation_mode": "static",
    "rgb_zones/animation_speed": "1",
    "fan/cpu_fan_rpm": "2500", "fan/gpu_fan_rpm": "0", "fan/max_rpm": "5500 5700",
    "fan/max_fan": "0", "fan/thermal_profile": "silent",
    "fan/fan_curve": "50:0 60:20 70:35 85:50 95:65", "fan/fan_curve_enable": "1",
    "fan/gpu_fan_curve": "(cpu)", "fan/power_limits": "default",
}


def make_tree(base: Path) -> Path:
    for name, value in FAKE.items():
        p = base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value + "\n")
    return base


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.dev = OmenDevice(make_tree(self.base / "dev"))

    def tearDown(self):
        self._tmp.cleanup()


class ParsingTests(unittest.TestCase):
    def test_colors(self):
        self.assertEqual(normalize_color("FF00aa"), "#ff00aa")
        self.assertEqual(normalize_color(" #ABCDEF\n"), "#abcdef")
        for bad in ("red", "#fff", "12345g", ""):
            with self.assertRaises(ValueError):
                normalize_color(bad)

    def test_curve_round_trip(self):
        pts = parse_curve("70:35 50:0 95:max")
        self.assertEqual(pts, [(50, 0), (70, 35), (95, 100)])
        self.assertEqual(format_curve(pts), "50:0 70:35 95:100")
        self.assertIsNone(parse_curve("(cpu)"))

    def test_curve_validation(self):
        for bad in ([(50, 10)], [(50, 10), (50, 20)], [(50, 10), (60, 120)],
                    [(i * 10, i) for i in range(9)]):
            with self.assertRaises(ValueError):
                format_curve(bad)

    def test_risky_curve(self):
        self.assertTrue(curve_is_risky([(50, 0), (90, 30)]))
        self.assertTrue(curve_is_risky([(40, 0), (70, 20)]))
        self.assertFalse(curve_is_risky([(50, 0), (70, 40), (90, 100)]))


class DeviceTests(Tmp):
    def test_read_values(self):
        self.assertEqual(self.dev.zones(), ["#ff0000"] * 4)
        self.assertEqual(self.dev.fan_rpm(), (2500, 0))
        self.assertEqual(self.dev.max_rpm(), (5500, 5700))
        self.assertEqual(self.dev.fan_curve()[0], (50, 0))
        self.assertIsNone(self.dev.gpu_fan_curve())
        self.assertIsNone(self.dev.power_limits().pl1)

    def test_writes_use_driver_format(self):
        self.dev.set_zone(2, "#00FF00")
        self.assertEqual((self.dev.root / "rgb_zones/zone02").read_text(), "00ff00\n")
        self.dev.set_brightness(150)
        self.assertEqual(self.dev.read("rgb_zones/brightness"), "100")
        self.dev.set_speed(0)
        self.assertEqual(self.dev.read("rgb_zones/animation_speed"), "1")
        self.dev.set_power_limits(45, 60)
        self.assertEqual(self.dev.read("fan/power_limits"), "45 60")
        self.dev.set_power_limits(None, None)
        self.assertEqual(self.dev.read("fan/power_limits"), "default")
        self.dev.set_gpu_fan_curve(None)
        self.assertEqual(self.dev.read("fan/gpu_fan_curve"), "cpu")

    def test_same_colour_everywhere_uses_all(self):
        self.dev.set_zones(["#123456"] * 4)
        self.assertEqual(self.dev.read("rgb_zones/all"), "123456")

    def test_rejects_bad_values(self):
        for call in (lambda: self.dev.set_animation("strobe"),
                     lambda: self.dev.set_thermal_profile("turbo"),
                     lambda: self.dev.set_power_limits(60, 45),
                     lambda: self.dev.set_zone(4, "ffffff")):
            with self.assertRaises(ValueError):
                call()

    def test_missing_driver(self):
        dev = OmenDevice(self.base / "nothing")
        self.assertFalse(dev.present())
        with self.assertRaises(NoDriver):
            dev.brightness()

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_no_access_explains_fix(self):
        p = self.dev.root / "rgb_zones/brightness"
        p.chmod(stat.S_IRUSR)
        with self.assertRaises(NoAccess) as cm:
            self.dev.set_brightness(10)
        self.assertIn("install.sh", str(cm.exception))


class PresetTests(Tmp):
    def test_apply_sets_colours_before_effect(self):
        Preset("x", ["#010203"] * 4, 40, "breathing", 7).apply(self.dev)
        self.assertEqual(self.dev.read("rgb_zones/all"), "010203")
        self.assertEqual(self.dev.animation(), "breathing")
        self.assertEqual(self.dev.brightness(), 40)

    def test_apply_keeps_brightness_unless_preset_has_one(self):
        self.dev.set_brightness(42)
        Preset("x", ["#010203"] * 4, animation="wave").apply(self.dev)
        self.assertEqual(self.dev.brightness(), 42)
        self.assertEqual(self.dev.animation(), "wave")
        Preset("y", ["#010203"] * 4, brightness=10).apply(self.dev)
        self.assertEqual(self.dev.brightness(), 10)

    def test_capture_brightness_is_opt_in(self):
        self.dev.set_brightness(70)
        self.assertIsNone(Preset.capture("a", self.dev).brightness)
        self.assertEqual(Preset.capture("b", self.dev, with_brightness=True).brightness, 70)

    def test_store_round_trip_and_override(self):
        store = PresetStore(self.base / "cfg" / "presets.json")
        self.assertEqual(len(store.all()), len(BUILTIN))
        store.save(Preset.capture("Mine", self.dev))
        store.save(Preset("Ice", ["#000001"] * 4))
        self.assertEqual(store.get("mine").zones, ["#ff0000"] * 4)
        self.assertEqual(store.get("Ice").zones[0], "#000001")
        self.assertFalse(store.is_builtin("Ice"))
        store.delete("Ice")
        self.assertTrue(store.is_builtin("Ice"))
        with self.assertRaises(KeyError):
            store.get("nope")

    def test_builtins_are_valid(self):
        for p in BUILTIN:
            p.validate()


class CliTests(Tmp):
    def run_cli(self, *argv):
        out, err = StringIO(), StringIO()
        os.environ["PRISM_SYSFS"] = str(self.dev.root)
        os.environ["XDG_CONFIG_HOME"] = str(self.base / "cfg")
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.run(list(argv))
        finally:
            del os.environ["PRISM_SYSFS"], os.environ["XDG_CONFIG_HOME"]
        return code, out.getvalue(), err.getvalue()

    def test_status_json(self):
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        st = json.loads(out)
        self.assertEqual(st["thermal_profile"], "silent")
        self.assertEqual(st["cpu_fan_rpm"], 2500)

    def test_commands(self):
        self.assertEqual(self.run_cli("effect", "rainbow", "--speed", "4")[0], 0)
        self.assertEqual(self.dev.animation(), "rainbow")
        self.assertEqual(self.run_cli("color", "1", "00ff00")[0], 0)
        self.dev.set_brightness(35)
        self.assertEqual(self.run_cli("preset", "apply", "focus")[0], 0)
        self.assertEqual(self.dev.brightness(), 35)  # presets keep your brightness
        self.assertEqual(self.run_cli("preset", "apply", "night")[0], 0)
        self.assertEqual(self.dev.brightness(), 25)  # unless they carry one
        self.assertEqual(self.run_cli("maxfan", "on")[0], 0)
        self.assertTrue(self.dev.max_fan())

    def test_errors_are_one_line(self):
        code, _, err = self.run_cli("color", "0", "zzz")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("prism: not a hex colour"))


class SensorTests(unittest.TestCase):
    def test_finds_cpu_and_gpus(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            def hwmon(n, name, temps, dev):
                h = root / f"hwmon{n}"
                h.mkdir()
                (h / "name").write_text(name + "\n")
                for i, t in enumerate(temps, 1):
                    (h / f"temp{i}_input").write_text(f"{t}\n")
                (root / dev / "power").mkdir(parents=True)
                (root / dev / "power" / "runtime_status").write_text("active\n")
                (h / "device").symlink_to(root / dev)
            hwmon(0, "k10temp", [61250], "cpu")
            hwmon(1, "amdgpu", [45000], "igpu")
            hwmon(2, "amdgpu", [50000, 55000, 60000], "dgpu")
            (root / "dgpu/power/runtime_status").write_text("suspended\n")
            stat_file = root / "stat"
            stat_file.write_text("cpu 100 0 100 800 0 0 0 0 0 0\n")
            s = Sensors(root, stat_file)
            self.assertEqual(s.cpu_temp_c(), 61.25)
            self.assertTrue(s.dgpu.discrete)
            self.assertIsNone(s.dgpu.temp_c())  # asleep: not read
            self.assertEqual(s.gpus[1].temp_c(), 45.0)
            self.assertIsNone(s.cpu_percent())
            stat_file.write_text("cpu 150 0 150 900 0 0 0 0 0 0\n")
            self.assertAlmostEqual(s.cpu_percent(), 50.0)


if __name__ == "__main__":
    unittest.main()
