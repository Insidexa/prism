import tempfile
import unittest
from pathlib import Path

from prism.omenkey import EVENT, device, presses


def ev(typ, code, value):
    return EVENT.pack(0, 0, typ, code, value)


class OmenKeyTest(unittest.TestCase):
    def test_only_key_down_of_the_omen_key(self):
        data = ev(4, 4, 0x21a5) + ev(1, 151, 1) + ev(0, 0, 0) + ev(1, 151, 0) + ev(1, 30, 1)
        self.assertEqual(list(presses(data)), [151])

    def test_finds_the_omen_input_device(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for n, name in (("event3", "AT Translated Set 2 keyboard"), ("event17", "HP Omen Keyboard")):
                (root / n / "device").mkdir(parents=True)
                (root / n / "device" / "name").write_text(name + "\n")
            self.assertEqual(device(root), Path("/dev/input/event17"))


if __name__ == "__main__":
    unittest.main()
