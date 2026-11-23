"""Custom drawn widgets: keyboard preview, fan curve editor, sparkline."""
from __future__ import annotations

import colorsys
import math
import random
from collections import deque
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .backend import CURVE_MAX_POINTS, CURVE_MIN_POINTS, CurvePoint  # noqa: E402

RGB = tuple[float, float, float]


def hex_to_rgb(h: str) -> RGB:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(c: RGB) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in c)


def _scale(c: RGB, k: float) -> RGB:
    return (c[0] * k, c[1] * k, c[2] * k)


def _hue(h: float, v: float = 1.0) -> RGB:
    return colorsys.hsv_to_rgb(h % 1.0, 1.0, v)


def rounded_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def preview_colors(mode: str, base: list[RGB], speed: int, t: float, rng: random.Random) -> list[RGB]:
    """An approximation of each driver effect, for the on-screen preview only."""
    s = 0.25 + speed * 0.25  # cycles per second-ish
    n = len(base)
    if mode == "breathing":
        k = 0.15 + 0.85 * (0.5 + 0.5 * math.sin(t * s * 2))
        return [_scale(c, k) for c in base]
    if mode == "pulse":
        k = 0.2 + 0.8 * (1 - (t * s) % 1.0) ** 3
        return [_scale(c, k) for c in base]
    if mode == "rainbow":
        return [_hue(t * s * 0.3 + i / n) for i in range(n)]
    if mode == "wave":
        return [_scale(c, 0.2 + 0.8 * (0.5 + 0.5 * math.sin(t * s * 3 - i * 1.3))) for i, c in enumerate(base)]
    if mode == "chase":
        lit = int(t * s * 2) % n
        return [c if i == lit else _scale(c, 0.08) for i, c in enumerate(base)]
    if mode == "sparkle":
        return [(1.0, 1.0, 1.0) if rng.random() < 0.06 * s else _scale(c, 0.6) for c in base]
    if mode == "candle":
        return [(1.0 * k, 0.42 * k, 0.05 * k) for k in (0.6 + 0.4 * rng.random() for _ in base)]
    if mode == "aurora":
        return [colorsys.hsv_to_rgb(0.38 + 0.17 * (0.5 + 0.5 * math.sin(t * s * 0.8 + i)), 1,
                                    0.55 + 0.45 * math.sin(t * s * 1.3 + i * 0.7) ** 2) for i in range(n)]
    if mode == "disco":
        step = int(t * s * 3)
        r = random.Random(step)
        return [_hue(r.random()) if r.random() > 0.3 else (0, 0, 0) for _ in base]
    if mode == "gradient":
        f = (t * s * 0.25) % n
        i0 = int(f)
        k = f - i0
        return [tuple(a * (1 - k) + b * k for a, b in zip(base[(z + i0) % n], base[(z + i0 + 1) % n]))
                for z in range(n)]  # type: ignore[misc]
    if mode == "spectrum":
        return [_hue(t / (20 / speed))] * n
    return list(base)


class KeyboardPreview(Gtk.DrawingArea):
    """A stylised 4-zone laptop keyboard. Click a key to select its zone."""

    # Driver zone numbers, as seen on the Omen 16-c0xxx: zone 3 is only the
    # W A S D keys, zone 2 the rest of the left side, zone 1 the middle and
    # zone 0 the right side including the numpad.
    WASD, LEFT, MIDDLE, RIGHT = 3, 2, 1, 0
    # Where the left / middle / right zones end, as a fraction of the width.
    LEFT_END, MIDDLE_END = 0.30, 0.58
    # (row, key index in the main block) of W, A, S, D in ROWS below.
    WASD_KEYS = {(2, 2), (3, 1), (3, 2), (3, 3)}

    ROWS = (
        # (key widths in units) per row; main block then numpad
        ([1] * 13 + [1.5], [1] * 4),
        ([1] * 13 + [1.5], [1] * 4),
        ([1.5] + [1] * 12 + [1], [1] * 4),
        ([1.8] + [1] * 11 + [1.7], [1] * 4),
        ([2.3] + [1] * 10 + [2.2], [1] * 4),
        ([1.2, 1, 1.2, 5.6, 1.2, 1.2, 1, 1, 1], [2, 1, 1]),
    )

    def __init__(self, on_select: Callable[[int], None] | None = None):
        super().__init__()
        self.set_content_height(250)
        self.set_hexpand(True)
        self.colors: list[RGB] = [(1, 0, 0)] * 4
        self.brightness = 1.0
        self.mode = "static"
        self.speed = 3
        self.selected: int | None = None
        self.on_select = on_select
        self._rng = random.Random(7)
        self._shown = list(self.colors)
        self._t0 = GLib.get_monotonic_time()
        self._tick = None
        self._keys: list[tuple[float, float, float, float, int]] = []  # x, y, w, h, zone
        self.set_draw_func(self._draw)
        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.add_controller(click)
        self.set_cursor_from_name("pointer")

    def update(self, colors: list[str], brightness: int, mode: str, speed: int) -> None:
        self.colors = [hex_to_rgb(c) for c in colors]
        self.brightness = brightness / 100
        self.mode, self.speed = mode, speed
        animated = mode != "static"
        if animated and self._tick is None:
            self._tick = self.add_tick_callback(self._on_tick)
        elif not animated and self._tick is not None:
            self.remove_tick_callback(self._tick)
            self._tick = None
        self._shown = list(self.colors)
        self.queue_draw()

    def _on_tick(self, _w, clock):
        t = (clock.get_frame_time() - self._t0) / 1e6
        self._shown = preview_colors(self.mode, self.colors, self.speed, t, self._rng)
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _layout(self, w, h):
        pad = 14
        return pad, pad, w - 2 * pad, h - 2 * pad

    def _zone_of(self, row: int, numpad: bool, index: int, fx: float) -> int:
        if numpad:
            return self.RIGHT
        if (row, index) in self.WASD_KEYS:
            return self.WASD
        if fx < self.LEFT_END:
            return self.LEFT
        if fx < self.MIDDLE_END:
            return self.MIDDLE
        return self.RIGHT

    def _clicked(self, _g, _n, x, y):
        # Nearest key wins, so clicks in the gaps still select something.
        if not self._keys:
            return
        zone = min(self._keys, key=lambda k: (k[0] + k[2] / 2 - x) ** 2 + (k[1] + k[3] / 2 - y) ** 2)[4]
        self.selected = zone
        self.queue_draw()
        if self.on_select:
            self.on_select(zone)

    def _draw(self, _area, cr, w, h):
        bx, by, bw, bh = self._layout(w, h)
        rounded_rect(cr, bx, by, bw, bh, 16)
        cr.set_source_rgb(0.07, 0.075, 0.085)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.set_line_width(1)
        cr.stroke()

        inner = 16
        foot = 26  # strip under the keys for the brightness badge
        kx, ky, kw, kh = bx + inner, by + inner, bw - 2 * inner, bh - 2 * inner - foot + 8
        main_units = max(sum(r[0]) for r in self.ROWS)
        pad_units, gap_units = 4, 0.6
        unit = kw / (main_units + gap_units + pad_units)
        row_h = kh / len(self.ROWS)
        g = max(2.0, unit * 0.12)
        b = self.brightness

        keys = []
        for r, (main, numpad) in enumerate(self.ROWS):
            y = ky + r * row_h
            for is_pad, blocks, start in ((False, main, kx),
                                          (True, numpad, kx + (main_units + gap_units) * unit)):
                x = start
                for i, units in enumerate(blocks):
                    kwid = units * unit
                    zone = self._zone_of(r, is_pad, i, (x + kwid / 2 - bx) / bw)
                    keys.append((x, y, kwid, row_h, zone))
                    x += kwid
        self._keys = keys

        # Perceived light: a 15% backlight still reads as dimly lit, not off.
        k = b ** 0.55 if b > 0 else 0.0
        if k > 0:
            # Soft halo leaking around each key, widest at full brightness.
            for spread, alpha in ((g * 2.6, 0.05), (g * 1.6, 0.08), (g * 0.8, 0.12)):
                for x, y, kwid, kh_, zone in keys:
                    cr.set_source_rgba(*self._shown[zone], alpha * k)
                    rounded_rect(cr, x - spread / 2, y - spread / 2, kwid + spread, kh_ + spread, 8)
                    cr.fill()
        for x, y, kwid, kh_, zone in keys:
            c = self._shown[zone]
            x0, y0, w0, h0 = x + g / 2, y + g / 2, kwid - g, kh_ - g
            # Keycap: dark plastic tinted by the light shining through it.
            rounded_rect(cr, x0, y0, w0, h0, 5)
            base = 0.12
            cr.set_source_rgb(*(base + (v - base) * 0.22 * k for v in c))
            cr.fill_preserve()
            if k > 0:
                cr.set_source_rgba(*c, 0.35 + 0.6 * k)
            else:
                cr.set_source_rgba(1, 1, 1, 0.08)
            cr.set_line_width(1.4)
            cr.stroke()
            # Legend: the brightest part, close to white at full brightness.
            lx, ly, lw, lh = x + kwid * 0.3, y + kh_ * 0.42, kwid * 0.4, kh_ * 0.16
            if k > 0:
                hot = tuple(v + (1 - v) * 0.45 * k for v in c)
                cr.set_source_rgba(*hot, 0.25 + 0.75 * k)
            else:
                cr.set_source_rgba(1, 1, 1, 0.10)
            rounded_rect(cr, lx, ly, lw, lh, 2)
            cr.fill()

        if self.selected is not None:
            # Accent ring around the selected zone's keys, offset so the zone's own
            # colour (even white) stays visible.
            cr.set_source_rgba(0.21, 0.52, 0.89, 0.95)
            cr.set_line_width(2)
            for x, y, kwid, kh_, zone in keys:
                if zone == self.selected:
                    rounded_rect(cr, x + g / 2 - 2.5, y + g / 2 - 2.5, kwid - g + 5, kh_ - g + 5, 7)
                    cr.stroke()

        # Brightness badge
        label = "Off" if b <= 0 else f"{round(b * 100)}%"
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(11)
        ext = cr.text_extents(label)
        bw_, bh_ = ext.width + 16, 20
        rx, ry = bx + bw - bw_ - inner, by + bh - bh_ - 8
        rounded_rect(cr, rx, ry, bw_, bh_, 10)
        cr.set_source_rgba(1, 1, 1, 0.08)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.7)
        cr.move_to(rx + 8 - ext.x_bearing, ry + bh_ / 2 - ext.y_bearing - ext.height / 2)
        cr.show_text(label)

    def set_brightness(self, percent: float) -> None:
        self.brightness = percent / 100
        self.queue_draw()


class Swatch(Gtk.DrawingArea):
    """Four small colour bars, used on preset buttons."""

    def __init__(self, colors: list[str], size=(56, 18)):
        super().__init__()
        self.set_content_width(size[0])
        self.set_content_height(size[1])
        self.colors = [hex_to_rgb(c) for c in colors]
        self.set_draw_func(self._draw)

    def _draw(self, _a, cr, w, h):
        rounded_rect(cr, 0, 0, w, h, 5)
        cr.clip()
        seg = w / len(self.colors)
        for i, c in enumerate(self.colors):
            cr.set_source_rgb(*c)
            cr.rectangle(i * seg, 0, seg + 1, h)
            cr.fill()


class Sparkline(Gtk.DrawingArea):
    def __init__(self, length=60, lo=0.0, hi=100.0):
        super().__init__()
        self.set_content_height(34)
        self.set_hexpand(True)
        self.values: deque[float | None] = deque([None] * length, maxlen=length)
        self.lo, self.hi = lo, hi
        self.set_draw_func(self._draw)

    def push(self, v: float | None) -> None:
        self.values.append(v)
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        color = self.get_color()
        pts = [(i, v) for i, v in enumerate(self.values) if v is not None]
        if len(pts) < 2:
            return
        n = len(self.values) - 1
        span = (self.hi - self.lo) or 1

        def xy(i, v):
            return w * i / n, h - 2 - (h - 4) * (min(max(v, self.lo), self.hi) - self.lo) / span

        cr.move_to(*xy(*pts[0]))
        for p in pts[1:]:
            cr.line_to(*xy(*p))
        cr.set_source_rgba(color.red, color.green, color.blue, 0.9)
        cr.set_line_width(1.6)
        cr.stroke_preserve()
        cr.line_to(xy(*pts[-1])[0], h)
        cr.line_to(xy(*pts[0])[0], h)
        cr.close_path()
        cr.set_source_rgba(color.red, color.green, color.blue, 0.12)
        cr.fill()


class FanCurveEditor(Gtk.DrawingArea):
    """Drag points to shape the curve. Click empty space to add, right-click to remove."""

    T_MIN, T_MAX = 30, 100
    HIT = 14

    def __init__(self, on_change: Callable[[list[CurvePoint]], None]):
        super().__init__()
        self.set_content_height(240)
        self.set_hexpand(True)
        self.points: list[CurvePoint] = []
        self.current_temp: float | None = None
        self.on_change = on_change
        self._drag: int | None = None
        self._drag_origin = (0.0, 0.0)
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", lambda *_: setattr(self, "_drag", None))
        self.add_controller(drag)

        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._pressed)
        self.add_controller(click)

    # geometry
    def _area(self):
        w, h = self.get_width(), self.get_height()
        return 40, 12, w - 56, h - 40

    def _to_px(self, t, p):
        x, y, w, h = self._area()
        return x + w * (t - self.T_MIN) / (self.T_MAX - self.T_MIN), y + h * (1 - p / 100)

    def _from_px(self, px, py):
        x, y, w, h = self._area()
        t = self.T_MIN + (px - x) / w * (self.T_MAX - self.T_MIN)
        p = (1 - (py - y) / h) * 100
        return round(min(max(t, self.T_MIN), self.T_MAX)), round(min(max(p, 0), 100))

    def _hit(self, px, py) -> int | None:
        for i, (t, p) in enumerate(self.points):
            x, y = self._to_px(t, p)
            if abs(x - px) <= self.HIT and abs(y - py) <= self.HIT:
                return i
        return None

    def set_points(self, points: list[CurvePoint]) -> None:
        self.points = sorted(points)
        self.queue_draw()

    def _changed(self):
        self.points.sort()
        self.queue_draw()
        self.on_change(list(self.points))

    def _drag_begin(self, _g, x, y):
        self._drag = self._hit(x, y)
        self._drag_origin = (x, y)

    def _drag_update(self, _g, dx, dy):
        if self._drag is None:
            return
        t, p = self._from_px(self._drag_origin[0] + dx, self._drag_origin[1] + dy)
        others = {pt[0] for i, pt in enumerate(self.points) if i != self._drag}
        if t in others:
            return
        self.points[self._drag] = (t, p)
        moved = self.points[self._drag]
        self.points.sort()
        self._drag = self.points.index(moved)
        self.queue_draw()
        self.on_change(list(self.points))

    def _pressed(self, gesture, n_press, x, y):
        button = gesture.get_current_button()
        hit = self._hit(x, y)
        if button == 3 and hit is not None and len(self.points) > CURVE_MIN_POINTS:
            del self.points[hit]
            self._changed()
        elif button == 1 and hit is None and len(self.points) < CURVE_MAX_POINTS:
            t, p = self._from_px(x, y)
            if t not in {pt[0] for pt in self.points}:
                self.points.append((t, p))
                self._changed()

    def _draw(self, _a, cr, w, h):
        fg = self.get_color()
        ax, ay, aw, ah = self._area()
        cr.set_line_width(1)
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        for p in range(0, 101, 25):
            _, y = self._to_px(self.T_MIN, p)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.10)
            cr.move_to(ax, y)
            cr.line_to(ax + aw, y)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.move_to(4, y + 3)
            cr.show_text(f"{p}%")
        for t in range(self.T_MIN, self.T_MAX + 1, 10):
            x, _ = self._to_px(t, 0)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.10)
            cr.move_to(x, ay)
            cr.line_to(x, ay + ah)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.move_to(x - 10, ay + ah + 16)
            cr.show_text(f"{t}°")

        # Danger zone
        x85, _ = self._to_px(85, 0)
        cr.set_source_rgba(0.9, 0.2, 0.2, 0.06)
        cr.rectangle(x85, ay, ax + aw - x85, ah)
        cr.fill()

        if self.current_temp is not None:
            x, _ = self._to_px(min(max(self.current_temp, self.T_MIN), self.T_MAX), 0)
            cr.set_source_rgba(1.0, 0.6, 0.1, 0.8)
            cr.set_dash([4, 3])
            cr.move_to(x, ay)
            cr.line_to(x, ay + ah)
            cr.stroke()
            cr.set_dash([])
            cr.move_to(x + 4, ay + 12)
            cr.show_text(f"now {self.current_temp:.0f}°")

        if not self.points:
            return
        accent = (0.21, 0.52, 0.89)
        pts = [self._to_px(t, p) for t, p in self.points]
        # clamp lines to edges, like the driver does
        cr.move_to(ax, pts[0][1])
        for x, y in pts:
            cr.line_to(x, y)
        cr.line_to(ax + aw, pts[-1][1])
        cr.set_source_rgb(*accent)
        cr.set_line_width(2.5)
        cr.stroke_preserve()
        cr.line_to(ax + aw, ay + ah)
        cr.line_to(ax, ay + ah)
        cr.close_path()
        cr.set_source_rgba(*accent, 0.15)
        cr.fill()
        for (x, y), (t, p) in zip(pts, self.points):
            cr.arc(x, y, 6, 0, 2 * math.pi)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(*accent)
            cr.set_line_width(2.5)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
            cr.move_to(x - 14, y - 10)
            cr.show_text(f"{t}°·{p}%")
