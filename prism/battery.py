"""Battery: status, history and estimated use per app (like macOS's Battery page).

No laptop measures power per program, macOS included: it is estimated from what
each program does. Prism does the same. While on battery, `prism daemon` notes
once a minute how much energy the battery gave and how much CPU and GPU time each
app used (apps grouped as GNOME starts them: one entry per app, not per process).
The energy above the idle floor (screen, Wi-Fi, the rest of the system) is shared
out by activity; the floor is shown as "Screen and system".

It also tells which apps keep the discrete GPU awake, the biggest single drain a
gaming laptop has on battery.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

BAT = Path("/sys/class/power_supply")
GPU_WEIGHT = 2.0       # a GPU-second costs about twice a CPU-second on this class of laptop
IDLE_FLOOR_W = 6.0     # until enough history: screen + system at idle
KEEP_DAYS = 7
SAMPLE_S = 60


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "prism"


def battery_dir(root: Path = BAT) -> Path | None:
    for d in sorted(root.glob("BAT*")):
        if (d / "type").exists() and (d / "type").read_text().strip() == "Battery":
            return d
    return None


def _read(d: Path, name: str) -> str | None:
    try:
        return (d / name).read_text().strip()
    except OSError:
        return None


def _num(d: Path, name: str) -> float | None:
    v = _read(d, name)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


@dataclass
class BatteryInfo:
    percent: float
    status: str               # Charging, Discharging, Full, Not charging
    energy_wh: float | None
    full_wh: float | None
    design_wh: float | None
    cycles: int | None
    rate_w: float | None      # from upower (the kernel's power_now is missing here)
    time_left_h: float | None

    @property
    def health(self) -> float | None:
        if self.full_wh and self.design_wh:
            return 100 * self.full_wh / self.design_wh
        return None


def upower_device() -> str | None:
    out = _run(["upower", "-e"])
    return next((line for line in out.splitlines() if "battery_BAT" in line), None)


def upower_rate() -> float | None:
    dev = upower_device()
    if not dev:
        return None
    m = re.search(r"energy-rate:\s*([\d.]+)", _run(["upower", "-i", dev]))
    return float(m.group(1)) if m else None


def info(root: Path = BAT, rate: float | None = None) -> BatteryInfo | None:
    d = battery_dir(root)
    if d is None:
        return None
    wh = lambda name: (v / 1e6) if (v := _num(d, name)) is not None else None  # noqa: E731
    energy, full = wh("energy_now"), wh("energy_full")
    status = _read(d, "status") or "Unknown"
    rate = upower_rate() if rate is None else rate
    left = None
    if rate and rate > 0.5 and energy is not None:
        if status == "Discharging":
            left = energy / rate
        elif status == "Charging" and full is not None:
            left = max(0.0, full - energy) / rate
    cycles = _num(d, "cycle_count")
    return BatteryInfo(_num(d, "capacity") or 0.0, status, energy, full, wh("energy_full_design"),
                       int(cycles) if cycles is not None else None, rate, left)


def history(kind: str = "rate", hours: float = 24, points: int = 200) -> list[tuple[float, float]]:
    """upower's history: (unix time, value) oldest first; kind 'rate' (W) or 'charge' (%)."""
    dev = upower_device()
    if not dev:
        return []
    out = _run(["gdbus", "call", "--system", "--dest", "org.freedesktop.UPower", "--object-path", dev,
                "--method", "org.freedesktop.UPower.Device.GetHistory", kind, str(int(hours * 3600)),
                str(points)])
    rows = [(float(t), float(v)) for t, v, _s in re.findall(r"\((\d+), ([\d.e+-]+), (\d+)\)", out)]
    return sorted(r for r in rows if r[1] >= 0)


# --------------------------------------------------------------------------- apps
APP_SCOPE = re.compile(r"app-(?:gnome|flatpak|org\.gnome\.\w+)?-?(?P<app>[^/]+?)(?:-\d+)?\.(?:scope|service)")


def app_of(pid: int) -> str | None:
    """The app a process belongs to, from the systemd scope GNOME starts it in."""
    try:
        name = app_from_cgroup(Path(f"/proc/{pid}/cgroup").read_text())
    except OSError:
        return None
    if name:
        return name
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip() or None
    except OSError:
        return None


def app_from_cgroup(cg: str) -> str | None:
    for part in reversed(cg.strip().split("/")):
        m = APP_SCOPE.fullmatch(part)
        if m:
            name = m.group("app").replace("\\x2d", "-")
            name = re.sub(r"@.*$", "", name)
            if name.count(".") >= 2:  # org.gnome.Nautilus -> Nautilus, com.spotify.Client -> spotify
                parts = name.split(".")
                return parts[-2] if parts[-1].lower() in ("client", "desktop", "app") else parts[-1]
            return name
    return None


def gpu_of(pid: int, dgpu: str | None) -> tuple[float, bool]:
    """(GPU busy seconds so far, uses the discrete GPU) from the DRM fdinfo of a process."""
    total, on_dgpu = 0.0, False
    try:
        fds = os.listdir(f"/proc/{pid}/fdinfo")
    except OSError:
        return 0.0, False
    seen: set[str] = set()
    for fd in fds:
        try:
            text = Path(f"/proc/{pid}/fdinfo/{fd}").read_text()
        except OSError:
            continue
        if "drm-driver" not in text:
            continue
        cid = re.search(r"drm-client-id:\s*(\d+)", text)
        key = cid.group(1) if cid else fd
        if key in seen:  # the same client opened twice
            continue
        seen.add(key)
        pdev = re.search(r"drm-pdev:\s*(\S+)", text)
        ns = sum(int(v) for v in re.findall(r"drm-engine-[\w-]+:\s*(\d+) ns", text))
        total += ns / 1e9
        if dgpu and pdev and pdev.group(1) == dgpu:
            on_dgpu = True
    return total, on_dgpu


def discrete_gpu() -> str | None:
    """PCI address of a discrete GPU that can sleep (has no display of its own)."""
    for d in sorted(Path("/sys/bus/pci/devices").glob("*")):
        try:
            cls = (d / "class").read_text().strip()
        except OSError:
            continue
        # 0x0380 "display controller" / 0x0302 "3D": a GPU without the laptop screen.
        if cls.startswith(("0x0380", "0x0302")) and (d / "power").exists():
            return d.name
    return None


def dgpu_state(addr: str | None = None) -> str | None:
    """'active' or 'suspended' (asleep), None without a discrete GPU."""
    addr = addr or discrete_gpu()
    if not addr:
        return None
    try:
        return Path(f"/sys/bus/pci/devices/{addr}/power/runtime_status").read_text().strip()
    except OSError:
        return None


def snapshot(dgpu: str | None) -> dict[str, list]:
    """{app: [cpu seconds, gpu seconds, uses dGPU]} over all processes we can see."""
    hz = os.sysconf("SC_CLK_TCK")
    uid = os.getuid()
    apps: dict[str, list] = {}
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        pid = int(p.name)
        try:
            stat = (p / "stat").read_text()
            fields = stat[stat.rindex(")") + 2:].split()
            cpu = (int(fields[11]) + int(fields[12])) / hz   # utime + stime
            own = p.stat().st_uid == uid
        except (OSError, ValueError, IndexError):
            continue
        name = app_of(pid) if own else "System"
        if not name:
            continue
        gpu, on_d = gpu_of(pid, dgpu) if own else (0.0, False)
        a = apps.setdefault(name, [0.0, 0.0, False])
        a[0] += cpu
        a[1] += gpu
        a[2] = a[2] or on_d
    return apps


def dgpu_users(dgpu: str | None = None) -> list[str]:
    dgpu = dgpu or discrete_gpu()
    return sorted(n for n, (_c, _g, d) in snapshot(dgpu).items() if d) if dgpu else []


# --------------------------------------------------------------------------- recorder
class UsageLog:
    def __init__(self, path: Path | None = None):
        self.path = path or state_dir() / "battery-usage.jsonl"

    def append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def records(self, since: float = 0.0) -> list[dict]:
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("t", 0) >= since:
                out.append(r)
        return out

    def prune(self, now: float) -> None:
        if not self.path.exists():
            return  # nothing recorded yet (and maybe no folder either)
        keep = self.records(now - KEEP_DAYS * 86400)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in keep))
        tmp.replace(self.path)


def usage(records: list[dict]) -> tuple[float, list[tuple[str, float]]]:
    """(total Wh, [(app, Wh)] largest first, 'Screen and system' included)."""
    rates = sorted(r["w"] for r in records if r.get("w"))
    floor = rates[len(rates) // 20] if len(rates) >= 30 else IDLE_FLOOR_W  # ~5th percentile
    total, per = 0.0, {}
    for r in records:
        wh = r.get("wh", 0.0)
        total += wh
        w = r.get("w") or 0.0
        active = max(0.0, min(1.0, 1 - floor / w)) if w > 0 else 0.0
        weights = r.get("apps", {})
        sw = sum(weights.values())
        if sw <= 0:
            active = 0.0
        per["Screen and system"] = per.get("Screen and system", 0.0) + wh * (1 - active)
        for app, wgt in weights.items():
            per[app] = per.get(app, 0.0) + wh * active * wgt / sw
    return total, sorted(per.items(), key=lambda kv: -kv[1])


class UsageRecorder:
    """Run by prism daemon: one record a minute while on battery."""

    def __init__(self, log_: UsageLog | None = None, root: Path = BAT, log=print):
        self.ulog = log_ or UsageLog()
        self.root, self.log = root, log
        self.prev: tuple[float, float, dict] | None = None  # (time, energy Wh, snapshot)
        self.dgpu = discrete_gpu()

    def step(self, now: float) -> dict | None:
        d = battery_dir(self.root)
        if d is None:
            return None
        status = _read(d, "status")
        energy = _num(d, "energy_now")
        if status != "Discharging" or energy is None:
            self.prev = None
            return None
        snap = snapshot(self.dgpu)
        e = energy / 1e6
        rec = None
        if self.prev is not None:
            t0, e0, s0 = self.prev
            dt = now - t0
            if 0 < dt < 3 * SAMPLE_S and e0 >= e:
                weights = {}
                for app, (cpu, gpu, _d) in snap.items():
                    c0, g0, _ = s0.get(app, (cpu, gpu, False))
                    w = max(0.0, cpu - c0) + GPU_WEIGHT * max(0.0, gpu - g0)
                    if w > 0.05:
                        weights[app] = round(w, 2)
                rec = {"t": round(now), "wh": round(e0 - e, 4), "w": round((e0 - e) * 3600 / dt, 2),
                       "apps": weights, "dgpu": sorted(a for a, (_c, _g, on) in snap.items() if on),
                       "dgpu_awake": dgpu_state(self.dgpu) == "active"}
                self.ulog.append(rec)
        self.prev = (now, e, snap)
        return rec

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        last_prune = 0.0
        while not stop.is_set():
            try:
                now = time.time()
                self.step(now)
                if now - last_prune > 6 * 3600:
                    self.ulog.prune(now)
                    last_prune = now
            except Exception as e:  # never let the recorder take the daemon down
                self.log(f"battery recorder: {e}")
            stop.wait(SAMPLE_S)


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# --------------------------------------------------------------------------- other devices
@dataclass
class DeviceBattery:
    name: str
    percent: float | None
    state: str        # charging, discharging, full, unknown
    source: str       # upower or solaar


def peripherals() -> list[DeviceBattery]:
    """Batteries of mice, keyboards and headphones: upower's devices, plus Logitech
    devices behind a Bolt receiver, which only Solaar can read."""
    out: list[DeviceBattery] = []
    for dev in _run(["upower", "-e"]).splitlines():
        if any(x in dev for x in ("DisplayDevice", "line_power", "battery_BAT")):
            continue
        info_ = _run(["upower", "-i", dev])
        model = re.search(r"model:\s*(.+)", info_)
        pct = re.search(r"percentage:\s*([\d.]+)%(.*)", info_)
        state = re.search(r"state:\s*(\S+)", info_)
        if not model or not pct or "should be ignored" in pct.group(2):
            continue
        out.append(DeviceBattery(model.group(1).strip(), float(pct.group(1)),
                                 state.group(1) if state else "unknown", "upower"))
    out += solaar_batteries()
    seen, uniq = set(), []
    for d in out:
        if d.name not in seen:
            seen.add(d.name)
            uniq.append(d)
    return uniq


_SOLAAR_CACHE: tuple[float, list] = (0.0, [])


def solaar_batteries(text: str | None = None) -> list[DeviceBattery]:
    """From `solaar show`: one entry per paired device with a battery reading.
    `solaar show` takes seconds, so readings are kept for two minutes."""
    global _SOLAAR_CACHE
    from shutil import which
    if text is None:
        if not which("solaar"):
            return []
        if time.monotonic() - _SOLAAR_CACHE[0] < 120:
            return list(_SOLAAR_CACHE[1])
        out = solaar_batteries(_run(["solaar", "show"]))
        _SOLAAR_CACHE = (time.monotonic(), out)
        return list(out)
    out, name = [], None
    for line in text.splitlines():
        m = re.match(r"\s{0,4}\d+: (.+?)\s*$", line)
        if m and "Receiver" not in m.group(1):
            name = m.group(1)
            continue
        b = re.search(r"Battery:\s*(\d+)%(?:,\s*(?:BatteryStatus\.)?([\w ]+))?", line)
        if b and name:
            state = (b.group(2) or "unknown").strip().lower().replace("recharging", "charging")
            out.append(DeviceBattery(name, float(b.group(1)), state, "solaar"))
            name = None
    return out
