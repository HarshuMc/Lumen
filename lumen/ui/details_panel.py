"""LUMEN details panel - Microsoft Photos style metadata rail."""

from __future__ import annotations

import os
import threading

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

from gen_icon import rounded_rect  # noqa: E402
from core.scanner import human_duration, read_exif_date  # noqa: E402
from core.thumbnailer import histogram, image_info  # noqa: E402
from .settings import palette  # noqa: E402
from .gallery_view import draw_text  # noqa: E402


class DetailsPanel(Gtk.Box):
    """Slide-in inspector: preview, metadata, EXIF date and RGB histogram."""

    __gsignals__ = {
        "action": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        "favorite": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, thumbnailer, dispatch=None):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.thumbnailer = thumbnailer
        self.dispatch = dispatch or GLib.idle_add
        self.item = None
        self._pixbuf = None
        self._hist = None
        self._generation = 0
        self.set_size_request(320, -1)
        self.get_style_context().add_class("details")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label="DETAILS")
        title.get_style_context().add_class("section-label")
        title.set_xalign(0.0)
        head.pack_start(title, True, True, 0)
        close = Gtk.Button(label="\u2715")
        close.get_style_context().add_class("icon-btn")
        close.set_tooltip_text("Hide details")
        close.connect("clicked", lambda *_: self.emit("action", "details-close", self.item))
        head.pack_end(close, False, False, 0)
        self.pack_start(head, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.NONE)
        scroller.get_style_context().add_class("gallery-scroll")
        self.pack_start(scroller, True, True, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_valign(Gtk.Align.START)
        scroller.add(body)

        self.preview_area = Gtk.DrawingArea()
        self.preview_area.set_size_request(280, 168)
        self.preview_area.connect("draw", self._draw_preview)
        body.pack_start(self.preview_area, False, False, 0)

        self.title_label = Gtk.Label(label="Nothing selected")
        self.title_label.get_style_context().add_class("details-title")
        self.title_label.set_xalign(0.0)
        self.title_label.set_line_wrap(True)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.title_label.set_max_width_chars(30)
        body.pack_start(self.title_label, False, False, 0)

        self.sub_label = Gtk.Label(label="Pick a photo, video or document")
        self.sub_label.get_style_context().add_class("details-sub")
        self.sub_label.set_xalign(0.0)
        self.sub_label.set_line_wrap(True)
        body.pack_start(self.sub_label, False, False, 0)

        badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.kind_pill = Gtk.Label(label="")
        self.kind_pill.get_style_context().add_class("side-count")
        badges.pack_start(self.kind_pill, False, False, 0)
        self.fav_button = Gtk.Button(label="\u2661 Favorite")
        self.fav_button.get_style_context().add_class("fav-btn")
        self.fav_button.connect("clicked", lambda *_: self.emit("favorite", self.item))
        badges.pack_end(self.fav_button, False, False, 0)
        body.pack_start(badges, False, False, 0)

        meta_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        meta_box.get_style_context().add_class("detail-box")
        body.pack_start(meta_box, False, False, 0)
        self._meta_labels = {}
        for key, label in (
            ("type", "Type"),
            ("size", "File size"),
            ("dimensions", "Dimensions"),
            ("duration", "Duration"),
            ("date", "Modified"),
            ("exif", "Taken"),
            ("folder", "Folder"),
        ):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.get_style_context().add_class("meta-row")
            name = Gtk.Label(label=label)
            name.get_style_context().add_class("meta-key")
            name.set_xalign(0.0)
            name.set_size_request(74, -1)
            value = Gtk.Label(label="-")
            value.get_style_context().add_class("meta-value")
            value.set_xalign(0.0)
            value.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            value.set_max_width_chars(28)
            row.pack_start(name, False, False, 0)
            row.pack_start(value, True, True, 0)
            meta_box.pack_start(row, False, False, 0)
            self._meta_labels[key] = value

        section = Gtk.Label(label="TONE  \u00b7  RGB HISTOGRAM")
        section.get_style_context().add_class("section-label")
        section.set_xalign(0.0)
        body.pack_start(section, False, False, 4)

        self.hist_area = Gtk.DrawingArea()
        self.hist_area.set_size_request(280, 76)
        self.hist_area.connect("draw", self._draw_histogram)
        body.pack_start(self.hist_area, False, False, 0)

        path_label = Gtk.Label(label="")
        path_label.get_style_context().add_class("details-sub")
        path_label.set_xalign(0.0)
        path_label.set_line_wrap(True)
        path_label.set_selectable(True)
        path_label.set_max_width_chars(34)
        body.pack_start(path_label, False, False, 0)
        self.path_label = path_label

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.action_buttons = {}
        for key, label in (("open", "Open"), ("reveal", "Reveal"), ("trash", "Bin")):
            button = Gtk.Button(label=label)
            button.get_style_context().add_class("ghost-btn")
            button.connect("clicked", lambda _b, k=key: self.emit("action", k, self.item))
            actions.pack_start(button, True, True, 0)
            self.action_buttons[key] = button
        body.pack_start(actions, False, False, 6)

        self.set_item(None)

    # -- public ----------------------------------------------------------
    def set_item(self, item, favorite=False):
        self._generation += 1
        generation = self._generation
        self.item = item
        self._pixbuf = None
        self._hist = None
        self.hist_area.queue_draw()
        self.preview_area.queue_draw()

        if item is None:
            self.title_label.set_text("Nothing selected")
            self.sub_label.set_text("Pick a photo, video or document")
            self.kind_pill.set_text("")
            self.fav_button.set_label("\u2661 Favorite")
            self.path_label.set_text("")
            for key, label in self._meta_labels.items():
                label.set_text("-")
            for button in self.action_buttons.values():
                button.set_sensitive(False)
            return

        for button in self.action_buttons.values():
            button.set_sensitive(True)
        self.title_label.set_text(item.name)
        self.sub_label.set_text(self._subtitle(item))
        self.kind_pill.set_text(item.kind_label.upper())
        self.fav_button.set_label("\u2665 Favorited" if favorite else "\u2661 Favorite")
        self.path_label.set_text(item.path)
        self._fill_meta(item)

        self.thumbnailer.request(item, self._preview_ready, 320)
        self._start_probe(item, generation)

    def refresh_favorite(self, favorite):
        self.fav_button.set_label("\u2665 Favorited" if favorite else "\u2661 Favorite")

    # -- internals -------------------------------------------------------
    @staticmethod
    def _subtitle(item):
        bits = [item.kind_label, item.size_text]
        if getattr(item, "is_screenshot", False):
            bits.insert(0, "Screenshot")
        return "  \u00b7  ".join(b for b in bits if b)

    def _fill_meta(self, item):
        self._meta_labels["type"].set_text(
            (item.ext.lstrip(".").upper() or item.kind_label))
        self._meta_labels["size"].set_text(item.size_text)
        self._meta_labels["dimensions"].set_text(item.dimensions)
        self._meta_labels["duration"].set_text(human_duration(
            getattr(item, "duration", 0.0) or 0.0) or "-")
        self._meta_labels["date"].set_text(item.date_text)
        self._meta_labels["exif"].set_text("-")
        folder = getattr(item, "folder", "")
        self._meta_labels["folder"].set_text(os.path.basename(folder.rstrip(os.sep)) or folder)
        self._meta_labels["folder"].set_tooltip_text(folder)

    def _preview_ready(self, pixbuf):
        if pixbuf is None or self.item is None:
            return False
        self._pixbuf = pixbuf
        self.preview_area.queue_draw()
        return False

    def _start_probe(self, item, generation):
        thumbnailer = self.thumbnailer

        def worker():
            data = {}
            try:
                if item.kind == "image":
                    width, height, fmt = image_info(item.path)
                    data["width"] = width
                    data["height"] = height
                    data["format"] = fmt
                    data["exif"] = read_exif_date(item.path)
                    data["hist"] = histogram(item.path, 48)
                elif item.kind == "video":
                    seconds = thumbnailer.duration(item)
                    if not seconds:
                        seconds = thumbnailer._probe(item.path) or 0.0
                        if seconds:
                            with thumbnailer._lock:
                                thumbnailer._probed[item.path] = seconds
                                thumbnailer._meta_dirty = True
                    data["duration"] = seconds
            except Exception:
                pass
            self.dispatch(self._apply_probe, generation, data)

        threading.Thread(target=worker, name="lumen-meta", daemon=True).start()

    def _apply_probe(self, generation, data):
        if generation != self._generation or self.item is None:
            return False
        item = self.item
        if data.get("width"):
            item.width = int(data["width"])
            item.height = int(data["height"] or 0)
            self._meta_labels["dimensions"].set_text(item.dimensions)
        if data.get("format"):
            self._meta_labels["type"].set_text(str(data["format"]).upper())
        if data.get("exif"):
            self._meta_labels["exif"].set_text(str(data["exif"]))
        if data.get("duration"):
            item.duration = float(data["duration"])
            self._meta_labels["duration"].set_text(
                human_duration(item.duration) or "-")
        if data.get("hist"):
            self._hist = data["hist"]
            self.hist_area.queue_draw()
        return False

    # -- painting --------------------------------------------------------
    def _draw_preview(self, area, cr):
        allocation = area.get_allocation()
        width, height = max(1, allocation.width), max(1, allocation.height)
        radius = 13.0
        rounded_rect(cr, 0, 0, width, height, radius)
        cr.save()
        cr.clip()
        pal = palette()
        if self._pixbuf is not None:
            pixbuf = self._pixbuf
            pw, ph = max(1, pixbuf.get_width()), max(1, pixbuf.get_height())
            scale = max(width / float(pw), height / float(ph))
            cr.translate((width - pw * scale) / 2.0, (height - ph * scale) / 2.0)
            cr.scale(scale, scale)
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
            cr.paint()
        else:
            gradient = cairo.LinearGradient(0, 0, width, height)
            accent = pal.get("accent", (0.48, 0.23, 0.93, 1.0))
            gradient.add_color_stop_rgba(0.0, accent[0], accent[1], accent[2], 0.28)
            gradient.add_color_stop_rgba(1.0, accent[0], accent[1], accent[2], 0.05)
            cr.set_source(gradient)
            cr.rectangle(0, 0, width, height)
            cr.fill()
            draw_text(cr, "\U0001F5BC", width / 2, height / 2 - 14, 22,
                      (1, 1, 1, 0.45), anchor="middle")
            draw_text(cr, "No preview", width / 2, height / 2 + 18, 8.5,
                      (1, 1, 1, 0.35), anchor="middle")
        cr.restore()
        return False

    def _draw_histogram(self, area, cr):
        allocation = area.get_allocation()
        width, height = max(1, allocation.width), max(1, allocation.height)
        pal = palette()
        rounded_rect(cr, 0, 0, width, height, 10)
        cr.save()
        cr.clip()
        cr.set_source_rgba(*pal.get("plate", (1, 1, 1, 0.06)))
        cr.rectangle(0, 0, width, height)
        cr.fill()

        if not self._hist:
            draw_text(cr, "No tone data", width / 2, height / 2 - 5, 8.5,
                      (1, 1, 1, 0.30), anchor="middle")
            cr.restore()
            return False

        red, green, blue, peak = self._hist
        channels = (
            (red, (0.98, 0.42, 0.42, 0.55)),
            (green, (0.36, 0.85, 0.52, 0.55)),
            (blue, (0.45, 0.60, 0.98, 0.55)),
        )
        bins = len(red)
        step = width / float(max(1, bins))
        for values, rgba in channels:
            cr.set_source_rgba(*rgba)
            cr.move_to(0, height)
            for index, value in enumerate(values):
                x = index * step
                bar = (value / float(peak)) * (height - 8)
                cr.line_to(x, height - bar)
                cr.line_to(x + step, height - bar)
            cr.line_to(width, height)
            cr.close_path()
            cr.fill()

        cr.set_source_rgba(1, 1, 1, 0.16)
        cr.rectangle(0, height - 1, width, 1)
        cr.fill()
        cr.restore()
        return False
