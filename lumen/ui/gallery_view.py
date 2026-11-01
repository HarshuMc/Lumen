"""LUMEN gallery - Cairo drawn thumbnail grid with lazy loading.

Cells paint themselves (rounded cover art, play badges, duration pills,
favourite hearts, selection rings) which keeps the widget tree tiny; thumbnails
are streamed in by :class:`core.thumbnailer.Thumbnailer` and released as soon
as a cell scrolls far out of view, so browsing 1000+ files stays cheap.
"""

from __future__ import annotations

import math

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango, PangoCairo  # noqa: E402

from gen_icon import rounded_rect  # noqa: E402
from core.scanner import human_duration  # noqa: E402
from .settings import color, palette  # noqa: E402

GRID_SIZE = 168
FILM_W, FILM_H = 226, 148
BATCH = 60
INITIAL_FILL = 180


def draw_text(cr, text, x, y, size=8.5, rgba=(1, 1, 1, 0.92), anchor="start",
              bold=False, max_width=None):
    """Draw a single line of Pango text; returns its pixel size."""
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription.from_string(
        "%s %.1f" % ("Sans Bold" if bold else "Sans", size))
    layout.set_font_description(desc)
    layout.set_text(text or "", -1)
    if max_width is not None:
        layout.set_width(int(max_width * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
    width, height = layout.get_pixel_size()
    if anchor == "end":
        x -= width
    elif anchor == "middle":
        x -= width / 2.0
    cr.move_to(x, y)
    cr.set_source_rgba(*rgba)
    PangoCairo.show_layout(cr, layout)
    return width, height


def _heart(cr, x, y, size):
    """Small heart path centred on (x, y)."""
    cr.new_sub_path()
    cr.move_to(x, y + size * 0.62)
    cr.curve_to(x - size * 1.15, y - size * 0.10, x - size * 0.42, y - size * 0.95,
                x, y - size * 0.32)
    cr.curve_to(x + size * 0.42, y - size * 0.95, x + size * 1.15, y - size * 0.10,
                x, y + size * 0.62)
    cr.close_path()


class ThumbCell(Gtk.EventBox):
    """One media tile.  Holds a pixbuf only while it is near the viewport."""

    def __init__(self, item, gallery, thumb_size=GRID_SIZE, width=GRID_SIZE,
                 height=GRID_SIZE, caption=True):
        Gtk.EventBox.__init__(self)
        self.item = item
        self.gallery = gallery
        self.thumb_size = thumb_size
        self.pixbuf = None
        self.selected = False
        self.hover = False
        self._requested = False
        self.set_visible_window(False)
        self.get_style_context().add_class("cell")
        self.set_tooltip_text("%s\n%s" % (item.name, getattr(item, "folder", "")))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add(box)

        self.area = Gtk.DrawingArea()
        self.area.set_size_request(width, height)
        self.area.connect("draw", self._on_draw)
        box.pack_start(self.area, False, False, 0)

        if caption:
            name = Gtk.Label(label=item.name)
            name.get_style_context().add_class("cell-name")
            name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            name.set_max_width_chars(18 if width <= GRID_SIZE else 24)
            name.set_xalign(0.0)
            name.set_margin_top(2)
            box.pack_start(name, False, False, 0)

            sub = Gtk.Label(label=self._sub_text())
            sub.get_style_context().add_class("cell-sub")
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            sub.set_xalign(0.0)
            box.pack_start(sub, False, False, 0)
            self.sub_label = sub
        else:
            self.sub_label = None

        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)
        self.connect("button-press-event", self._on_button)

    # -- state -----------------------------------------------------------
    def _sub_text(self) -> str:
        size = getattr(self.item, "size_text", "")
        duration = human_duration(getattr(self.item, "duration", 0.0) or 0.0)
        if duration and self.item.kind == "video":
            return "%s  \u00b7  %s" % (duration, size)
        parent = getattr(self.item, "parent_name", "")
        return "%s  \u00b7  %s" % (parent, size) if parent else size

    def refresh_subtitle(self):
        if self.sub_label is not None:
            self.sub_label.set_text(self._sub_text())

    def ensure_thumb(self):
        if self.pixbuf is not None or self._requested:
            return
        if not self.get_mapped():
            return
        self._requested = True
        self.gallery.thumbnailer.request(self.item, self._thumb_ready, self.thumb_size)

    def _thumb_ready(self, pixbuf):
        self._requested = False
        if pixbuf is None:
            return False
        if not self.get_mapped():
            return False
        self.pixbuf = pixbuf
        self.area.queue_draw()
        return False

    def release(self):
        if self.pixbuf is not None:
            self.pixbuf = None
            self.area.queue_draw()

    def set_selected(self, selected: bool):
        if self.selected != selected:
            self.selected = selected
            self.area.queue_draw()

    # -- events ----------------------------------------------------------
    def _on_enter(self, *_args):
        self.hover = True
        self.area.queue_draw()
        return False

    def _on_leave(self, *_args):
        self.hover = False
        self.area.queue_draw()
        return False

    def _on_button(self, _widget, event):
        if event.button == 3:
            if not self.selected:
                self.gallery.select_only(self.item)
            self.gallery.emit("context-menu", self.item, event)
            return True
        if event.button != 1:
            return False
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self.gallery.emit("item-activated", self.item)
            return True
        state = event.state
        additive = bool(state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.META_MASK))
        ranged = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if ranged:
            self.gallery.select_range(self.item)
        else:
            self.gallery.toggle_select(self.item, additive=additive)
        self.gallery.emit("item-focused", self.item)
        return True

    # -- painting --------------------------------------------------------
    def _on_draw(self, area, cr):
        allocation = area.get_allocation()
        width, height = max(1, allocation.width), max(1, allocation.height)
        pal = palette()
        self.ensure_thumb()

        radius = 13.0
        rounded_rect(cr, 0.5, 0.5, width - 1, height - 1, radius)
        cr.save()
        cr.clip()

        if self.pixbuf is not None:
            pixbuf = self.pixbuf
            pw, ph = max(1, pixbuf.get_width()), max(1, pixbuf.get_height())
            scale = max(width / float(pw), height / float(ph))
            draw_w, draw_h = pw * scale, ph * scale
            cr.save()
            cr.translate((width - draw_w) / 2.0, (height - draw_h) / 2.0)
            cr.scale(scale, scale)
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
            cr.paint()
            cr.restore()
        else:
            self._draw_placeholder(cr, width, height, pal)

        if self.hover and not self.selected:
            cr.set_source_rgba(1, 1, 1, 0.06)
            cr.rectangle(0, 0, width, height)
            cr.fill()

        if self.item.kind == "video" and self.pixbuf is not None:
            self._draw_play(cr, width, height)

        duration = human_duration(getattr(self.item, "duration", 0.0) or 0.0)
        if self.item.kind == "video" and duration:
            self._draw_duration(cr, width, height, duration)

        if self._is_favorite():
            accent = color("accent2", 0.95)
            _heart(cr, 15, 17, 6.2)
            cr.set_source_rgba(*accent)
            cr.fill()

        if self.selected:
            cr.set_line_width(2.4)
            cr.set_source_rgba(*color("accent", 1.0))
            rounded_rect(cr, 1.6, 1.6, width - 3.2, height - 3.2, radius - 1.2)
            cr.stroke()
            self._draw_check(cr, width)
        cr.restore()

        return False

    def _is_favorite(self):
        checker = getattr(self.gallery, "favorite_check", None)
        if checker is None:
            return False
        try:
            return bool(checker(self.item.path))
        except Exception:
            return False

    def _draw_placeholder(self, cr, width, height, pal):
        plate = cairo_gradient(cr, width, height, pal)
        cr.set_source(plate)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        phase = getattr(self.gallery, "shimmer_phase", 0.0)
        if self.gallery.loading:
            band = cairo.LinearGradient(-width, 0, width * 2, height)
            offset = (phase % 1.0) * width * 2 - width
            band.add_color_stop_rgba(0.0, 1, 1, 1, 0.0)
            band.add_color_stop_rgba(max(0.0, offset / (width * 2) - 0.08), 1, 1, 1, 0.0)
            band.add_color_stop_rgba(max(0.0, offset / (width * 2)), 1, 1, 1, 0.07)
            band.add_color_stop_rgba(min(1.0, offset / (width * 2) + 0.08), 1, 1, 1, 0.0)
            band.add_color_stop_rgba(1.0, 1, 1, 1, 0.0)
            cr.set_source(band)
            cr.rectangle(0, 0, width, height)
            cr.fill()

        glyph = (1, 1, 1, 0.30)
        cx, cy = width / 2.0, height / 2.0
        if self.item.kind == "video":
            cr.set_source_rgba(*glyph)
            cr.arc(cx, cy, min(width, height) * 0.17, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.75)
            size = min(width, height) * 0.075
            cr.move_to(cx - size * 0.55, cy - size)
            cr.line_to(cx + size, cy)
            cr.line_to(cx - size * 0.55, cy + size)
            cr.close_path()
            cr.fill()
        elif self.item.kind == "document":
            page_w, page_h = width * 0.30, height * 0.36
            cr.set_source_rgba(*glyph)
            rounded_rect(cr, cx - page_w / 2, cy - page_h / 2, page_w, page_h, 4)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.35)
            for line in range(4):
                cr.rectangle(cx - page_w * 0.33, cy - page_h * 0.28 + line * page_h * 0.16,
                             page_w * 0.66, 2.0)
                cr.fill()
        else:
            cr.set_source_rgba(*glyph)
            cr.rectangle(cx - width * 0.22, cy - height * 0.16, width * 0.44, height * 0.32)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.55)
            cr.arc(cx + width * 0.10, cy - height * 0.06, min(width, height) * 0.045, 0, 2 * math.pi)
            cr.fill()
            cr.move_to(cx - width * 0.18, cy + height * 0.14)
            cr.line_to(cx - width * 0.02, cy - height * 0.02)
            cr.line_to(cx + width * 0.16, cy + height * 0.14)
            cr.close_path()
            cr.fill()

    def _draw_play(self, cr, width, height):
        cx, cy = width / 2.0, height / 2.0
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.arc(cx, cy, min(width, height) * 0.16, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.92)
        size = min(width, height) * 0.058
        cr.move_to(cx - size * 0.7, cy - size)
        cr.line_to(cx + size * 1.1, cy)
        cr.line_to(cx - size * 0.7, cy + size)
        cr.close_path()
        cr.fill()

    def _draw_duration(self, cr, width, height, text):
        cr.save()
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 7.5"))
        layout.set_text(text, -1)
        tw, th = layout.get_pixel_size()
        pad = 4.0
        bx = width - tw - pad * 2 - 5
        by = height - th - pad * 2 - 4
        rounded_rect(cr, bx, by, tw + pad * 2, th + pad * 1.4, 4.5)
        cr.set_source_rgba(0, 0, 0, 0.62)
        cr.fill()
        cr.move_to(bx + pad, by + pad * 0.55)
        cr.set_source_rgba(1, 1, 1, 0.94)
        PangoCairo.show_layout(cr, layout)
        cr.restore()

    def _draw_check(self, cr, width):
        cx, cy, radius = width - 15, 15, 9.0
        cr.set_source_rgba(*color("accent", 1.0))
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.fill()
        cr.set_line_width(1.8)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(1, 1, 1, 1)
        cr.move_to(cx - 4.0, cy + 0.2)
        cr.line_to(cx - 1.2, cy + 3.0)
        cr.line_to(cx + 4.2, cy - 2.8)
        cr.stroke()


def cairo_gradient(cr, width, height, pal):
    gradient = cairo.LinearGradient(0, 0, width, height)
    accent = pal.get("accent", (0.48, 0.23, 0.93, 1.0))
    plate = pal.get("plate", (1, 1, 1, 0.06))
    gradient.add_color_stop_rgba(0.0, accent[0], accent[1], accent[2], 0.20)
    gradient.add_color_stop_rgba(0.55, plate[0], plate[1], plate[2], max(0.05, plate[3]))
    gradient.add_color_stop_rgba(1.0, accent[0], accent[1], accent[2], 0.10)
    return gradient


class AlbumCard(Gtk.EventBox):
    """Auto/custom album tile with a Cairo collage cover."""

    def __init__(self, key, title, count, cover_pixbufs, on_open=None, subtitle=None):
        Gtk.EventBox.__init__(self)
        self.album_name = key
        self.set_visible_window(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.get_style_context().add_class("album-card")
        box.set_size_request(196, -1)
        self.add(box)

        self.area = Gtk.DrawingArea()
        self.area.set_size_request(180, 118)
        self._covers = list(cover_pixbufs)[:4]
        while len(self._covers) < 4:
            self._covers.append(None)
        self.area.connect("draw", self._draw)
        box.pack_start(self.area, False, False, 0)

        title = Gtk.Label(label=title)
        title.get_style_context().add_class("album-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_xalign(0.0)
        title.set_max_width_chars(22)
        box.pack_start(title, False, False, 0)

        sub = Gtk.Label(label=subtitle or "%d item%s" % (count, "" if count == 1 else "s"))
        sub.get_style_context().add_class("album-count")
        sub.set_xalign(0.0)
        box.pack_start(sub, False, False, 0)

        self._on_open = on_open
        if on_open is not None:
            self.connect("button-press-event", self._on_click)
        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)

    def set_cover(self, slot: int, pixbuf):
        while len(self._covers) <= slot:
            self._covers.append(None)
        self._covers[slot] = pixbuf
        self.area.queue_draw()

    def _on_click(self, _widget, _event):
        if self._on_open is not None:
            self._on_open(self.album_name)
        return True

    def _on_enter(self, *_args):
        self.area.queue_draw()
        return False

    def _on_leave(self, *_args):
        self.area.queue_draw()
        return False

    def _draw(self, area, cr):
        allocation = area.get_allocation()
        width, height = max(1, allocation.width), max(1, allocation.height)
        radius = 11.0
        rounded_rect(cr, 0, 0, width, height, radius)
        cr.save()
        cr.clip()
        pal = palette()
        available = [pb for pb in self._covers if pb is not None]
        if not available:
            cr.set_source(cairo_gradient(cr, width, height, pal))
            cr.rectangle(0, 0, width, height)
            cr.fill()
            draw_text(cr, "\U0001F5C2\uFE0F", width / 2, height / 2 - 12, 20,
                      (1, 1, 1, 0.5), anchor="middle")
        elif len(available) == 1:
            self._paint_cover(cr, available[0], 0, 0, width, height)
        else:
            half_w, half_h = width / 2.0, height / 2.0
            for index, pixbuf in enumerate(available[:4]):
                x = (index % 2) * half_w
                y = (index // 2) * half_h
                self._paint_cover(cr, pixbuf, x, y, half_w, half_h)
            cr.set_source_rgba(0, 0, 0, 0.10)
            cr.rectangle(0, 0, width, height)
            cr.fill()
        cr.restore()

    @staticmethod
    def _paint_cover(cr, pixbuf, x, y, width, height):
        pw, ph = max(1, pixbuf.get_width()), max(1, pixbuf.get_height())
        scale = max(width / float(pw), height / float(ph))
        cr.save()
        cr.rectangle(x, y, width, height)
        cr.clip()
        cr.translate(x + (width - pw * scale) / 2.0, y + (height - ph * scale) / 2.0)
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
        cr.paint()
        cr.restore()


class GalleryView(Gtk.Box):
    """Scrollable, lazily built media grid (grid + filmstrip modes)."""

    __gsignals__ = {
        "item-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "item-focused": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "context-menu": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
    }

    def __init__(self, thumbnailer, dispatch=None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.thumbnailer = thumbnailer
        self.dispatch = dispatch or GLib.idle_add
        self.favorite_check = None
        self.loading = False
        self.shimmer_phase = 0.0
        self._items = []
        self._cells = []
        self._selection = set()
        self._anchor = None
        self._built = 0
        self._mode = "grid"
        self._generation = 0
        self._select_mode = False
        self._sweep_source = None
        self._grow_source = None
        self._shimmer_source = None

        self.get_style_context().add_class("gallery-host")
        self.set_hexpand(True)
        self.set_vexpand(True)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        self.pack_start(self.stack, True, True, 0)

        # grid page
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self.scroll.get_style_context().add_class("gallery-scroll")
        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_column_spacing(8)
        self.flow.set_row_spacing(4)
        self.flow.set_max_children_per_line(0)
        self.flow.set_min_children_per_line(1)
        self.flow.set_homogeneous(False)
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_halign(Gtk.Align.FILL)
        self.flow.get_style_context().add_class("gallery-flow")
        self.scroll.add(self.flow)
        self.scroll.get_vadjustment().connect("value-changed", self._on_scrolled)
        self.stack.add_named(self.scroll, "grid")

        # filmstrip page
        self.film_scroll = Gtk.ScrolledWindow()
        self.film_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.film_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self.film_scroll.get_style_context().add_class("gallery-scroll")
        self.film_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.film_box.set_valign(Gtk.Align.START)
        self.film_box.get_style_context().add_class("gallery-flow")
        self.film_scroll.add(self.film_box)
        self.film_scroll.get_hadjustment().connect("value-changed", self._on_scrolled)
        self.stack.add_named(self.film_scroll, "film")

        # empty page
        self.empty = self._build_empty()
        self.stack.add_named(self.empty, "empty")
        self.stack.set_visible_child_name("grid")
        self.show_all()

    # -- empty state -----------------------------------------------------
    def _build_empty(self):
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        wrapper.set_valign(Gtk.Align.CENTER)
        wrapper.set_halign(Gtk.Align.CENTER)
        icon = Gtk.Label(label="\U0001F304")
        icon.get_style_context().add_class("empty-icon")
        wrapper.pack_start(icon, False, False, 0)
        self.empty_title = Gtk.Label(label="No memories here yet")
        self.empty_title.get_style_context().add_class("empty-title")
        wrapper.pack_start(self.empty_title, False, False, 0)
        self.empty_sub = Gtk.Label(
            label="Add a folder from the sidebar, or drop media into ~/Pictures.")
        self.empty_sub.get_style_context().add_class("empty-sub")
        self.empty_sub.set_line_wrap(True)
        self.empty_sub.set_justify(Gtk.Justification.CENTER)
        wrapper.pack_start(self.empty_sub, False, False, 0)
        wrapper.show_all()
        return wrapper

    def set_empty_message(self, title, subtitle):
        self.empty_title.set_text(title)
        self.empty_sub.set_text(subtitle)

    # -- content ---------------------------------------------------------
    @property
    def container(self):
        return self.flow if self._mode == "grid" else self.film_box

    def get_scroll_widget(self):
        return self.scroll if self._mode == "grid" else self.film_scroll

    def set_items(self, items, reset_scroll=True):
        self._generation += 1
        self._items = list(items)
        valid = {item.path for item in self._items}
        self._selection = {p for p in self._selection if p in valid}
        if self._anchor not in valid:
            self._anchor = None
        self._clear_cells()
        if reset_scroll:
            for scroll in (self.scroll, self.film_scroll):
                scroll.get_vadjustment().set_value(0.0)
                scroll.get_hadjustment().set_value(0.0)
        if not self._items:
            self.stack.set_visible_child_name("empty")
            self.emit("selection-changed")
            return
        self.stack.set_visible_child_name("grid" if self._mode == "grid" else "film")
        self._schedule_grow(0)

    def set_view_mode(self, mode: str):
        if mode not in ("grid", "film") or mode == self._mode:
            return
        self._mode = mode
        self._rebuild_cells()
        self.stack.set_visible_child_name(mode)

    def set_select_mode(self, enabled: bool):
        self._select_mode = enabled

    def set_loading(self, loading: bool, message: str = None):
        self.loading = loading
        if loading:
            if self._shimmer_source is None:
                self._shimmer_source = GLib.timeout_add(90, self._tick_shimmer)
        elif self._shimmer_source is not None:
            GLib.source_remove(self._shimmer_source)
            self._shimmer_source = None
        if self._items:
            return
        if loading:
            self.set_empty_message("Looking for memories\u2026", message or "Scanning your folders")
            self.stack.set_visible_child_name("empty")
        else:
            self.set_empty_message("No memories here yet",
                                   "Add a folder from the sidebar, or drop media into ~/Pictures.")

    def _tick_shimmer(self):
        if not self.loading:
            self._shimmer_source = None
            return False
        self.shimmer_phase = (self.shimmer_phase + 0.06) % 1.4
        for cell in self._cells:
            if cell.pixbuf is None and cell.get_mapped():
                cell.area.queue_draw()
        return True

    # -- cells -----------------------------------------------------------
    def _clear_cells(self):
        for child in list(self.flow.get_children()):
            self.flow.remove(child)
        for child in list(self.film_box.get_children()):
            self.film_box.remove(child)
        self._cells = []
        self._built = 0

    def _rebuild_cells(self):
        saved = set(self._selection)
        self._clear_cells()
        self._selection = saved
        if self._items:
            self._schedule_grow(0)
        self.stack.set_visible_child_name("grid" if self._mode == "grid" else "film")

    def _schedule_grow(self, delay=16):
        if self._grow_source is not None:
            return
        self._grow_source = GLib.timeout_add(delay, self._grow)

    def _grow(self):
        self._grow_source = None
        generation = self._generation
        if self._built >= len(self._items):
            return False
        end = min(len(self._items), self._built + BATCH)
        for index in range(self._built, end):
            cell = self._make_cell(self._items[index])
            self._cells.append(cell)
            self.container.add(cell)
        self._built = end
        self.container.show_all()
        self._refresh_selection_visuals()
        if (self._built < len(self._items) and generation == self._generation
                and (self._built < INITIAL_FILL or self._needs_more())):
            self._schedule_grow(16)
        return False

    def _needs_more(self) -> bool:
        """True while the viewport is not yet filled (keeps memory bounded)."""
        try:
            scroll = self.get_scroll_widget()
            if self._mode == "grid":
                extent = scroll.get_vadjustment().get_upper()
                viewport = scroll.get_allocated_height()
            else:
                extent = scroll.get_hadjustment().get_upper()
                viewport = scroll.get_allocated_width()
        except Exception:
            return False
        return viewport < 40 or extent < viewport + 260

    def _make_cell(self, item):
        if self._mode == "grid":
            cell = ThumbCell(item, self, thumb_size=GRID_SIZE, width=GRID_SIZE, height=GRID_SIZE)
        else:
            cell = ThumbCell(item, self, thumb_size=FILM_W, width=FILM_W, height=FILM_H)
        if item.kind == "video":
            self._probe_duration(item, cell)
        return cell

    def _probe_duration(self, item, cell):
        if getattr(item, "duration", 0.0):
            return
        probe = getattr(self.thumbnailer, "probe_duration", None)
        if probe is None:
            return

        def _ready(seconds):
            if not seconds:
                return False
            item.duration = float(seconds)
            cell.refresh_subtitle()
            cell.area.queue_draw()
            return False

        probe(item.path, _ready)

    def _on_scrolled(self, adjustment):
        page = adjustment.get_page_size()
        upper = adjustment.get_upper()
        value = adjustment.get_value()
        if upper > 0 and value + page * 2.0 >= upper - 200:
            self._schedule_grow(0)
        if self._sweep_source is not None:
            GLib.source_remove(self._sweep_source)
        self._sweep_source = GLib.timeout_add(220, self._sweep)

    def _sweep(self):
        self._sweep_source = None
        scroll = self.get_scroll_widget()
        if self._mode == "grid":
            adj = scroll.get_vadjustment()
        else:
            adj = scroll.get_hadjustment()
        top = adj.get_value()
        bottom = top + adj.get_page_size()
        for cell in self._cells:
            if cell.pixbuf is None:
                continue
            try:
                ok, x, y = cell.translate_coordinates(self.container, 0, 0)
            except Exception:
                continue
            if not ok:
                continue
            position = y if self._mode == "grid" else x
            if position < top - 900 or position > bottom + 900:
                cell.release()
        return False

    # -- selection -------------------------------------------------------
    @property
    def items(self):
        return self._items

    def selected_paths(self) -> set:
        return set(self._selection)

    def get_selection(self) -> list:
        return [item for item in self._items if item.path in self._selection]

    def selection_count(self) -> int:
        return len(self._selection)

    def focus_item(self):
        return self.get_selection()[0] if self._selection else None

    def is_selected(self, item) -> bool:
        return item.path in self._selection

    def select_only(self, item):
        self._selection = {item.path}
        self._anchor = item.path
        self._refresh_selection_visuals()
        self.emit("selection-changed")

    def toggle_select(self, item, additive=False):
        if additive:
            if item.path in self._selection:
                self._selection.discard(item.path)
            else:
                self._selection.add(item.path)
                self._anchor = item.path
        else:
            if self._selection == {item.path}:
                self._selection.clear()
            else:
                self._selection = {item.path}
                self._anchor = item.path
        self._refresh_selection_visuals()
        self.emit("selection-changed")

    def select_range(self, item):
        paths = [i.path for i in self._items]
        if item.path not in paths:
            return self.select_only(item)
        end = paths.index(item.path)
        start = paths.index(self._anchor) if self._anchor in paths else end
        low, high = min(start, end), max(start, end)
        self._selection = set(paths[low:high + 1])
        self._refresh_selection_visuals()
        self.emit("selection-changed")

    def select_all(self):
        self._selection = {item.path for item in self._items}
        self._refresh_selection_visuals()
        self.emit("selection-changed")

    def clear_selection(self):
        if not self._selection:
            return
        self._selection.clear()
        self._refresh_selection_visuals()
        self.emit("selection-changed")

    def _refresh_selection_visuals(self):
        for cell in self._cells:
            cell.set_selected(cell.item.path in self._selection)

    def refresh_favorites(self):
        for cell in self._cells:
            cell.area.queue_draw()

    def remove_items(self, paths):
        paths = set(paths)
        if not paths:
            return
        self._items = [item for item in self._items if item.path not in paths]
        self._selection -= paths
        self._clear_cells()
        if not self._items:
            self.set_empty_message("No memories here yet",
                                   "Add a folder from the sidebar, or drop media into ~/Pictures.")
            self.stack.set_visible_child_name("empty")
        else:
            self.stack.set_visible_child_name("grid" if self._mode == "grid" else "film")
            self._schedule_grow(0)
        self.emit("selection-changed")

    def replace_item(self, old_path, new_item):
        for index, item in enumerate(self._items):
            if item.path == old_path:
                self._items[index] = new_item
                break
        else:
            self._items.append(new_item)
        self._clear_cells()
        self._schedule_grow(0)

    def scroll_to_path(self, path):
        for cell in self._cells:
            if cell.item.path == path:
                try:
                    cell.grab_focus()
                except Exception:
                    pass
                return

    def shutdown(self):
        for source_attr in ("_sweep_source", "_grow_source", "_shimmer_source"):
            source = getattr(self, source_attr)
            if source is not None:
                try:
                    GLib.source_remove(source)
                except Exception:
                    pass
                setattr(self, source_attr, None)
