"""Prism GTK4/libadwaita app: Lighting and Performance pages."""
from __future__ import annotations

import sys
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
from .widgets import (FanCurveEditor, KeyboardPreview, Sparkline, Swatch,  # noqa: E402
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


def _run(win, fn, *args) -> bool:
    """Call a backend setter, showing a toast instead of raising."""
    try:
        fn(*args)
        return True
    except (OmenError, ValueError) as e:
        win.toast(str(e))
        return False


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
            zones = self.dev.zones()
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

    def _color_picked(self, *_):
        if self._loading:
            return
        color = self._picked_hex()
        if _run(self.win, self.dev.set_zone, self.zone, color):
            self.refresh()

    def _color_to_all(self, _b):
        if _run(self.win, self.dev.set_all, self._picked_hex()):
            self.refresh()

    def _effect_changed(self, row, _p):
        if self._loading:
            return
        if _run(self.win, self.dev.set_animation, ANIMATIONS[row.get_selected()]):
            self.refresh()

    def _speed_changed(self, row, _p):
        if self._loading:
            return
        if _run(self.win, self.dev.set_speed, int(row.get_value())):
            self.refresh()

    def _brightness_changed(self, scale):
        if self._loading:
            return
        # Coalesce slider drags into one firmware call every 80 ms.
        if self._brightness_timer:
            GLib.source_remove(self._brightness_timer)

        def commit():
            self._brightness_timer = 0
            if _run(self.win, self.dev.set_brightness, int(scale.get_value())):
                self.preview.brightness = scale.get_value() / 100
                self.preview.queue_draw()
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
        if _run(self.win, preset.apply, self.dev):
            self.refresh()
            self.win.toast(f"Applied “{preset.name}”")

    def _save_preset(self, _b):
        entry = Adw.EntryRow(title="Name")
        dialog = Adw.AlertDialog(heading="Save preset",
                                 body="Saves the current colours, effect, speed and brightness.",
                                 extra_child=entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def done(d, response):
            name = entry.get_text().strip()
            if response != "save" or not name:
                return
            try:
                self.store.save(Preset.capture(name, self.dev))
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
        if not self.get_mapped():
            return GLib.SOURCE_CONTINUE
        t = self.sensors.cpu_temp_c()
        self.cpu_temp.set(t)
        self.editor.current_temp = t
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
        try:
            cpu, gpu = self.dev.fan_rpm()
            self.fans.set(max(cpu, gpu), "{:,.0f}", sub=f"CPU {cpu:,} · GPU {gpu:,}")
            profile = self.dev.thermal_profile()
            if profile in self.mode_buttons and not self.mode_buttons[profile].get_active():
                self._loading = True  # changed from GNOME's Power Mode
                self.mode_buttons[profile].set_active(True)
                self._loading = False
        except OmenError:
            self.fans.set(None, sub="unavailable")
        return GLib.SOURCE_CONTINUE

    def _mode_toggled(self, btn, name):
        if self._loading or not btn.get_active():
            return
        if _run(self.win, self.dev.set_thermal_profile, name):
            self.win.toast(f"{PROFILE_INFO[name][0]} mode")
            self.refresh()

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
        if self._pending_curve and _run(self.win, self.dev.set_fan_curve, self._pending_curve):
            self.win.toast("Fan curve applied")
            self.refresh()

    def _set_power(self, limits):
        args = (None, None) if limits is None else limits
        if _run(self.win, self.dev.set_power_limits, *args):
            self.win.toast("Power limits set to firmware default" if limits is None
                           else f"Power limits: {limits[0]} W / {limits[1]} W")
            self.refresh()


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

        GLib.timeout_add_seconds(15, self._tick)

    def refresh(self) -> None:
        try:
            self.enabled, self.rules = self.store.load()
        except (ValueError, TypeError) as e:
            self.win.toast(f"automation.json is invalid: {e}")
            return
        self._loading = True
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


class PrismWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Prism", default_width=860, default_height=900)
        self.dev = OmenDevice()
        self.toasts = Adw.ToastOverlay()
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
        self.automation = AutomationPage(self)
        stack.add_titled_with_icon(self.automation, "automation", "Automation", "prism-time-symbolic")
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
