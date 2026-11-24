"""The OMEN key opens Prism, like OMEN Gaming Hub on Windows.

The key comes from the Omen driver's own input device ("HP Omen Keyboard", only
this key) as KEY_MSDOS. GNOME shortcuts don't reliably react to it, so prism daemon
reads that device itself; system/70-prism-omen.rules lets the laptop's owner read
it (it carries no typing). On laptops where the key arrives on the keyboard
instead, system/71-prism-omen-key.hwdb maps it to XF86Launch1 for a GNOME shortcut.
"""
from __future__ import annotations

import os
import struct
import subprocess
import threading
import time
from pathlib import Path

NAME = "HP Omen Keyboard"
EV_KEY = 1
KEYS = {151, 148}  # KEY_MSDOS, KEY_PROG1
EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit


def device(root: Path = Path("/sys/class/input")) -> Path | None:
    for ev in sorted(root.glob("event*")):
        try:
            if (ev / "device" / "name").read_text().strip() == NAME:
                return Path("/dev/input") / ev.name
        except OSError:
            continue
    return None


def presses(data: bytes):
    """Key-down events of the OMEN key in a chunk of input_event records."""
    for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
        _s, _us, typ, code, value = EVENT.unpack_from(data, off)
        if typ == EV_KEY and code in KEYS and value == 1:
            yield code


def open_prism(cmd: str | None = None) -> None:
    cmd = cmd or str(Path.home() / ".local/bin/prism")
    # Detached: Prism must outlive the daemon's restarts. A running Prism comes to the front.
    subprocess.Popen(["systemd-run", "--user", "--collect", "--quiet", cmd],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class OmenKeyListener:
    def __init__(self, log=print, launch=open_prism):
        self.log, self.launch = log, launch

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        warned = False
        while not stop.is_set():
            dev = device()
            if dev is None or not os.access(dev, os.R_OK):
                if dev is not None and not warned:
                    self.log(f"OMEN key: no permission to read {dev} (run sudo system/install.sh)")
                    warned = True
                stop.wait(30)
                continue
            last = 0.0
            try:
                with open(dev, "rb", buffering=0) as f:
                    while not stop.is_set():
                        data = f.read(EVENT.size * 16)
                        if not data:
                            break
                        for _ in presses(data):
                            now = time.monotonic()
                            if now - last > 1.0:  # a burst of presses opens it once
                                last = now
                                self.launch()
            except OSError:
                stop.wait(5)  # driver reloaded: find the device again
