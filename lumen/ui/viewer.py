"""LUMEN lightbox viewer - zoom, pan, keyboard navigation.

The viewer is an in-window overlay (Microsoft Photos style) with a Cairo canvas
that renders the preview at fit scale and supports smooth zooming and panning.
Previews are decoded in a worker thread and capped at 2600px for memory safety.
"""

from __future__ import annotations

import threading

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

from gen_icon import rounded_rect  # noqa: E402
from core.scanner import human_duration  # noqa: E402
from core.thumbnailer import load_preview  # noqa: E402
from .gallery_view import draw_text  # noqa: E402

ZOOM_STEP = 1.25
MIN_ZOOM = 0.2
MAX_ZOOM = 8.0


class Viewer(Gtk.Box):
    """Full-window lightbox overlay."""

    __gsignals__ = {
        "closed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "action": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }

    def __init__(self, thumbnailer):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.thumbnailer = thumbnailer
        self.favorite_check = None
        self.items = []
        self.index = 0
        self.pixbuf = None
        self._loading = False
        self._generation = 0
        self._zoom = 1.0
        self._pan = [0.0, 0.0]
        self._drag = None
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.get_style_context().add_class("viewer")
        self.set_no_show_all(True)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.get_style_context().add_class("viewer-bar")
        close = Gtk.Button(label="\u2715")
        close.get_style_context().add_class("icon-btn")
        close.set_tooltip_text("Close viewer (Esc)")
        close.connect("clicked", lambda *_: self.close())
        bar.pack_start(close, False, False, 0)

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.title_label = Gtk.Label(label="")
        self.title_label.get_style_context().add_class("viewer-title")
        self.title_label.set_xalign(0.0)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        titles.pack_start(self.title_label, False, False, 0)
        self.sub_label = Gtk.Label(label="")
        self.sub_label.get_style_context().add_class("viewer-sub")
        self.sub_label.set_xalign(0.0)
        titles.pack_start(self.sub_label, False, False, 0)
        bar.pack_start(titles, True, True, 0)

        self.fav_button = Gtk.Button(label="\u2661")
        self.fav_button.get_style_context().add_class("icon-btn")
        self.fav_button.set_tooltip_text("Toggle favorite")
        self.fav_button.connect("clicked", lambda *_: self._emit_current("favorite"))
        bar.pack_end(self.fav_button, False, False, 0)

        for label, tooltip, action in (
            ("\u2197", "Open with default app", "open"),
            ("\u2699", "Show in file manager", "reveal"),
        ):
            button = Gtk.Button(label=label)
            button.get_style_context().add_class("icon-btn")
            button.set_tooltip_text(tooltip)
            button.connect("clicked", lambda _b, a=action: self._emit_current(a))
            bar.pack_end(button, False, False, 0)

        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        for label, tooltip, zoom in (
            ("\u2212", "Zoom out", -1),
            ("\u2921", "Fit to window", 0),
            ("\uff0b", "Zoom in", 1),
        ):
            button = Gtk.Button(label=label)
            button.get_style_context().add_class("icon-btn")
            button.set_tooltip_text(tooltip)
            button.connect("clicked", lambda _b, z=zoom: self.zoom_by(z))
            zoom_box.pack_start(button, False, False, 0)
        bar.pack_end(zoom_box, False, False, 0)

        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        prev_button = Gtk.Button(label="\u2039")
        prev_button.get_style_context().add_class("icon-btn")
        prev_button.set_tooltip_text("Previous")
        prev_button.connect("clicked", lambda *_: self.step(-1))
        nav.pack_start(prev_button, False, False, 0)
        next_button = Gtk.Button(label="\u203a")
        next_button.get_style_context().add_class("icon-btn")
        next_button.set_tooltip_text("Next")
        next_button.connect("clicked", lambda *_: self.step(1))
        nav.pack_start(next_button, False, False, 0)
        bar.pack_end(nav, False, False, 0)

        self.pack_start(bar, False, False, 0)

        self.canvas = Gtk.DrawingArea()
        self.canvas.set_hexpand(True)
        self.canvas.set_vexpand(True)
        self.canvas.add_events(Gdk.EventMask.SCROLL_MASK |
                               Gdk.EventMask.SMOOTH_SCROLL_MASK |
                               Gdk.EventMask.BUTTON_PRESS_MASK |
                               Gdk.EventMask.BUTTON_RELEASE_MASK |
                               Gdk.EventMask.POINTER_MOTION_MASK)
        self.canvas.connect("draw", self._draw)
        self.canvas.connect("scroll-event", self._on_scroll)
        self.canvas.connect("button-press-event", self._on_press)
        self.canvas.connect("button-release-event", self._on_release)
        self.canvas.connect("motion-notify-event", self._on_motion)
        self.pack_start(self.canvas, True, True, 0)

        self.hint = Gtk.Label(
            label="\u2190 \u2192 navigate \u00b7 scroll to zoom \u00b7 drag to pan \u00b7 Esc to close")
        self.hint.get_style_context().add_class("viewer-hint")
        self.pack_start(self.hint, False, False, 0)

    # -- lifecycle -------------------------------------------------------
    def show_items(self, items, index=0):
        if not items:
            return
        self.items = list(items)
        self.index = max(0, min(index, len(self.items) - 1))
        self._zoom = 1.0
        self._pan = [0.0, 0.0]
        self.set_no_show_all(False)
        self.show_all()
        self.load_current()
        self.canvas.grab_focus()

    def close(self):
        self._generation += 1
        self.pixbuf = None
        self.items = []
        self.hide()
        self.set_no_show_all(True)
        self.emit("closed")

    def is_open(self) -> bool:
        return self.get_visible()

    @property
    def item(self):
        if 0 <= self.index < len(self.items):
            return self.items[self.index]
        return None

    def load_current(self):
        item = self.item
        if item is None:
            return
        self._generation += 1
        generation = self._generation
        self.pixbuf = None
        self._loading = True
        self._zoom = 1.0
        self._pan = [0.0, 0.0]
        self.canvas.queue_draw()
        self._update_bar()

        def worker():
            pixbuf = None
            try:
                pixbuf = load_preview(item.path, 2600)
            except Exception:
                pixbuf = None
            GLib.idle_add(self._loaded, generation, pixbuf)

        threading.Thread(target=worker, name="lumen-viewer", daemon=True).start()

    def _loaded(self, generation, pixbuf):
        if generation != self._generation:
            return False
        self.pixbuf = pixbuf
        self._loading = False
        self.canvas.queue_draw()
        return False

    def _update_bar(self):
        item = self.item
        if item is None:
            return
        self.title_label.set_text(item.name)
        bits = ["%d / %d" % (self.index + 1, len(self.items)),
                item.kind_label]
        if getattr(item, "dimensions", "-") not in ("-", ""):
            bits.append(item.dimensions)
        if item.kind == "video":
            duration = human_duration(getattr(item, "duration", 0.0) or 0.0)
            if duration:
                bits.append(duration)
        if self._zoom != 1.0:
            bits.append("%d%%" % int(self._zoom * 100))
        self.sub_label.set_text("  \u00b7  ".join(bits))
        favorite = False
        if self.favorite_check is not None:
            try:
                favorite = bool(self.favorite_check(item.path))
            except Exception:
                favorite = False
        self.fav_button.set_label("\u2665" if favorite else "\u2661")

    def _emit_current(self, action):
        if self.item is not None:
            self.emit("action", action, self.item)

    # -- navigation ------------------------------------------------------
    def step(self, delta: int):
        if not self.items:
            return
        self.index = (self.index + delta) % len(self.items)
        self.load_current()

    def zoom_by(self, direction: int, factor: float = ZOOM_STEP):
        if direction == 0:
            self._zoom = 1.0
            self._pan = [0.0, 0.0]
        else:
            target = self._zoom * (factor if direction > 0 else 1.0 / factor)
            self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, target))
            if self._zoom <= 1.0:
                self._pan = [0.0, 0.0]
        self._update_bar()
        self.canvas.queue_draw()

    def handle_key(self, event) -> bool:
        key = event.keyval
        if key in (Gdk.KEY_Escape, Gdk.KEY_q):
            self.close()
            return True
        if key in (Gdk.KEY_Right, Gdk.KEY_space, Gdk.KEY_Page_Down):
            self.step(1)
            return True
        if key in (Gdk.KEY_Left, Gdk.KEY_Page_Up):
            self.step(-1)
            return True
        if key in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.zoom_by(1)
            return True
        if key in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.zoom_by(-1)
            return True
        if key == Gdk.KEY_0:
            self.zoom_by(0)
            return True
        if key == Gdk.KEY_f:
            self._emit_current("favorite")
            return True
        if key in (Gdk.KEY_Delete,):
            self._emit_current("trash")
            return True
        return False

    # -- interaction -----------------------------------------------------
    def _on_scroll(self, _widget, event):
        direction = 1 if (event.direction == Gdk.ScrollDirection.UP or
                          (event.direction == Gdk.ScrollDirection.SMOOTH and
                           getattr(event, "delta_y", 0) < 0)) else -1
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            delta = getattr(event, "delta_y", 0) or 0
            if delta == 0:
                return True
            direction = 1 if delta < 0 else -1
        self.zoom_by(direction, 1.12)
        return True

    def _on_press(self, _widget, event):
        if event.button == 1:
            if event.type == Gdk.EventType._2BUTTON_PRESS:
                self._zoom = 1.0 if self._zoom > 1.0 else 2.0
                self._pan = [0.0, 0.0]
                self._update_bar()
                self.canvas.queue_draw()
                return True
            self._drag = (event.x, event.y, self._pan[0], self._pan[1])
            self.canvas.get_window().set_cursor(
                Gdk.Cursor.new_from_name(self.get_display(), "grabbing"))
            return True
        if event.button == 8:
            self.step(-1)
            return True
        if event.button == 9:
            self.step(1)
            return True
        return False

    def _on_release(self, _widget, _event):
        if self._drag is not None:
            self._drag = None
            self.canvas.get_window().set_cursor(None)
            return True
        return False

    def _on_motion(self, _widget, event):
        if self._drag is None:
            return False
        start_x, start_y, pan_x, pan_y = self._drag
        self._pan = [pan_x + (event.x - start_x), pan_y + (event.y - start_y)]
        self.canvas.queue_draw()
        return True

    # -- painting --------------------------------------------------------
    def _draw(self, area, cr):
        allocation = area.get_allocation()
        width, height = max(1, allocation.width), max(1, allocation.height)

        # subtle studio backdrop
        backdrop = cairo.LinearGradient(0, 0, 0, height)
        backdrop.add_color_stop_rgba(0.0, 0.05, 0.05, 0.07, 1.0)
        backdrop.add_color_stop_rgba(1.0, 0.02, 0.02, 0.04, 1.0)
        cr.set_source(backdrop)
        rounded_rect(cr, 0, 0, width, height, 0)
        cr.fill()

        if self.pixbuf is None:
            if self._loading:
                draw_text(cr, "Loading\u2026", width / 2, height / 2, 11,
                          (1, 1, 1, 0.45), anchor="middle")
            else:
                draw_text(cr, "No preview available", width / 2, height / 2, 11,
                          (1, 1, 1, 0.45), anchor="middle")
                draw_text(cr, "Try opening the file with its default app",
                          width / 2, height / 2 + 20, 8.5,
                          (1, 1, 1, 0.28), anchor="middle")
            return False

        pixbuf = self.pixbuf
        pw, ph = max(1, pixbuf.get_width()), max(1, pixbuf.get_height())
        fit = min(width / float(pw), height / float(ph))
        scale = fit * self._zoom
        draw_w, draw_h = pw * scale, ph * scale

        # clamp panning so the picture never leaves the canvas
        max_pan_x = max(0.0, (draw_w - width) / 2.0 + 40)
        max_pan_y = max(0.0, (draw_h - height) / 2.0 + 40)
        self._pan[0] = max(-max_pan_x, min(max_pan_x, self._pan[0]))
        self._pan[1] = max(-max_pan_y, min(max_pan_y, self._pan[1]))

        cr.save()
        cr.translate(width / 2.0 + self._pan[0], height / 2.0 + self._pan[1])
        cr.scale(scale, scale)
        cr.translate(-pw / 2.0, -ph / 2.0)
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
        cr.paint()
        cr.restore()

        if self._zoom > 1.0:
            draw_text(cr, "%d%%" % int(self._zoom * 100), width - 14, height - 14,
                      9, (1, 1, 1, 0.45), anchor="end", bold=True)
        return False
