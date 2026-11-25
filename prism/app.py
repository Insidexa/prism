"""Prism GTK4/libadwaita app: Lighting and Performance pages."""
from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

GObject_SYNC = GObject.BindingFlags.SYNC_CREATE

from . import APP_ID, __version__  # noqa: E402
from .backend import (ANIMATIONS, THERMAL_PROFILES, OmenDevice,  # noqa: E402
                      OmenError, curve_is_risky)
from .automation import (EXAMPLE_RULES, POWER_CHOICES, POWER_LABELS,  # noqa: E402
                         AutomationStore, Rule, on_ac_power, pick)
from .presets import Preset, PresetStore  # noqa: E402
from .sensors import Sensors  # noqa: E402
from .chain import BASS_MAX, BASS_MIN, CHAIN, load_chains  # noqa: E402
from .sound import (OFF, SoundEngine, base_presets, easyeffects_available,  # noqa: E402
                    easyeffects_running,
                    eq_bands_hz, label, load_eq_presets)
from .widgets import (EqBars, FanCurveEditor, KeyboardPreview, Sparkline, Swatch,  # noqa: E402
                      rgb_to_hex)

CSS = b"""
.stat-card { padding: 14px 16px; border-radius: 14px; }
.stat-value { font-size: 22pt; font-weight: 800; font-feature-settings: "tnum"; }
.stat-unit { opacity: 0.6; font-weight: 600; }
.stat-sub { opacity: 0.6; font-size: 9pt; }
.preview-frame { background: #0b0c0e; border-radius: 22px; padding: 6px; }
.preset-btn { padding: 8px 10px; }
.mode-card { padding: 16px 10px; }
.mode-card label.title { font-weight: 700; }
"""

# Driver zone numbers on the Omen 16-c0xxx, and the order to show them in.
ZONE_NAMES = {3: "WASD", 2: "Left", 1: "Middle", 0: "Right"}
ZONE_ORDER = (3, 2, 1, 0)

EFFECT_LABELS = {
    "static": "Static", "breathing": "Breathing", "rainbow": "Rainbow", "wave": "Wave",
    "pulse": "Pulse", "chase": "Chase", "sparkle": "Sparkle", "candle": "Candle",
    "aurora": "Aurora", "disco": "Disco", "gradient": "Gradient", "spectrum": "Spectrum",
}
PROFILE_INFO = {
    "silent": ("Quiet", "prism-weather-clear-night-symbolic", "Low noise, cooler, longer battery"),
    "normal": ("Balanced", "prism-power-profile-balanced-symbolic", "The firmware's default tuning"),
    "performance": ("Performance", "prism-power-profile-performance-symbolic", "Highest clocks and fan speeds"),
}


class DeviceWorker:
    """Runs driver calls on one background thread, in order.

    Firmware calls behind the driver can take hundreds of milliseconds (a fan
    speed read is ~300 ms), so they must never run on the GTK main thread.
    Results and errors come back on the main thread via GLib.idle_add.
    """

    def __init__(self, on_error):
        self._q: queue.Queue = queue.Queue()
        self._on_error = on_error
        threading.Thread(target=self._loop, name="prism-device", daemon=True).start()

    def submit(self, fn, *args, then=None):
        self._q.put((fn, args, then))

    def _loop(self):
        while True:
            fn, args, then = self._q.get()
            try:
                result = fn(*args)
            except (OmenError, ValueError) as e:
                GLib.idle_add(self._on_error, str(e))
                continue
            if then is not None:
                GLib.idle_add(lambda r=result: (then(r), False)[1])


def _run(win, fn, *args, then=None) -> None:
    """Queue a backend call; `then(result)` runs on the main thread if it succeeds."""
    win.worker.submit(fn, *args, then=then)


class LightingPage(Gtk.Box):
    def __init__(self, win: "PrismWindow"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.dev = win, win.dev
        self.store = PresetStore()
        self.zone = ZONE_ORDER[0]
        self._loading = False
        self._brightness_timer = 0

        page = Adw.PreferencesPage()
        self.append(page)

        frame = Gtk.Box(css_classes=["preview-frame"], margin_bottom=6)
        self.preview = KeyboardPreview(on_select=self._select_zone)
        frame.append(self.preview)
        hero = Adw.PreferencesGroup()
        hero.add(frame)
        hint = Gtk.Label(label="Click a key to pick its zone", css_classes=["dim-label", "caption"])
        hero.add(hint)
        page.add(hero)

        # Colour
        color_group = Adw.PreferencesGroup(title="Colour")
        self.zone_toggle = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        for zone in ZONE_ORDER:
            self.zone_toggle.add(Adw.Toggle(label=ZONE_NAMES[zone], name=str(zone)))
        self.zone_toggle.connect("notify::active-name", self._zone_toggled)
        zone_row = Adw.ActionRow(title="Zone")
        zone_row.add_suffix(self.zone_toggle)
        color_group.add(zone_row)

        self.color_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                               valign=Gtk.Align.CENTER)
        self.color_btn.connect("notify::rgba", self._color_picked)
        self.color_row = Adw.ActionRow(title="Zone colour")
        self.color_row.add_suffix(self.color_btn)
        self.color_row.set_activatable_widget(self.color_btn)
        color_group.add(self.color_row)

        all_btn = Gtk.Button(label="Use for all zones", valign=Gtk.Align.CENTER)
        all_btn.connect("clicked", self._color_to_all)
        all_row = Adw.ActionRow(title="Whole keyboard", subtitle="Copy this colour to every zone")
        all_row.add_suffix(all_btn)
        color_group.add(all_row)
        page.add(color_group)

        # Effect
        fx_group = Adw.PreferencesGroup(title="Effect")
        self.effect_row = Adw.ComboRow(title="Effect",
                                       model=Gtk.StringList.new([EFFECT_LABELS[a] for a in ANIMATIONS]))
        self.effect_row.connect("notify::selected", self._effect_changed)
        fx_group.add(self.effect_row)
        self.speed_row = Adw.SpinRow.new_with_range(1, 10, 1)
        self.speed_row.set_title("Speed")
        self.speed_row.connect("notify::value", self._speed_changed)
        fx_group.add(self.speed_row)
        self.music_row = Adw.SwitchRow(title="Music",
                                       subtitle="Each zone pulses with part of the sound: bass on the left, "
                                                "treble on the right")
        self.music_row.connect("notify::active", self._music_toggled)
        fx_group.add(self.music_row)

        self.bright_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.bright_scale.set_hexpand(True)
        self.bright_scale.set_size_request(240, -1)
        self.bright_scale.set_valign(Gtk.Align.CENTER)
        self.bright_scale.connect("value-changed", self._brightness_changed)
        bright_row = Adw.ActionRow(title="Brightness")
        bright_row.add_suffix(self.bright_scale)
        fx_group.add(bright_row)
        page.add(fx_group)

        # Presets
        self.preset_group = Adw.PreferencesGroup(title="Presets")
        save_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Save current look as a preset",
                              css_classes=["flat"])
        save_btn.connect("clicked", self._save_preset)
        self.preset_group.set_header_suffix(save_btn)
        self.preset_box = Adw.WrapBox(child_spacing=8, line_spacing=8)
        self.preset_group.add(self.preset_box)
        page.add(self.preset_group)
        self._fill_presets()

    # -- state -----------------------------------------------------------------
    def refresh(self) -> None:
        try:
            zones = self.dev.base_zones()  # as set, not dimmed by the brightness
            b, mode, speed = self.dev.brightness(), self.dev.animation(), self.dev.speed()
        except OmenError as e:
            self.win.toast(str(e))
            return
        self._loading = True
        self.zones = zones
        self.preview.update(zones, b, mode, speed)
        self.effect_row.set_selected(ANIMATIONS.index(mode) if mode in ANIMATIONS else 0)
        self.speed_row.set_value(speed)
        self.bright_scale.set_value(b)
        from .music import MusicStore, available
        why = available() or (None if self.dev.has_frame() else "needs the updated keyboard driver")
        self.music_row.set_active(MusicStore().load().enabled and why is None)
        self.music_row.set_sensitive(why is None)
        if why:
            self.music_row.set_subtitle(f"Not available: {why}")
        self._show_zone_color()
        self._loading = False

    def _show_zone_color(self):
        rgba = Gdk.RGBA()
        rgba.parse(self.zones[self.zone])
        self.color_btn.set_rgba(rgba)
        self.color_row.set_subtitle(f"{ZONE_NAMES[self.zone]} · {self.zones[self.zone].upper()}")
        self.zone_toggle.set_active_name(str(self.zone))
        self.preview.selected = self.zone
        self.preview.queue_draw()

    def _select_zone(self, zone: int):
        self.zone = zone
        was, self._loading = self._loading, True
        self._show_zone_color()
        self._loading = was

    def _zone_toggled(self, tg, _p):
        name = tg.get_active_name()
        if name is not None and int(name) != self.zone:
            self._select_zone(int(name))

    def _picked_hex(self) -> str:
        c = self.color_btn.get_rgba()
        return rgb_to_hex((c.red, c.green, c.blue))

    def _manual(self, fn, *args):
        """A lighting change by hand: music lighting ends first."""
        from .music import stop_for_manual_change

        def go():
            stop_for_manual_change()
            return fn(*args)
        _run(self.win, go, then=lambda _r: self.refresh())

    def _music_toggled(self, row, _p):
        if self._loading:
            return
        from .music import set_enabled
        _run(self.win, set_enabled, row.get_active())

    def _color_picked(self, *_):
        if self._loading:
            return
        self._manual(self.dev.set_zone, self.zone, self._picked_hex())

    def _color_to_all(self, _b):
        self._manual(self.dev.set_all, self._picked_hex())

    def _effect_changed(self, row, _p):
        if self._loading:
            return
        self._manual(self.dev.set_animation, ANIMATIONS[row.get_selected()])

    def _speed_changed(self, row, _p):
        if self._loading:
            return
        _run(self.win, self.dev.set_speed, int(row.get_value()), then=lambda _r: self.refresh())

    def _brightness_changed(self, scale):
        if self._loading:
            return
        self.preview.set_brightness(scale.get_value())  # follow the slider live
        # Coalesce slider drags into one firmware call every 80 ms.
        if self._brightness_timer:
            GLib.source_remove(self._brightness_timer)

        def commit():
            self._brightness_timer = 0
            _run(self.win, self.dev.set_brightness, int(scale.get_value()))
            return GLib.SOURCE_REMOVE
        self._brightness_timer = GLib.timeout_add(80, commit)

    # -- presets ---------------------------------------------------------------
    def _fill_presets(self):
        while (child := self.preset_box.get_first_child()) is not None:
            self.preset_box.remove(child)
        try:
            presets = self.store.all()
        except (ValueError, TypeError) as e:
            self.win.toast(f"presets.json is invalid: {e}")
            presets = []
        for p in presets:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.append(Swatch(p.zones, (96, 14)))
            fx = EFFECT_LABELS[p.animation]
            label = p.name if p.animation == "static" or fx in p.name else f"{p.name} · {fx}"
            if p.brightness is not None:
                label += f" · {p.brightness}%"
            box.append(Gtk.Label(label=label, xalign=0, css_classes=["caption"]))
            btn = Gtk.Button(child=box, css_classes=["preset-btn"])
            btn.connect("clicked", self._apply_preset, p)
            if not self.store.is_builtin(p.name):
                menu_click = Gtk.GestureClick(button=3)
                menu_click.connect("pressed", lambda *_a, name=p.name: self._delete_preset(name))
                btn.add_controller(menu_click)
                btn.set_tooltip_text("Right-click to delete")
            self.preset_box.append(btn)

    def _apply_preset(self, _b, preset: Preset):
        def done(_r):
            self.refresh()
            self.win.toast(f"Applied “{preset.name}”")
        from .music import stop_for_manual_change

        def go():
            stop_for_manual_change()
            preset.apply(self.dev)
        _run(self.win, go, then=done)

    def _save_preset(self, _b):
        entry = Adw.EntryRow(title="Name")
        keep = Adw.SwitchRow(title="Include brightness",
                             subtitle="Off: applying it keeps the brightness you have")
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        box.append(entry)
        box.append(keep)
        dialog = Adw.AlertDialog(heading="Save preset",
                                 body="Saves the current colours, effect and speed.",
                                 extra_child=box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def done(d, response):
            name = entry.get_text().strip()
            if response != "save" or not name:
                return
            try:
                self.store.save(Preset.capture(name, self.dev, keep.get_active()))
            except (OmenError, ValueError) as e:
                self.win.toast(str(e))
                return
            self._fill_presets()
            self.win.toast(f"Saved “{name}”")
        dialog.connect("response", done)
        dialog.present(self.win)

    def _delete_preset(self, name: str):
        dialog = Adw.AlertDialog(heading=f"Delete “{name}”?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(_d, response):
            if response == "delete":
                self.store.delete(name)
                self._fill_presets()
        dialog.connect("response", done)
        dialog.present(self.win)


class StatCard(Gtk.Box):
    def __init__(self, title: str, unit: str, lo=0.0, hi=100.0):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                         css_classes=["card", "stat-card"], hexpand=True)
        self.append(Gtk.Label(label=title, xalign=0, css_classes=["caption-heading", "dim-label"]))
        row = Gtk.Box(spacing=4)
        self.value = Gtk.Label(label="–", xalign=0, css_classes=["stat-value"])
        row.append(self.value)
        row.append(Gtk.Label(label=unit, valign=Gtk.Align.END, margin_bottom=6, css_classes=["stat-unit"]))
        self.append(row)
        self.sub = Gtk.Label(label=" ", xalign=0, css_classes=["stat-sub"])
        self.append(self.sub)
        self.spark = Sparkline(lo=lo, hi=hi)
        self.append(self.spark)

    def set(self, value: float | None, fmt="{:.0f}", sub: str | None = None):
        self.value.set_label("–" if value is None else fmt.format(value))
        if sub is not None:
            self.sub.set_label(sub)
        self.spark.push(value)


class PerformancePage(Gtk.Box):
    def __init__(self, win: "PrismWindow"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.dev = win, win.dev
        self.sensors = Sensors()
        self._loading = False
        self._pending_curve: list | None = None

        page = Adw.PreferencesPage()
        self.append(page)

        # Mode cards
        mode_group = Adw.PreferencesGroup(title="Mode",
                                          description="Also follows GNOME's Power Mode setting")
        modes = Gtk.Box(spacing=10, homogeneous=True)
        self.mode_buttons: dict[str, Gtk.ToggleButton] = {}
        first = None
        for name in THERMAL_PROFILES:
            title, icon, desc = PROFILE_INFO[name]
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.append(Gtk.Image(icon_name=icon, pixel_size=28))
            box.append(Gtk.Label(label=title, css_classes=["title"]))
            box.append(Gtk.Label(label=desc, wrap=True, justify=Gtk.Justification.CENTER,
                                 css_classes=["caption", "dim-label"]))
            btn = Gtk.ToggleButton(child=box, css_classes=["mode-card"], group=first)
            first = first or btn
            btn.connect("toggled", self._mode_toggled, name)
            self.mode_buttons[name] = btn
            modes.append(btn)
        mode_group.add(modes)
        page.add(mode_group)

        # Live stats
        stats_group = Adw.PreferencesGroup(title="Live")
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True)
        self.cpu_temp = StatCard("CPU temperature", "°C", 30, 100)
        self.cpu_load = StatCard("CPU load", "%")
        self.gpu_temp = StatCard("GPU temperature", "°C", 30, 100)
        self.fans = StatCard("Fans", "rpm", 0, 6000)
        for i, c in enumerate((self.cpu_temp, self.cpu_load, self.gpu_temp, self.fans)):
            grid.attach(c, i % 2, i // 2, 1, 1)
        stats_group.add(grid)
        page.add(stats_group)

        # Fans
        fan_group = Adw.PreferencesGroup(title="Fans")
        self.max_fan_row = Adw.SwitchRow(title="Max fans", subtitle="Run both fans at full speed")
        self.max_fan_row.connect("notify::active", self._max_fan_changed)
        fan_group.add(self.max_fan_row)
        self.curve_row = Adw.SwitchRow(title="Custom fan curve",
                                       subtitle="Off: the laptop's firmware controls the fans")
        self.curve_row.connect("notify::active", self._curve_enabled_changed)
        fan_group.add(self.curve_row)
        page.add(fan_group)

        curve_group = Adw.PreferencesGroup(
            title="CPU fan curve",
            description="Drag points to shape it · click to add · right-click to remove")
        self.risk = Adw.Banner(title="Fans stay slow when the CPU is hot. This can cause throttling.")
        curve_group.add(self.risk)
        self.editor = FanCurveEditor(self._curve_edited)
        curve_frame = Gtk.Box(css_classes=["card"], margin_top=6)
        curve_frame.append(self.editor)
        curve_group.add(curve_frame)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8)
        reset = Gtk.Button(label="Revert")
        reset.connect("clicked", lambda *_: self.refresh())
        self.apply_curve = Gtk.Button(label="Apply curve", css_classes=["suggested-action"], sensitive=False)
        self.apply_curve.connect("clicked", self._apply_curve)
        actions.append(reset)
        actions.append(self.apply_curve)
        curve_group.add(actions)
        page.add(curve_group)

        # Advanced
        adv = Adw.PreferencesGroup(title="Advanced")
        exp = Adw.ExpanderRow(title="CPU power limits",
                              subtitle="Sustained (PL1) and boost (PL2) power, in watts")
        self.pl1 = Adw.SpinRow.new_with_range(15, 90, 1)
        self.pl1.set_title("PL1 · sustained")
        self.pl2 = Adw.SpinRow.new_with_range(15, 90, 1)
        self.pl2.set_title("PL2 · boost")
        self.pl1.set_value(45)
        self.pl2.set_value(54)
        exp.add_row(self.pl1)
        exp.add_row(self.pl2)
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=6, margin_bottom=6, margin_end=12)
        d = Gtk.Button(label="Firmware default")
        d.connect("clicked", lambda *_: self._set_power(None))
        a = Gtk.Button(label="Apply", css_classes=["suggested-action"])
        a.connect("clicked", lambda *_: self._set_power((int(self.pl1.get_value()), int(self.pl2.get_value()))))
        btns.append(d)
        btns.append(a)
        exp.add_row(btns)
        self.pl_row = exp
        adv.add(exp)
        page.add(adv)

        self._ticks = 0
        self._fan_pending = False
        self.sensors.cpu_percent()
        GLib.timeout_add_seconds(1, self._tick)

    def refresh(self) -> None:
        try:
            profile = self.dev.thermal_profile()
            max_fan = self.dev.max_fan()
            enabled = self.dev.curve_enabled()
            curve = self.dev.fan_curve()
            pl = self.dev.power_limits()
        except OmenError as e:
            self.win.toast(str(e))
            return
        self._loading = True
        if profile in self.mode_buttons:
            self.mode_buttons[profile].set_active(True)
        self.max_fan_row.set_active(max_fan)
        self.curve_row.set_active(enabled)
        self.editor.set_points(curve)
        self._pending_curve = None
        self.apply_curve.set_sensitive(False)
        self.risk.set_revealed(bool(curve) and curve_is_risky(curve))
        self.pl_row.set_subtitle("Firmware default" if pl.pl1 is None else f"{pl.pl1} W / {pl.pl2} W")
        self._loading = False

    def _tick(self):
        # Nothing to show when the page is hidden, or the window minimized or
        # covered (GTK keeps those mapped): skip the sensor and firmware reads.
        if not self.get_mapped() or self.win.is_suspended():
            return GLib.SOURCE_CONTINUE
        t = self.sensors.cpu_temp_c()
        self.cpu_temp.set(t)
        # Mark the temperature the driver's curve actually follows (acpitz,
        # slower and often a few degrees off the CPU's own sensor).
        if not hasattr(self, "_curve_zone"):
            self._curve_zone = self.dev.fan_temp_zone()
        from .sensors import thermal_zone_temp_c
        zt = thermal_zone_temp_c(self._curve_zone) if self._curve_zone else None
        self.editor.current_temp = zt if zt is not None else t
        self.editor.queue_draw()
        self.cpu_load.set(self.sensors.cpu_percent())
        g = self.sensors.dgpu or (self.sensors.gpus[0] if self.sensors.gpus else None)
        if g is None:
            self.gpu_temp.set(None, sub="no GPU sensor")
        elif g.asleep():
            self.gpu_temp.set(None, sub=f"{g.name} · asleep (saving power)")
        else:
            busy = g.busy_percent()
            self.gpu_temp.set(g.temp_c(), sub=f"{g.name} · {busy if busy is not None else '–'}% busy")
        # Fan speeds come from firmware (~300 ms of kernel time each): read them
        # in the background, at most one read in flight, every other tick, and
        # every fifth on battery.
        self._ticks += 1
        from .automation import on_ac_power
        every = 2 if on_ac_power() else 5
        if not self._fan_pending and self._ticks % every == 0:
            self._fan_pending = True
            self.win.worker.submit(self._read_fans, then=self._show_fans)
        return GLib.SOURCE_CONTINUE

    def _read_fans(self):
        try:
            return self.dev.fan_rpm(), self.dev.thermal_profile()
        except OmenError:
            return None

    def _show_fans(self, result):
        self._fan_pending = False
        if result is None:
            self.fans.set(None, sub="unavailable")
            return
        (cpu, gpu), profile = result
        self.fans.set(max(cpu, gpu), "{:,.0f}", sub=f"CPU {cpu:,} · GPU {gpu:,}")
        if profile in self.mode_buttons and not self.mode_buttons[profile].get_active():
            self._loading = True  # changed from GNOME's Power Mode
            self.mode_buttons[profile].set_active(True)
            self._loading = False

    def _mode_toggled(self, btn, name):
        if self._loading or not btn.get_active():
            return
        def done(_r):
            self.win.toast(f"{PROFILE_INFO[name][0]} mode")
            self.refresh()
        _run(self.win, self.dev.set_thermal_profile, name, then=done)

    def _max_fan_changed(self, row, _p):
        if not self._loading:
            _run(self.win, self.dev.set_max_fan, row.get_active())

    def _curve_enabled_changed(self, row, _p):
        if not self._loading:
            _run(self.win, self.dev.set_curve_enabled, row.get_active())

    def _curve_edited(self, points):
        self._pending_curve = points
        self.apply_curve.set_sensitive(True)
        self.risk.set_revealed(curve_is_risky(points))

    def _apply_curve(self, _b):
        if not self._pending_curve:
            return

        def done(_r):
            self.win.toast("Fan curve applied")
            self.refresh()
        _run(self.win, self.dev.set_fan_curve, self._pending_curve, then=done)

    def _set_power(self, limits):
        args = (None, None) if limits is None else limits
        def done(_r):
            self.win.toast("Power limits set to firmware default" if limits is None
                           else f"Power limits: {limits[0]} W / {limits[1]} W")
            self.refresh()
        _run(self.win, self.dev.set_power_limits, *args, then=done)


class AutomationPage(Gtk.Box):
    """Rules that switch presets by time of day and power source."""

    def __init__(self, win: "PrismWindow"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.store = AutomationStore()
        self.presets = PresetStore()
        self.enabled, self.rules = False, []
        self._loading = False

        page = Adw.PreferencesPage()
        self.append(page)

        top = Adw.PreferencesGroup(
            description="Rules are checked from the top; the first that matches picks the preset. "
                        "Colours you change by hand stay until a different rule starts to apply.")
        self.enable_row = Adw.SwitchRow(title="Switch presets automatically",
                                        subtitle="By time of day and whether the laptop is plugged in")
        self.enable_row.connect("notify::active", self._toggled)
        top.add(self.enable_row)
        self.now_row = Adw.ActionRow(title="Right now")
        top.add(self.now_row)
        page.add(top)

        self.rules_group = Adw.PreferencesGroup(title="Rules")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add rule", css_classes=["flat"])
        add.connect("clicked", lambda *_: self._edit(None))
        self.rules_group.set_header_suffix(add)
        page.add(self.rules_group)
        self._rows: list[Gtk.Widget] = []

        from .power import KEEP, PLANS, PlanStore
        self.plans = PlanStore()
        self.plan_keys = [KEEP] + list(PLANS)
        labels = ["Keep as it is"] + [p.label for p in PLANS.values()]
        plan_group = Adw.PreferencesGroup(
            title="Power plans",
            description="Applied when you plug in or unplug. Battery saver: GNOME's power saver mode "
                        "(quiet fans, lower CPU and GPU power), the screen at 60 Hz and music lighting off. "
                        "The discrete GPU sleeps by itself when nothing uses it, unless sleep is turned off for it (amdgpu.runpm=0): see the Battery page.")
        self.plan_rows = {}
        for key, title in (("battery", "On battery"), ("ac", "Plugged in")):
            row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
            row.connect("notify::selected", self._plan_changed, key)
            plan_group.add(row)
            self.plan_rows[key] = row
        page.add(plan_group)

        GLib.timeout_add_seconds(15, self._tick)

    def _plan_changed(self, row, _p, key):
        if self._loading:
            return
        d = self.plans.load()
        d[key] = self.plan_keys[row.get_selected()]
        self.plans.save(d)

    def refresh(self) -> None:
        try:
            self.enabled, self.rules = self.store.load()
        except (ValueError, TypeError) as e:
            self.win.toast(f"automation.json is invalid: {e}")
            return
        self._loading = True
        plans = self.plans.load()
        for key, row in self.plan_rows.items():
            row.set_selected(self.plan_keys.index(plans[key]))
        self.enable_row.set_active(self.enabled)
        self._loading = False
        for row in self._rows:
            self.rules_group.remove(row)
        self._rows = []
        if not self.rules:
            empty = Adw.ActionRow(title="No rules yet",
                                  subtitle="Example: dim “Battery Saver” on battery, warm “Night” from 22:00 to 07:00")
            btn = Gtk.Button(label="Add examples", valign=Gtk.Align.CENTER)
            btn.connect("clicked", self._add_examples)
            empty.add_suffix(btn)
            self._add_row(empty)
        names = {p.name for p in self.presets.all()}
        for i, rule in enumerate(self.rules):
            row = Adw.ActionRow(title=GLib.markup_escape_text(f"{i + 1}. {rule.preset}"),
                                subtitle=rule.describe() + ("" if rule.preset in names else " · preset missing"),
                                activatable=True)
            row.connect("activated", lambda _r, i=i: self._edit(i))
            for icon, tip, cb, ok in (
                    ("prism-go-up-symbolic", "Move up", self._move, i > 0),
                    ("prism-go-down-symbolic", "Move down", self._move, i < len(self.rules) - 1),
                    ("prism-trash-symbolic", "Delete", self._delete, True)):
                b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER,
                               css_classes=["flat"], sensitive=ok)
                if tip == "Move up":
                    b.connect("clicked", lambda _b, i=i: cb(i, -1))
                elif tip == "Move down":
                    b.connect("clicked", lambda _b, i=i: cb(i, 1))
                else:
                    b.connect("clicked", lambda _b, i=i: cb(i))
                row.add_suffix(b)
            self._add_row(row)
        self._tick()

    def _add_row(self, row):
        self.rules_group.add(row)
        self._rows.append(row)

    def _tick(self):
        from datetime import datetime
        now = datetime.now()
        ac = on_ac_power()
        hit = pick(self.rules, now.hour * 60 + now.minute, ac)
        where = f"{'Plugged in' if ac else 'On battery'} · {now:%H:%M}"
        if not self.enabled:
            what = "automatic presets are off"
        elif hit:
            what = f"rule {hit[0] + 1} → {hit[1].preset}"
        else:
            what = "no rule matches, lighting is left as is"
        self.now_row.set_subtitle(GLib.markup_escape_text(f"{where} · {what}"))
        return GLib.SOURCE_CONTINUE

    def _save(self) -> bool:
        try:
            self.store.save(self.enabled, self.rules)
        except ValueError as e:
            self.win.toast(str(e))
            return False
        self.refresh()
        return True

    def _toggled(self, row, _p):
        if self._loading:
            return
        self.enabled = row.get_active()
        if self._save():
            self.win.toast("Automatic presets on" if self.enabled else "Automatic presets off")

    def _add_examples(self, _b):
        self.rules = [Rule(**vars(r)) for r in EXAMPLE_RULES]
        self.enabled = True
        self._save()

    def _move(self, i, d):
        self.rules[i], self.rules[i + d] = self.rules[i + d], self.rules[i]
        self._save()

    def _delete(self, i):
        del self.rules[i]
        self._save()

    def _edit(self, index: int | None):
        rule = self.rules[index] if index is not None else Rule(self.presets.all()[0].name)
        names = [p.name for p in self.presets.all()]
        if rule.preset not in names:
            names.append(rule.preset)

        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        preset_row = Adw.ComboRow(title="Preset", model=Gtk.StringList.new(names))
        preset_row.set_selected(names.index(rule.preset))
        power_row = Adw.ComboRow(title="When",
                                 model=Gtk.StringList.new([POWER_LABELS[p] for p in POWER_CHOICES]))
        power_row.set_selected(POWER_CHOICES.index(rule.power))
        timed = Adw.SwitchRow(title="Only between certain times", active=rule.start is not None)
        start = Adw.EntryRow(title="From (HH:MM)", text=rule.start or "22:00")
        end = Adw.EntryRow(title="To (HH:MM)", text=rule.end or "07:00")
        for r in (start, end):
            timed.bind_property("active", r, "visible", GObject_SYNC)
        for r in (preset_row, power_row, timed, start, end):
            box.append(r)

        dialog = Adw.AlertDialog(heading="Edit rule" if index is not None else "New rule",
                                 extra_child=box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def done(_d, response):
            if response != "save":
                return
            new = Rule(names[preset_row.get_selected()], POWER_CHOICES[power_row.get_selected()],
                       start.get_text().strip() if timed.get_active() else None,
                       end.get_text().strip() if timed.get_active() else None)
            try:
                new.validate()
            except ValueError as e:
                self.win.toast(str(e))
                return
            old = list(self.rules)
            if index is None:
                self.rules.append(new)
            else:
                self.rules[index] = new
            if not self._save():
                self.rules = old
        dialog.connect("response", done)
        dialog.present(self.win)


class SoundPage(Gtk.Box):
    """Speaker sound: the Prism chain (PipeWire) or EasyEffects, OMEN equalizer, bass."""

    ENGINES = [(CHAIN, "Prism chain", "Runs inside PipeWire: no extra app, nothing to crash"),
               ("easyeffects", "EasyEffects", "An EasyEffects preset, applied by the EasyEffects app")]

    def __init__(self, win: "PrismWindow"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.engine = SoundEngine()
        self._loading = False
        self._bass_timer = 0
        self.chains = load_chains()
        self.engines = [e for e in self.ENGINES
                        if (e[0] == CHAIN and self.chains) or (e[0] != CHAIN and easyeffects_available())]
        page = Adw.PreferencesPage()
        self.append(page)

        if not self.engines:
            page.add(Adw.PreferencesGroup())
            self.append(Adw.StatusPage(
                icon_name="prism-speaker-symbolic", title="No speaker tuning installed",
                description="Install a Prism chain profile (see the README), or EasyEffects:\n"
                            "<tt>sudo dnf install easyeffects</tt>"))
            page.set_visible(False)
            return

        main = Adw.PreferencesGroup(
            title="Built-in speakers",
            description="Applied to the laptop speakers only. "
                        "Headphones, HDMI and Bluetooth are left untouched.")
        self.engine_row = Adw.ComboRow(title="Engine",
                                       model=Gtk.StringList.new([e[1] for e in self.engines]))
        self.engine_row.connect("notify::selected", self._engine_changed)
        main.add(self.engine_row)
        if len(self.engines) < 2:
            self.engine_row.set_visible(False)

        self.bases: list[str] = []
        self.base_row = Adw.ComboRow(title="Sound")
        self.base_row.connect("notify::selected", self._changed)
        main.add(self.base_row)

        self.eq_names = list(load_eq_presets())
        self.eq_row = Adw.ComboRow(title="Equalizer", subtitle="OMEN Audio Control presets",
                                   model=Gtk.StringList.new(["None"] + self.eq_names))
        self.eq_row.connect("notify::selected", self._changed)
        main.add(self.eq_row)

        self.bass = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 8, 0.5)
        self.bass.set_hexpand(True)
        self.bass.set_size_request(220, -1)
        self.bass.set_valign(Gtk.Align.CENTER)
        self.bass.set_draw_value(True)
        self.bass.set_value_pos(Gtk.PositionType.LEFT)
        self.bass.connect("value-changed", self._bass_moved)
        reset = Gtk.Button(icon_name="prism-undo-symbolic", tooltip_text="Back to the tuning's own bass",
                           valign=Gtk.Align.CENTER, css_classes=["flat"])
        reset.connect("clicked", self._bass_reset)
        self.bass_row = Adw.ActionRow(title="Bass")
        self.bass_row.add_suffix(self.bass)
        self.bass_row.add_suffix(reset)
        main.add(self.bass_row)
        self.status_row = Adw.ActionRow(title="Status")
        main.add(self.status_row)
        page.add(main)

        self.eq_group = Adw.PreferencesGroup(title="Equalizer curve")
        self.bars = EqBars()
        frame = Gtk.Box(css_classes=["card"])
        frame.append(self.bars)
        self.eq_group.add(frame)
        page.add(self.eq_group)
        if not self.eq_names:
            self.eq_row.set_visible(False)
            self.eq_group.set_visible(False)

    # ------------------------------------------------------------------ helpers
    def _engine(self) -> str:
        return self.engines[self.engine_row.get_selected()][0]

    def _fill_for_engine(self, cur) -> None:
        """Sound list, bass range and texts for the selected engine (no applying)."""
        chain = self._engine() == CHAIN
        self.engine_row.set_subtitle(self.engines[self.engine_row.get_selected()][2])
        if chain:
            self.bases = list(self.chains)
            self.base_row.set_model(Gtk.StringList.new([self.chains[n].label for n in self.bases]))
            name = cur.chain if cur.chain in self.chains else self.bases[0]
            self.base_row.set_selected(self.bases.index(name))
            self.bass.set_range(BASS_MIN, BASS_MAX)
            self.bass.set_value(cur.chain_bass)
            self.bass_row.set_subtitle("Prism Bass, dB from the tuning (far left = off)")
        else:
            self.bases = base_presets()
            self.base_row.set_model(Gtk.StringList.new(["Off"] + [label(b) for b in self.bases]))
            self.base_row.set_selected(0 if cur.base == OFF or cur.base not in self.bases
                                       else self.bases.index(cur.base) + 1)
            self.bass.set_range(0, 8)
            self.bass.set_value(cur.bass if cur.bass is not None else self._preset_bass(cur.base))
            self.bass_row.set_subtitle("Virtual bass: harmonics the small speakers can play")

    def refresh(self) -> None:
        if not hasattr(self, "base_row"):
            return
        self._loading = True
        cur = self.engine.current()
        names = [e[0] for e in self.engines]
        self.engine_row.set_selected(names.index(cur.engine) if cur.engine in names else 0)
        self._fill_for_engine(cur)
        self.eq_row.set_selected(self.eq_names.index(cur.eq) + 1 if cur.eq in self.eq_names else 0)
        self._sync_sensitivity()
        self._loading = False
        self._show_status(cur)

    def _show_status(self, cur) -> None:
        if cur.engine == CHAIN:
            from .chain import NODE, node_id
            self.win.worker.submit(lambda: node_id(NODE) is not None, then=lambda up: self.status_row.set_subtitle(
                "Prism chain is playing on the speakers" if up
                else "Prism chain is not running: it starts when the speakers are the output"))
        else:
            self.win.worker.submit(easyeffects_running, then=lambda up: self.status_row.set_subtitle(
                "EasyEffects is running" if up else "EasyEffects is not running: it starts on the next change"))

    def _preset_bass(self, base: str) -> float:
        from .sound import base_bass
        try:
            v = base_bass(self.engine.read_preset(base)) if base != OFF else None
        except (OSError, ValueError):
            v = None
        return v if v is not None else 0.0

    def _sync_sensitivity(self):
        on = self._engine() == CHAIN or self.base_row.get_selected() > 0
        self.eq_row.set_sensitive(on)
        self.bass_row.set_sensitive(on)
        sel = self.eq_row.get_selected()
        gains = load_eq_presets().get(self.eq_names[sel - 1]) if sel > 0 else None
        self.bars.set(eq_bands_hz(), gains)

    def _settings(self, bass_from_slider: bool):
        cur = self.engine.current()
        cur.engine = self._engine()
        i = self.base_row.get_selected()
        if cur.engine == CHAIN:
            cur.chain = self.bases[i] if 0 <= i < len(self.bases) else None
            if bass_from_slider:
                cur.chain_bass = float(self.bass.get_value())
        else:
            cur.base = OFF if i <= 0 else self.bases[i - 1]
            if bass_from_slider:
                cur.bass = float(self.bass.get_value())
        j = self.eq_row.get_selected()
        cur.eq = self.eq_names[j - 1] if j > 0 else None
        return cur

    def _apply(self, settings):
        self.status_row.set_subtitle("Applying…")

        def done(msg):
            self.status_row.set_subtitle(GLib.markup_escape_text(msg[:1].upper() + msg[1:]))  # "B&O"
        self.win.worker.submit(self.engine.apply, settings, then=done)

    # ------------------------------------------------------------------ events
    def _engine_changed(self, *_):
        if self._loading:
            return
        cur = self.engine.current()
        cur.engine = self._engine()
        self._loading = True
        self._fill_for_engine(cur)
        self._loading = False
        self._sync_sensitivity()
        self._apply(self._settings(bass_from_slider=False))

    def _changed(self, *_):
        if self._loading:
            return
        s = self._settings(bass_from_slider=False)
        if s.engine != CHAIN and s.base != self.engine.current().base:
            s.bass = None  # new tuning: start from its own bass
            self._loading = True
            self.bass.set_value(self._preset_bass(s.base))
            self._loading = False
        self._sync_sensitivity()
        self._apply(s)

    def _bass_moved(self, _scale):
        if self._loading:
            return
        if self._bass_timer:
            GLib.source_remove(self._bass_timer)

        def commit():
            self._bass_timer = 0
            self._apply(self._settings(bass_from_slider=True))
            return GLib.SOURCE_REMOVE
        self._bass_timer = GLib.timeout_add(300, commit)

    def _bass_reset(self, _b):
        s = self._settings(bass_from_slider=False)
        self._loading = True
        if s.engine == CHAIN:
            s.chain_bass = 0.0
            self.bass.set_value(0.0)
        else:
            s.bass = None
            self.bass.set_value(self._preset_bass(s.base))
        self._loading = False
        self._apply(s)


class BatteryPage(Gtk.Box):
    """Battery status, charge history and estimated use per app (prism/battery.py)."""

    def __init__(self, win: "PrismWindow"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        page = Adw.PreferencesPage()
        self.append(page)

        now = Adw.PreferencesGroup(title="Battery")
        self.charge_row = Adw.ActionRow(title="Charge")
        self.left_row = Adw.ActionRow(title="Time left")
        self.health_row = Adw.ActionRow(title="Health")
        self.gpu_row = Adw.ActionRow(title="Discrete GPU")
        for r in (self.charge_row, self.left_row, self.health_row, self.gpu_row):
            now.add(r)
        page.add(now)

        from . import charge as _c
        self._charge = _c
        care = Adw.PreferencesGroup(
            title="Battery care",
            description="Stop charging at a lower level while plugged in: the battery wears much more "
                        "slowly near 80% than at 100%. Charging starts again 5% below the limit, and "
                        "unplugged the full charge is always used.")
        self.limit_values = [None, *_c.LIMITS]
        self.limit_row = Adw.ComboRow(
            title="Stop charging at",
            model=Gtk.StringList.new(["100% (off)", *[f"{v}%" for v in _c.LIMITS]]))
        lim = _c.ChargeStore().load().limit
        self.limit_row.set_selected(self.limit_values.index(lim) if lim in self.limit_values else 0)
        self.limit_row.connect("notify::selected", self._limit_changed)
        care.add(self.limit_row)
        care.set_visible(_c.available())
        page.add(care)

        hist = Adw.PreferencesGroup(title="Last 24 hours", description="Charge, from GNOME's power history")
        self.chart = Sparkline(length=200, lo=0, hi=100)
        self.chart.set_content_height(70)
        self.chart.set_margin_top(10)
        self.chart.set_margin_bottom(10)
        self.chart.set_margin_start(12)
        self.chart.set_margin_end(12)
        card = Gtk.Box(css_classes=["card"])
        card.append(self.chart)
        hist.add(card)
        page.add(hist)

        self.devices = Adw.PreferencesGroup(
            title="Devices",
            description="Mice, keyboards and headphones. Logitech devices on a Bolt receiver "
                        "show up once Solaar is installed (sudo dnf install solaar).")
        page.add(self.devices)
        self._dev_rows: list[Gtk.Widget] = []

        self.apps = Adw.PreferencesGroup(
            title="Use per app",
            description="On battery in the last 24 hours. Estimated from what each app did, as macOS does: "
                        "no laptop measures power per app.")
        page.add(self.apps)
        self._app_rows: list[Gtk.Widget] = []

    def _limit_changed(self, row, _p) -> None:
        store = self._charge.ChargeStore()
        s = store.load()
        s.limit = self.limit_values[row.get_selected()]
        store.save(s)
        self.win.toast("Charge limit off" if s.limit is None
                       else f"Charging stops at {s.limit}% while plugged in")

    def refresh(self) -> None:
        from . import battery as b
        import time as _t

        def gather():
            return (b.info(), b.history("charge", 24, 200), b.dgpu_state(), b.dgpu_users(),
                    b.usage(b.UsageLog().records(_t.time() - 86400)))
        self.win.worker.submit(gather, then=self._show)
        # Device batteries can take seconds (Solaar): their own thread, not the shared worker.
        threading.Thread(target=lambda: GLib.idle_add(self._show_devices, b.peripherals()),
                         daemon=True).start()

    def _show(self, data) -> None:
        i, hist, gpu, users, (total, apps) = data
        self._show_data(i, hist, gpu, users, total, apps)

    def _show_devices(self, devs) -> bool:
        for r in self._dev_rows:
            self.devices.remove(r)
        self._dev_rows = []
        for d in devs or []:
            row = Adw.ActionRow(title=GLib.markup_escape_text(d.name),
                                subtitle=f"{d.percent:.0f} % · {d.state}" if d.percent is not None else d.state)
            bar = Gtk.LevelBar(min_value=0, max_value=100, value=d.percent or 0, valign=Gtk.Align.CENTER)
            bar.set_size_request(160, -1)
            row.add_suffix(bar)
            self.devices.add(row)
            self._dev_rows.append(row)
        if not self._dev_rows:
            row = Adw.ActionRow(title="No device batteries found")
            self.devices.add(row)
            self._dev_rows.append(row)
        return False

    def _show_data(self, i, hist, gpu, users, total, apps) -> None:
        if i is None:
            self.charge_row.set_subtitle("No battery found")
            return
        rate = f" · {i.rate_w:.1f} W" if i.rate_w else ""
        self.charge_row.set_subtitle(f"{i.percent:.0f} % · {i.status.lower()}{rate}")
        if i.time_left_h:
            h, m = int(i.time_left_h), round(i.time_left_h % 1 * 60)
            self.left_row.set_subtitle(f"{h} h {m} min " + ("left" if i.status == "Discharging" else "to full"))
        else:
            self.left_row.set_subtitle("—")
        if i.health is not None:
            self.health_row.set_subtitle(f"{i.health:.0f} % of its design capacity ({i.full_wh:.1f} of "
                                         f"{i.design_wh:.1f} Wh)" + (f" · {i.cycles} cycles" if i.cycles else ""))
        if gpu is None:
            self.gpu_row.set_visible(False)
        else:
            text = "Asleep (good for the battery)" if gpu != "active" else "Awake"
            if users:
                text += " · in use by " + ", ".join(users)
            self.gpu_row.set_subtitle(GLib.markup_escape_text(text))
        self.chart.values.clear()
        pts = [v for _t, v in hist][-200:]
        for _ in range(200 - len(pts)):
            self.chart.values.append(None)
        for v in pts:
            self.chart.values.append(v)
        self.chart.queue_draw()
        for r in self._app_rows:
            self.apps.remove(r)
        self._app_rows = []
        if total <= 0:
            row = Adw.ActionRow(title="Nothing recorded on battery yet",
                                subtitle="Prism notes app activity once a minute while unplugged.")
            self.apps.add(row)
            self._app_rows.append(row)
            return
        for app, wh in apps[:10]:
            share = wh / total
            row = Adw.ActionRow(title=GLib.markup_escape_text(app),
                                subtitle=f"{100 * share:.0f} % · {wh:.1f} Wh")
            bar = Gtk.LevelBar(min_value=0, max_value=1, value=share, valign=Gtk.Align.CENTER)
            bar.set_size_request(160, -1)
            row.add_suffix(bar)
            self.apps.add(row)
            self._app_rows.append(row)


class PrismWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Prism", default_width=860, default_height=900)
        self.dev = OmenDevice()
        self.toasts = Adw.ToastOverlay()
        self.worker = DeviceWorker(lambda msg: (self.toast(msg), False)[1])
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view.add_top_bar(header)

        self.banner = Adw.Banner(
            title="Read-only: run  sudo system/install.sh  from the Prism folder once to enable changes")
        view.add_top_bar(self.banner)

        if not self.dev.present():
            view.set_content(Adw.StatusPage(
                icon_name="prism-input-keyboard-symbolic", title="Omen driver not loaded",
                description="Prism needs the omen_rgb_keyboard driver.\n"
                            "Run <tt>sudo system/install.sh</tt> from the Prism folder."))
            self.toasts.set_child(view)
            self.set_content(self.toasts)
            return

        stack = Adw.ViewStack()
        self.lighting = LightingPage(self)
        stack.add_titled_with_icon(self.lighting, "lighting", "Lighting", "prism-keyboard-brightness-symbolic")
        if self.dev.has_fan():
            self.performance = PerformancePage(self)
            stack.add_titled_with_icon(self.performance, "performance", "Performance",
                                       "prism-power-profile-performance-symbolic")
        else:
            self.performance = None
        self.sound = SoundPage(self)
        stack.add_titled_with_icon(self.sound, "sound", "Sound", "prism-speaker-symbolic")
        self.automation = AutomationPage(self)
        stack.add_titled_with_icon(self.automation, "automation", "Automation", "prism-time-symbolic")
        self.battery = BatteryPage(self)
        stack.add_titled_with_icon(self.battery, "battery", "Battery", "prism-battery-symbolic")
        header.set_title_widget(Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        menu = Gio.Menu()
        menu.append("About Prism", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        view.set_content(stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        self.refresh()
        # Pick up changes made elsewhere (Quick Settings, GNOME Power Mode).
        self.connect("notify::is-active", lambda w, _p: w.is_active() and w.refresh())

    def refresh(self):
        self.banner.set_revealed(not self.dev.writable())
        self.lighting.refresh()
        if self.performance:
            self.performance.refresh()
        self.automation.refresh()
        self.sound.refresh()
        self.battery.refresh()

    def toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))


class PrismApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        about = Gio.SimpleAction(name="about")
        about.connect("activate", self._about)
        self.add_action(about)

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(
            str(Path(__file__).resolve().parent.parent / "data" / "icons"))

    def do_activate(self):
        win = self.props.active_window or PrismWindow(self)
        win.present()

    def _about(self, *_):
        Adw.AboutDialog(application_name="Prism", application_icon=APP_ID, version=__version__,
                        comments="Lighting and performance control for HP Omen laptops on Linux.\n"
                                 "Uses the community omen-rgb-keyboard driver.",
                        license_type=Gtk.License.GPL_3_0).present(self.props.active_window)


def main() -> int:
    return PrismApp().run(sys.argv[:1])
