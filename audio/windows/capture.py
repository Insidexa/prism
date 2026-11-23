"""Capture what the Windows B&O / DTS audio processing does on this laptop.

Run on Windows (see INSTRUCTIONS.md). It does three things, all read-only for
the system:

1. inventory  - audio driver versions, the speaker endpoint's audio effects
                (APO) registry settings, and copies of DTS / B&O / Realtek
                tuning files from the driver store, for analysis on Linux.
2. capture    - plays exponential sine sweeps at several levels through the
                speakers and records the processed digital signal via WASAPI
                loopback. Run once with B&O/DTS enabled and once disabled.
3. compare    - checks whether loopback actually sees the processing.

Everything is written to ./prism-capture/ next to this script.
Requires: Python 3.10+, `pip install numpy soundcard`.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

SR = 48000
SWEEP_SECONDS = 8.0
F1, F2 = 20.0, 20000.0
LEVELS_DBFS = (-40, -24, -12, -6)   # quiet to loud: shows level-dependent processing
OUT = Path(__file__).resolve().parent / "prism-capture"

CONFIG_EXTS = {".ini", ".xml", ".json", ".dat", ".bin", ".cfg", ".txt", ".dtsx", ".preset",
               ".inf", ".csv", ".prm", ".dts", ".bnk"}
NAME_HINTS = ("dts", "bang", "olufsen", "b&o", "bno", "hpaudio", "hp_audio", "omen", "realtek",
              "rtk", "rtkapo", "apo", "audio boost")
SEARCH_ROOTS = (
    r"C:\Windows\System32\DriverStore\FileRepository",
    r"C:\ProgramData",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\Windows\System32\drivers",
)
MAX_FILE = 8 * 1024 * 1024


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace").stdout
    except Exception as e:  # noqa: BLE001 - inventory is best effort
        return f"<failed: {e}>"


def ps(script: str) -> str:
    return run(["powershell", "-NoProfile", "-Command", script])


# --------------------------------------------------------------------------- inventory
def inventory() -> None:
    inv = OUT / "inventory"
    inv.mkdir(parents=True, exist_ok=True)
    (inv / "system.txt").write_text(
        f"{platform.platform()}\n" + ps("Get-CimInstance Win32_ComputerSystem | Format-List *"),
        encoding="utf-8")
    (inv / "audio_drivers.txt").write_text(ps(
        "Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceClass -match "
        "'MEDIA|AUDIOENDPOINT|SOFTWARECOMPONENT|EXTENSION' -or $_.DeviceName -match "
        "'Realtek|DTS|Audio|Bang' } | Select-Object DeviceName,DeviceClass,DriverVersion,"
        "DriverDate,Manufacturer,InfName,HardWareID | Format-List"), encoding="utf-8")
    (inv / "packages.txt").write_text(ps(
        "Get-AppxPackage | Where-Object { $_.Name -match 'DTS|Realtek|Audio|Omen|Bang|HP' } | "
        "Select-Object Name,Version,InstallLocation | Format-List"), encoding="utf-8")
    # Endpoint effects (APO CLSIDs and their property stores) for every playback device.
    reg = inv / "mmdevices_render.reg"
    run(["reg", "export", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render",
         str(reg), "/y"])
    clsids = inv / "apo_clsids.txt"
    clsids.write_text(ps(
        "Get-ChildItem 'HKLM:\\SOFTWARE\\Classes\\AudioEngine\\AudioProcessingObjects' | "
        "ForEach-Object { $p = Get-ItemProperty $_.PSPath; "
        "\"$($_.PSChildName)  $($p.FriendlyName)  $($p.Copyright)\" }"), encoding="utf-8")

    copied = inv / "files"
    copied.mkdir(exist_ok=True)
    listing = []
    for root in SEARCH_ROOTS:
        rp = Path(root)
        if not rp.exists():
            continue
        for p in rp.rglob("*"):
            try:
                if not p.is_file():
                    continue
                low = str(p).lower()
                if not any(h in low for h in NAME_HINTS):
                    continue
                size = p.stat().st_size
                listing.append(f"{size:>10}  {p}")
                if p.suffix.lower() in CONFIG_EXTS and size <= MAX_FILE:
                    dest = copied / str(p).replace(":", "").replace("\\", "__")
                    shutil.copy2(p, dest)
            except (OSError, PermissionError):
                continue
    (inv / "file_listing.txt").write_text("\n".join(listing), encoding="utf-8")

    # Realtek ships a codec dump tool in some driver packages; it answers whether
    # Windows programs the codec (amp gain, COEF registers) differently.
    for tool in Path(SEARCH_ROOTS[0]).rglob("RtHDDump*.exe") if Path(SEARCH_ROOTS[0]).exists() else []:
        (inv / "rthddump.txt").write_text(run([str(tool)]), encoding="utf-8")
        break
    print(f"inventory: {len(listing)} matching files listed, configs copied to {copied}")


# --------------------------------------------------------------------------- capture
def sweep():
    import numpy as np
    n = int(SR * SWEEP_SECONDS)
    t = np.arange(n) / SR
    k = SWEEP_SECONDS / np.log(F2 / F1)
    s = np.sin(2 * np.pi * F1 * k * (np.exp(t / k) - 1))
    fade = int(0.05 * SR)
    s[:fade] *= np.linspace(0, 1, fade)
    s[-fade:] *= np.linspace(1, 0, fade)
    return s


def save_wav(path: Path, data, channels: int) -> None:
    import numpy as np
    pcm = (np.clip(data, -1, 1) * 2147483647).astype("<i4")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(4)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def capture(tag: str) -> None:
    import numpy as np
    import soundcard as sc

    spk = sc.default_speaker()
    print(f"speaker: {spk.name}")
    if "speaker" not in spk.name.lower():
        print("WARNING: the default output does not look like the internal speakers. "
              "Unplug headphones/HDMI and set the laptop speakers as default.")
    loop = sc.get_microphone(id=str(spk.name), include_loopback=True)
    d = OUT / tag
    d.mkdir(parents=True, exist_ok=True)
    s = sweep()
    save_wav(OUT / "sweep_reference.wav", s, 1)
    meta = {"tag": tag, "speaker": spk.name, "samplerate": SR, "levels_dbfs": LEVELS_DBFS,
            "sweep": {"seconds": SWEEP_SECONDS, "f1": F1, "f2": F2}, "takes": []}
    pad = int(0.5 * SR)
    tail = int(1.5 * SR)
    for level in LEVELS_DBFS:
        for side in ("both", "left", "right"):
            if level != -24 and side != "both":
                continue  # per-channel takes only at the reference level
            sig = s * 10 ** (level / 20)
            st = np.zeros((pad + len(sig) + tail, 2))
            if side in ("both", "left"):
                st[pad:pad + len(sig), 0] = sig
            if side in ("both", "right"):
                st[pad:pad + len(sig), 1] = sig
            box = {}

            def rec(frames=len(st) + SR):
                with loop.recorder(samplerate=SR, channels=2) as r:
                    box["data"] = r.record(numframes=frames)

            th = threading.Thread(target=rec)
            th.start()
            time.sleep(0.3)
            with spk.player(samplerate=SR, channels=2) as p:
                p.play(st)
            th.join()
            name = f"sweep_{level}dB_{side}.wav"
            save_wav(d / name, box["data"], 2)
            peak = 20 * np.log10(np.max(np.abs(box["data"])) + 1e-12)
            meta["takes"].append({"file": name, "level_dbfs": level, "channels": side,
                                  "recorded_peak_dbfs": round(float(peak), 2)})
            print(f"  {name}: recorded peak {peak:.1f} dBFS")
            time.sleep(0.5)
    (d / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"capture '{tag}' saved to {d}")


# --------------------------------------------------------------------------- compare
def compare() -> None:
    import numpy as np

    def spectrum(path: Path):
        with wave.open(str(path)) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), "<i4").astype(float) / 2147483647
        x = x.reshape(-1, 2).mean(axis=1)
        f = np.fft.rfftfreq(len(x), 1 / SR)
        m = np.abs(np.fft.rfft(x))
        bands = [60, 100, 150, 250, 400, 700, 1000, 2000, 4000, 8000, 12000, 16000]
        out = []
        for b in bands:
            sel = (f > b / 1.2) & (f < b * 1.2)
            out.append(20 * np.log10(m[sel].mean() + 1e-12))
        return bands, np.array(out)

    on, off = OUT / "dts_on", OUT / "dts_off"
    if not (on.exists() and off.exists()):
        sys.exit("run `capture dts_on` and `capture dts_off` first")
    lines = ["level  " + " ".join(f"{b:>6}" for b in spectrum(on / "sweep_-24dB_both.wav")[0])]
    max_diff = 0.0
    for level in LEVELS_DBFS:
        _, a = spectrum(on / f"sweep_{level}dB_both.wav")
        _, b = spectrum(off / f"sweep_{level}dB_both.wav")
        diff = a - b
        max_diff = max(max_diff, float(np.max(np.abs(diff))))
        lines.append(f"{level:>4}dB " + " ".join(f"{v:>+6.1f}" for v in diff))
    verdict = ("Loopback SEES the B&O/DTS processing (on/off differ by up to "
               f"{max_diff:.1f} dB). The captures are usable." if max_diff > 1.0 else
               "Loopback does NOT see the processing (on/off identical). The effect probably "
               "runs as an endpoint effect after the loopback tap; capture with a microphone "
               "instead (see INSTRUCTIONS.md).")
    report = "dB difference, DTS on minus off, per band (Hz):\n" + "\n".join(lines) + "\n\n" + verdict
    (OUT / "compare.txt").write_text(report)
    print(report)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory")
    c = sub.add_parser("capture")
    c.add_argument("tag", choices=("dts_on", "dts_off"))
    sub.add_parser("compare")
    sub.add_parser("pack")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    if args.cmd == "inventory":
        inventory()
    elif args.cmd == "capture":
        capture(args.tag)
    elif args.cmd == "compare":
        compare()
    else:
        archive = shutil.make_archive(str(OUT), "zip", OUT)
        print(f"packed: {archive}")


if __name__ == "__main__":
    main()
