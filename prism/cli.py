"""`prism` command line. With no arguments it opens the app."""
from __future__ import annotations

import argparse
import json
import sys

from .backend import ANIMATIONS, THERMAL_PROFILES, OmenDevice, OmenError
from .presets import PresetStore


def status(dev: OmenDevice) -> dict:
    out: dict = {"present": dev.present(), "writable": dev.present() and dev.writable()}
    if not out["present"]:
        return out
    out.update(zones=dev.zones(), brightness=dev.brightness(),
               animation=dev.animation(), speed=dev.speed())
    if dev.has_fan():
        cpu, gpu = dev.fan_rpm()
        out.update(thermal_profile=dev.thermal_profile(), max_fan=dev.max_fan(),
                   cpu_fan_rpm=cpu, gpu_fan_rpm=gpu, curve_enabled=dev.curve_enabled())
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prism", description="Omen laptop lighting and performance control")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("status", help="show current settings")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("color", help="set a zone colour")
    s.add_argument("zone", help="0-3 or 'all'")
    s.add_argument("color", help="RRGGBB")

    s = sub.add_parser("brightness", help="set brightness 0-100")
    s.add_argument("value", type=int)

    s = sub.add_parser("effect", help="set lighting effect")
    s.add_argument("name", choices=ANIMATIONS)
    s.add_argument("--speed", type=int)

    s = sub.add_parser("preset", help="lighting presets")
    s.add_argument("action", choices=("list", "apply", "save", "delete"))
    s.add_argument("name", nargs="?")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("profile", help="get or set the thermal profile")
    s.add_argument("name", nargs="?", choices=THERMAL_PROFILES)

    s = sub.add_parser("maxfan", help="run both fans at full speed")
    s.add_argument("state", choices=("on", "off"))

    s = sub.add_parser("auto", help="automatic presets by time and power source")
    s.add_argument("action", choices=("status", "on", "off"))

    sub.add_parser("daemon", help="apply automatic presets (run by the prism-automation service)")
    sub.add_parser("gui", help="open the app (default)")
    return p


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in (None, "gui"):
        from .app import main as gui_main
        return gui_main()

    if args.cmd == "daemon":
        from .automation import run_daemon
        return run_daemon()

    dev = OmenDevice()
    store = PresetStore()
    try:
        if args.cmd == "status":
            st = status(dev)
            if args.json:
                print(json.dumps(st))
            else:
                for k, v in st.items():
                    print(f"{k}: {' '.join(v) if isinstance(v, list) else v}")
        elif args.cmd == "color":
            if args.zone == "all":
                dev.set_all(args.color)
            else:
                dev.set_zone(int(args.zone), args.color)
        elif args.cmd == "brightness":
            dev.set_brightness(args.value)
        elif args.cmd == "effect":
            if args.speed is not None:
                dev.set_speed(args.speed)
            dev.set_animation(args.name)
        elif args.cmd == "preset":
            if args.action == "list":
                names = [p.name for p in store.all()]
                print(json.dumps(names) if args.json else "\n".join(names))
            elif not args.name:
                raise ValueError(f"preset {args.action} needs a name")
            elif args.action == "apply":
                store.get(args.name).apply(dev)
            elif args.action == "save":
                from .presets import Preset
                store.save(Preset.capture(args.name, dev))
            else:
                store.delete(args.name)
        elif args.cmd == "profile":
            if args.name:
                dev.set_thermal_profile(args.name)
            else:
                print(dev.thermal_profile())
        elif args.cmd == "maxfan":
            dev.set_max_fan(args.state == "on")
        elif args.cmd == "auto":
            from datetime import datetime

            from .automation import AutomationStore, on_ac_power, pick
            auto = AutomationStore()
            enabled, rules = auto.load()
            if args.action in ("on", "off"):
                auto.save(args.action == "on", rules)
            else:
                now = datetime.now()
                ac = on_ac_power()
                hit = pick(rules, now.hour * 60 + now.minute, ac)
                print(f"automatic presets: {'on' if enabled else 'off'}")
                print(f"power: {'plugged in' if ac else 'battery'}")
                for i, r in enumerate(rules):
                    mark = "*" if hit and hit[0] == i else " "
                    print(f" {mark} {i + 1}. {r.preset}  ({r.describe()})")
                if not rules:
                    print("   no rules yet: add them in Prism > Automation")
    except (OmenError, ValueError, KeyError) as e:
        print(f"prism: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return run(sys.argv[1:])
