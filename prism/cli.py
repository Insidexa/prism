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
    s.add_argument("--with-brightness", action="store_true",
                   help="save: also store the current brightness (otherwise applying keeps it)")

    s = sub.add_parser("profile", help="get or set the thermal profile")
    s.add_argument("name", nargs="?", choices=THERMAL_PROFILES)

    s = sub.add_parser("maxfan", help="run both fans at full speed")
    s.add_argument("state", choices=("on", "off"))

    s = sub.add_parser("plan", help="power plan now, or which plan to use on battery / plugged in")
    s.add_argument("name", nargs="?", choices=("battery", "balanced", "performance", "status"),
                   default="status")
    s.add_argument("--on-battery", choices=("battery", "balanced", "performance", "keep"))
    s.add_argument("--plugged-in", choices=("battery", "balanced", "performance", "keep"))

    s = sub.add_parser("battery", help="battery status, history and estimated use per app")
    s.add_argument("--hours", type=float, default=24, help="period for use per app (default 24)")

    s = sub.add_parser("music", help="keyboard lighting that follows the music playing")
    s.add_argument("action", choices=("on", "off", "status"))
    s.add_argument("--sensitivity", type=float, help="0.5 (calm) to 2 (lively), default 1")

    s = sub.add_parser("sound", help="speaker sound: Prism chain or EasyEffects, OMEN equalizer, bass")
    s.add_argument("action", choices=("status", "set", "list", "toggle"),
                   help="toggle: switch between the Prism chain and EasyEffects (to compare)")
    s.add_argument("--engine", choices=("chain", "easyeffects"),
                   help="chain: Prism's PipeWire chain; easyeffects: an EasyEffects preset")
    s.add_argument("--chain", help="a Prism chain profile (engine chain)")
    s.add_argument("--base", help="an EasyEffects output preset, or 'off' (engine easyeffects)")
    s.add_argument("--eq", help="an OMEN equalizer preset, or 'none'")
    s.add_argument("--bass", type=float,
                   help="chain: Prism Bass in dB vs the profile (-12 = off); easyeffects: amount (0 = off)")

    s = sub.add_parser("auto", help="automatic presets by time and power source")
    s.add_argument("action", choices=("status", "on", "off"))

    sub.add_parser("daemon", help="automatic presets and the speaker chain watcher (run by the prism-automation service)")
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

    if args.cmd == "battery":
        return battery_report(args.hours)
    if args.cmd == "plan":
        from .power import PLANS, PlanStore, apply_plan, builtin_rates, power_mode
        st = PlanStore()
        auto = st.load()
        if args.on_battery or args.plugged_in:
            auto["battery"] = args.on_battery or auto["battery"]
            auto["ac"] = args.plugged_in or auto["ac"]
            st.save(auto)
        if args.name == "status":
            rates = builtin_rates()
            print(f"power mode: {power_mode() or 'unknown'}")
            if rates:
                print(f"screen: {rates[0]:g} Hz (of {', '.join(f'{r:g}' for r in rates[1])})")
            lab = lambda k: "keep as is" if auto[k] == "keep" else PLANS[auto[k]].label  # noqa: E731
            print(f"on battery: {lab('battery')}; plugged in: {lab('ac')}")
        else:
            print(f"{PLANS[args.name].label}: " + (", ".join(apply_plan(args.name)) or "no change"))
        return 0

    dev = OmenDevice()
    store = PresetStore()
    try:
        if args.cmd == "status":
            st = status(dev)
            if args.json:
                st["sound"] = sound_status()
                from .music import MusicStore
                st["music"] = MusicStore().load().enabled and dev.has_frame()
                print(json.dumps(st))
            else:
                for k, v in st.items():
                    print(f"{k}: {' '.join(v) if isinstance(v, list) else v}")
        elif args.cmd == "music":
            from .music import MusicStore, available, set_enabled
            store = MusicStore()
            if args.action == "status":
                m = store.load()
                why = available()
                print(f"music lighting: {'on' if m.enabled else 'off'}"
                      + (f" (not available: {why})" if why else ""))
                print(f"sensitivity: {m.sensitivity:g}")
                if not dev.has_frame():
                    print("driver: no live lighting (update the keyboard driver)")
            else:
                if args.action == "on":
                    why = available()
                    if why:
                        raise ValueError(f"music lighting {why}")
                    if not dev.has_frame():
                        raise ValueError("the keyboard driver is too old for music lighting: "
                                         "update it (sudo system/install.sh)")
                set_enabled(args.action == "on", store)
                if args.sensitivity is not None:
                    m = store.load()
                    m.sensitivity = min(2.0, max(0.5, args.sensitivity))
                    store.save(m)
        elif args.cmd == "color":
            stop_for_manual_change()
            if args.zone == "all":
                dev.set_all(args.color)
            else:
                dev.set_zone(int(args.zone), args.color)
        elif args.cmd == "brightness":
            dev.set_brightness(args.value)
        elif args.cmd == "effect":
            stop_for_manual_change()
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
                stop_for_manual_change()
                store.get(args.name).apply(dev)
            elif args.action == "save":
                from .presets import Preset
                store.save(Preset.capture(args.name, dev, args.with_brightness))
            else:
                store.delete(args.name)
        elif args.cmd == "profile":
            if args.name:
                dev.set_thermal_profile(args.name)
            else:
                print(dev.thermal_profile())
        elif args.cmd == "maxfan":
            dev.set_max_fan(args.state == "on")
        elif args.cmd == "sound":
            from .chain import BASS_MIN, CHAIN, load_chains
            from .sound import (OFF, SoundEngine, base_presets, label, load_eq_presets,
                                speaker_sink)
            eng = SoundEngine()
            cur = eng.current()
            chains = load_chains()
            if args.action == "list":
                print("chains: " + (", ".join(f"{n} ({c.label})" for n, c in chains.items()) or "none"))
                print("easyeffects presets: off, " + ", ".join(base_presets()))
                print("equalizers: none, " + ", ".join(load_eq_presets()))
            elif args.action == "status":
                sink = speaker_sink()
                print(f"speakers: {sink[1] if sink else 'not found'}")
                if cur.engine == CHAIN:
                    c = chains.get(cur.chain) if cur.chain else next(iter(chains.values()), None)
                    print(f"engine: Prism chain ({c.label if c else 'no profile installed'})")
                    print(f"bass: {'off' if cur.chain_bass <= BASS_MIN else f'{cur.chain_bass:+g} dB'}")
                else:
                    print("engine: EasyEffects")
                    print(f"sound: {'off' if cur.base == OFF else label(cur.base)}")
                    print(f"bass: {'preset default' if cur.bass is None else cur.bass}")
                print(f"equalizer: {cur.eq or 'none'}")
            elif args.action == "toggle":
                if not chains:
                    raise ValueError("no Prism chain profile installed (see: prism sound list)")
                cur.engine = "easyeffects" if cur.engine == CHAIN else CHAIN
                print(eng.apply(cur))
            else:
                if args.engine is not None:
                    cur.engine = args.engine
                if args.chain is not None:
                    cur.chain = args.chain
                if args.base is not None:
                    cur.base = args.base
                if args.eq is not None:
                    cur.eq = None if args.eq.lower() == "none" else args.eq
                if args.bass is not None:
                    if cur.engine == CHAIN:
                        cur.chain_bass = args.bass
                    else:
                        cur.bass = args.bass
                if cur.engine != CHAIN and cur.base != OFF and cur.base not in base_presets():
                    raise ValueError(f"no EasyEffects preset named {cur.base!r} (see: prism sound list)")
                print(eng.apply(cur))
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


def battery_report(hours: float) -> int:
    import time as _t

    from . import battery as b
    i = b.info()
    if i is None:
        print("prism: no battery found", file=sys.stderr)
        return 1
    print(f"battery: {i.percent:.0f}% {i.status.lower()}"
          + (f", {i.rate_w:.1f} W" if i.rate_w else "")
          + (f", {int(i.time_left_h)} h {round(i.time_left_h % 1 * 60)} min "
             + ("left" if i.status == "Discharging" else "to full") if i.time_left_h else ""))
    if i.health is not None:
        print(f"health: {i.health:.0f}% of design ({i.full_wh:.1f} of {i.design_wh:.1f} Wh)"
              + (f", {i.cycles} cycles" if i.cycles is not None else ""))
    state = b.dgpu_state()
    if state:
        users = b.dgpu_users()
        print(f"discrete GPU: {'awake' if state == 'active' else 'asleep'}"
              + (f", in use by {', '.join(users)}" if users else "")
              + (" (sleep is off: amdgpu.runpm=0)" if b.dgpu_sleep_disabled() else ""))
    for d in b.peripherals():
        print(f"{d.name}: {d.percent:.0f}% {d.state}")
    total, apps = b.usage(b.UsageLog().records(_t.time() - hours * 3600))
    if total <= 0:
        print(f"use per app: nothing recorded on battery in the last {hours:g} h yet")
        return 0
    print(f"on battery in the last {hours:g} h: {total:.1f} Wh"
          + (f" ({100 * total / i.full_wh:.0f}% of a full charge)" if i.full_wh else ""))
    for app, wh in apps[:12]:
        print(f"  {100 * wh / total:5.1f}%  {wh:5.1f} Wh  {app}")
    return 0


def stop_for_manual_change() -> None:
    from .music import stop_for_manual_change as stop
    stop()


def sound_status() -> dict:
    """Speaker engine, for the Quick Settings tile (cheap: no PipeWire calls)."""
    from .chain import CHAIN, load_chains
    from .sound import SoundStore, easyeffects_available
    chains = load_chains()
    s = SoundStore().load()
    name = s.chain if s.chain in chains else next(iter(chains), None)
    return {"engine": s.engine, "chain_label": chains[name].label if name else None,
            "can_switch": bool(chains) and easyeffects_available()}


def main() -> int:
    return run(sys.argv[1:])
