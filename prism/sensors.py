"""Read-only system telemetry: temperatures and load, without waking the dGPU."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

HWMON = Path("/sys/class/hwmon")


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


@dataclass
class Gpu:
    name: str          # "Radeon RX 6600M" / "Radeon Vega"
    hwmon: Path
    device: Path       # PCI device dir
    discrete: bool

    def asleep(self) -> bool:
        """A runtime-suspended dGPU reports nothing; reading must not wake it."""
        try:
            return (self.device / "power" / "runtime_status").read_text().strip() == "suspended"
        except OSError:
            return False

    def temp_c(self) -> float | None:
        if self.asleep():
            return None
        v = _read_int(self.hwmon / "temp1_input")
        return None if v is None else v / 1000

    def busy_percent(self) -> int | None:
        if self.asleep():
            return None
        return _read_int(self.device / "gpu_busy_percent")


class Sensors:
    def __init__(self, hwmon_root: Path = HWMON, proc_stat: Path = Path("/proc/stat")):
        self.proc_stat = proc_stat
        self.cpu_hwmon: Path | None = None
        self.gpus: list[Gpu] = []
        self._last_cpu: tuple[int, int] | None = None
        for h in sorted(hwmon_root.glob("hwmon*")):
            try:
                name = (h / "name").read_text().strip()
            except OSError:
                continue
            if name == "k10temp" and self.cpu_hwmon is None:
                self.cpu_hwmon = h
            elif name == "amdgpu":
                dev = (h / "device").resolve()
                # The dGPU has edge/junction/mem sensors; the iGPU only edge.
                discrete = (h / "temp2_input").exists()
                self.gpus.append(Gpu("Radeon RX 6600M" if discrete else "Radeon Vega",
                                     h, dev, discrete))
        self.gpus.sort(key=lambda g: not g.discrete)

    @property
    def dgpu(self) -> Gpu | None:
        return next((g for g in self.gpus if g.discrete), None)

    def cpu_temp_c(self) -> float | None:
        if not self.cpu_hwmon:
            return None
        v = _read_int(self.cpu_hwmon / "temp1_input")  # Tctl
        return None if v is None else v / 1000

    def cpu_percent(self) -> float | None:
        """CPU busy % since the previous call (None on the first call)."""
        try:
            fields = [int(x) for x in self.proc_stat.read_text().split("\n", 1)[0].split()[1:]]
        except (OSError, ValueError):
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        last, self._last_cpu = self._last_cpu, (idle, total)
        if not last or total == last[1]:
            return None
        return 100 * (1 - (idle - last[0]) / (total - last[1]))


def thermal_zone_temp_c(zone_type: str, root: Path = Path("/sys/class/thermal")) -> float | None:
    """Temperature of the first thermal zone of this type (e.g. acpitz), in °C."""
    for z in sorted(root.glob("thermal_zone*")):
        try:
            if (z / "type").read_text().strip() != zone_type:
                continue
        except OSError:
            continue
        v = _read_int(z / "temp")
        return None if v is None else v / 1000
    return None
