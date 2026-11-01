"""LUMEN main window.

Glass shell with hand made window controls (drag / resize / traffic lights),
Samsung-style navigation, the media grid, the lightbox and every batch action.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import urllib.parse

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from core.scanner import (  # noqa: E402
    MediaItem, Scanner, default_roots, documents_roots, home_dir,
    is_under, iter_folder, kind_of,
)
from core.storage import Storage, thumbs_dir, unique_path  # noqa: E402
from core.thumbnailer import Thumbnailer  # noqa: E402
from gen_icon import draw_logo  # noqa: E402
from ui.details_panel import DetailsPanel  # noqa: E402
from ui.gallery_view import AlbumCard, GalleryView  # noqa: E402
from ui.settings import Appearance  # noqa: E402
from ui.sidebar import Sidebar  # noqa: E402
from ui.viewer import Viewer  # noqa: E402

RESIZE_EDGE = 7
SORT_OPTIONS = (("date", "Date"), ("name", "Name"), ("size", "Size"), ("type", "Type"))
PAGE_TITLES = {
    "all": ("All Media", "Everything LUMEN found in your folders"),
    "images": ("Images", "Photos, screenshots and artwork"),
    "videos": ("Videos", "Clips and movies"),
    "documents": ("Documents", "Files from Documents and screenshots"),
    "favorites": ("Favorites", "The memories you starred"),
    "trash": ("Recycle Bin", "Restore anything you removed by mistake"),
    "albums": ("Albums", "Auto albums and your own collections"),
}


def make_logo_area(size=22):
    area = Gtk.DrawingArea()
    area.set_size_request(size, size)
    area.set_valign(Gtk.Align.CENTER)

    def _draw(_widget, cr):
        draw_logo(cr, size)
        return False

    area.connect("draw", _draw)
    return area


class TrafficLights(Gtk.DrawingArea):
    """macOS style window buttons, painted with Cairo."""

    COLORS = (
        (1.0, 0.373, 0.341),
        (0.996, 0.737, 0.180),
        (0.157, 0.792, 0.259),
    )
    LABELS = ("\u00d7", "\u2212", "+")

    def __init__(self, window):
        Gtk.DrawingArea.__init__(self)
        self._window = window
        self._hover = -1
        self._last_green = 0.0
        self.set_size_request(64, 22)
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK |
                        Gdk.EventMask.LEAVE_NOTIFY_MASK |
                        Gdk.EventMask.BUTTON_PRESS_MASK)
        self.set_tooltip_text("Close   \u00b7   Minimize   \u00b7   Maximize (double click: fullscreen)")
        self.connect("draw", self._draw)
        self.connect("motion-notify-event", self._motion)
        self.connect("leave-notify-event", self._leave)
        self.connect("button-press-event", self._press)

    @staticmethod
    def _centres():
        return ((11.0, 11.0), (31.0, 11.0), (51.0, 11.0))

    def _index_at(self, x, y):
        for index, (cx, cy) in enumerate(self._centres()):
            if (x - cx) ** 2 + (y - cy) ** 2 <= 81:
                return index
        return -1

    def _draw(self, _widget, cr):
        for index, (cx, cy) in enumerate(self._centres()):
            red, green, blue = self.COLORS[index]
            if index == self._hover:
                cr.set_source_rgba(red, green, blue, 0.35)
                cr.arc(cx, cy, 7.6, 0, 6.2832)
                cr.fill()
            cr.set_source_rgb(red, green, blue)
            cr.arc(cx, cy, 6.4, 0, 6.2832)
            cr.fill()
            if index == self._hover:
                cr.set_source_rgba(0.05, 0.05, 0.05, 0.65)
                cr.set_line_width(1.2)
                if index == 0:
                    cr.move_to(cx - 2.4, cy - 2.4)
                    cr.line_to(cx + 2.4, cy + 2.4)
                    cr.move_to(cx + 2.4, cy - 2.4)
                    cr.line_to(cx - 2.4, cy + 2.4)
                    cr.stroke()
                elif index == 1:
                    cr.move_to(cx - 3.0, cy)
                    cr.line_to(cx + 3.0, cy)
                    cr.stroke()
                else:
                    cr.move_to(cx - 3.0, cy)
                    cr.line_to(cx + 3.0, cy)
                    cr.move_to(cx, cy - 3.0)
                    cr.line_to(cx, cy + 3.0)
                    cr.stroke()
        return False

    def _motion(self, _widget, event):
        index = self._index_at(event.x, event.y)
        if index != self._hover:
            self._hover = index
            self.queue_draw()
        return False

    def _leave(self, *_args):
        if self._hover != -1:
            self._hover = -1
            self.queue_draw()
        return False

    def _press(self, _widget, event):
        if event.button != 1:
            return False
        index = self._index_at(event.x, event.y)
        if index == 0:
            Gtk.main_quit()
            return True
        if index == 1:
            self._window.iconify()
            return True
        if index == 2:
            now = time.time()
            if now - self._last_green < 0.45:
                self._window.toggle_fullscreen()
            else:
                self._window.toggle_maximize()
            self._last_green = now
            return True
        return False


class LumenWindow(Gtk.Window):
    """The whole application shell."""

    def __init__(self, store: Storage, start_path: str = None):
        Gtk.Window.__init__(self, title="LUMEN")
        self.store = store
        self._destroyed = False

        self.set_default_size(1100, 700)
        self.set_size_request(900, 600)
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        try:
            self.set_wmclass("lumen", "LUMEN")
        except Exception:
            pass

        screen = self.get_screen()
        visual = None
        try:
            visual = screen.get_rgba_visual()
        except Exception:
            visual = None
        if visual is not None and screen.is_composited():
            self.set_visual(visual)
        self.get_style_context().add_class("lumen")

        self.appearance = Appearance(screen, store.get("theme"), store.get("opacity"))

        # -- state -------------------------------------------------------
        self.sort = store.get("sort", "date")
        self.view_mode = store.get("view", "grid")
        self.tab = store.get("last_tab", "all")
        self.filter_folder = None
        self.filter_album = None
        self.search_text = ""
        self.details_open = bool(store.get("details_open", False))
        self._items = {}
        self.trash_items = []
        self._focus_item = None
        self._scanning = False
        self._loading_visible = False
        self._refresh_source = None
        self._search_source = None
        self._toast_source = None
        self._sweep_source = None
        self._fs_sources = {}
        self._monitors = []
        self._maximized = False
        self._fullscreen = False
        self._saved_geometry = None
        self._hover_edge = None
        self._cursors = {}
        self._select_mode = False
        self._albums_built = False

        self.thumbnailer = Thumbnailer(cache_root=thumbs_dir(), dispatch=self._dispatch)
        self.scanner = Scanner(
            dispatch=self._dispatch,
            on_batch=self._on_scan_batch,
            on_done=self._on_scan_done,
            on_progress=self._on_scan_progress,
        )

        self._build_ui()
        self._build_icon()

        # restore persisted view mode and tab into the widgets
        if self.view_mode == "film":
            self.gallery.set_view_mode("film")
        sidebar_keys = {"all", "images", "videos", "documents", "albums",
                        "favorites", "trash"}
        if self.tab not in sidebar_keys:
            self.tab = "all"
        self.sidebar.select(self.tab, silent=True)

        self._load_trash()
        self._refresh(reset_scroll=True)

        self.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_window_press)
        self.connect("motion-notify-event", self._on_window_motion)
        self.connect("leave-notify-event", self._on_window_leave)
        self.connect("key-press-event", self._on_key_press)
        self.connect("window-state-event", self._on_window_state)
        self.connect("destroy", self._on_destroy)
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._on_drag_data)

        self.show_all()
        self.viewer.hide()
        self.viewer.set_no_show_all(True)
        self.action_revealer.set_reveal_child(False)
        self.toast_revealer.set_reveal_child(False)
        self._update_action_bar(0)
        self.details_revealer.set_reveal_child(self.details_open)

        GLib.idle_add(self._start_scan)
        GLib.idle_add(self._setup_monitors)
        if start_path:
            GLib.idle_add(self._open_start_path, start_path)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.overlay = Gtk.Overlay()
        self.add(self.overlay)

        self.container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.container.get_style_context().add_class("main-container")
        self.overlay.add(self.container)

        self.header = self._build_header()
        self.container.pack_start(self.header, False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.get_style_context().add_class("body")
        self.container.pack_start(body, True, True, 0)

        self.sidebar = Sidebar()
        self.sidebar.connect("navigate", self._on_navigate)
        self.sidebar.connect("add-folder", lambda *_: self._add_folder_dialog())
        self.sidebar.connect("remove-folder", self._on_remove_folder)
        body.pack_start(self.sidebar, False, False, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.get_style_context().add_class("content")
        content.set_hexpand(True)
        body.pack_start(content, True, True, 0)

        content.pack_start(self._build_content_header(), False, False, 0)

        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.set_transition_duration(120)
        content.pack_start(self.content_stack, True, True, 0)

        self.gallery = GalleryView(self.thumbnailer, dispatch=self._dispatch)
        self.gallery.favorite_check = self.store.is_favorite
        self.gallery.connect("item-activated", self._on_item_activated)
        self.gallery.connect("item-focused", self._on_item_focused)
        self.gallery.connect("selection-changed", self._on_selection_changed)
        self.gallery.connect("context-menu", self._on_context_menu)
        self.content_stack.add_named(self.gallery, "media")

        self.albums_page = self._build_albums_page()
        self.content_stack.add_named(self.albums_page, "albums")
        self.content_stack.set_visible_child_name("media")

        self.details = DetailsPanel(self.thumbnailer, dispatch=self._dispatch)
        self.details.connect("action", self._on_details_action)
        self.details.connect("favorite", lambda _p, item: self._toggle_favorite(item))
        self.details_revealer = Gtk.Revealer()
        self.details_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.details_revealer.set_transition_duration(180)
        self.details_revealer.add(self.details)
        body.pack_start(self.details_revealer, False, False, 0)

        # overlays ------------------------------------------------------
        self._build_loading_overlay()
        self._build_action_bar()

        self.toast_label = Gtk.Label(label="")
        self.toast_label.get_style_context().add_class("toast")
        self.toast_revealer = Gtk.Revealer()
        self.toast_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.toast_revealer.set_transition_duration(160)
        self.toast_revealer.add(self.toast_label)
        self.toast_revealer.set_halign(Gtk.Align.CENTER)
        self.toast_revealer.set_valign(Gtk.Align.START)
        self.toast_revealer.set_margin_top(62)
        self.overlay.add_overlay(self.toast_revealer)

        self.viewer = Viewer(self.thumbnailer)
        self.viewer.favorite_check = self.store.is_favorite
        self.viewer.set_halign(Gtk.Align.FILL)
        self.viewer.set_valign(Gtk.Align.FILL)
        self.viewer.connect("action", self._on_viewer_action)
        self.viewer.connect("closed", self._on_viewer_closed)
        self.viewer.connect("button-press-event", lambda *_: True)
        self.overlay.add_overlay(self.viewer)

        self.sidebar.set_folders(self.store.custom_folders)

    def _build_header(self):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.get_style_context().add_class("header")
        header.set_size_request(-1, 48)
        header.connect("button-press-event", self._on_header_press)

        self.traffic = TrafficLights(self)
        header.pack_start(self.traffic, False, False, 0)

        title_block = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        title_block.set_halign(Gtk.Align.CENTER)
        title_block.set_hexpand(True)
        title_block.pack_start(make_logo_area(22), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        texts.set_valign(Gtk.Align.CENTER)
        app_name = Gtk.Label(label="LUMEN")
        app_name.get_style_context().add_class("app-name")
        app_name.set_xalign(0.0)
        texts.pack_start(app_name, False, False, 0)
        tagline = Gtk.Label(label="Your Memories, Clearly.")
        tagline.get_style_context().add_class("tagline")
        tagline.set_xalign(0.0)
        texts.pack_start(tagline, False, False, 0)
        title_block.pack_start(texts, False, False, 0)
        header.pack_start(title_block, True, True, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        controls.set_valign(Gtk.Align.CENTER)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search memories")
        self.search_entry.get_style_context().add_class("search")
        self.search_entry.set_size_request(180, -1)
        self.search_entry.connect("search-changed", self._on_search_changed)
        controls.pack_start(self.search_entry, False, False, 0)

        opacity_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        opacity_icon = Gtk.Label(label="\u25d0")
        opacity_icon.get_style_context().add_class("tagline")
        opacity_box.pack_start(opacity_icon, False, False, 0)
        self.opacity_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.40, 1.00, 0.01)
        self.opacity_scale.get_style_context().add_class("opacity-scale")
        self.opacity_scale.set_size_request(88, -1)
        self.opacity_scale.set_draw_value(False)
        self.opacity_scale.set_value(float(self.store.get("opacity", 0.82)))
        self.opacity_scale.set_tooltip_text("Glass opacity")
        self.opacity_scale.connect("value-changed", self._on_opacity_changed)
        self.opacity_scale.connect("change", self._on_opacity_commit)
        opacity_box.pack_start(self.opacity_scale, False, False, 0)
        controls.pack_start(opacity_box, False, False, 4)

        self.theme_button = Gtk.Button(label="\u263d" if self.store.get("theme") == "light" else "\u2600")
        self.theme_button.get_style_context().add_class("icon-btn")
        self.theme_button.set_tooltip_text("Toggle light / dark theme")
        self.theme_button.connect("clicked", self._on_theme_clicked)
        controls.pack_start(self.theme_button, False, False, 0)

        self.bin_button = Gtk.Button(label="\U0001F5D1")
        self.bin_button.get_style_context().add_class("icon-btn")
        self.bin_button.set_tooltip_text("Recycle Bin")
        self.bin_button.connect("clicked", lambda *_: self._goto_tab("trash"))
        controls.pack_start(self.bin_button, False, False, 0)

        header.pack_end(controls, False, False, 0)
        return header

    def _build_content_header(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.get_style_context().add_class("content-header")

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.page_title = Gtk.Label(label="All Media")
        self.page_title.get_style_context().add_class("page-title")
        self.page_title.set_xalign(0.0)
        titles.pack_start(self.page_title, False, False, 0)
        self.page_sub = Gtk.Label(label="")
        self.page_sub.get_style_context().add_class("page-sub")
        self.page_sub.set_xalign(0.0)
        self.page_sub.set_ellipsize(Pango.EllipsizeMode.END)
        self.page_sub.set_max_width_chars(46)
        titles.pack_start(self.page_sub, False, False, 0)
        bar.pack_start(titles, True, True, 0)

        self.back_button = Gtk.Button(label="\u2190 Albums")
        self.back_button.get_style_context().add_class("chip")
        self.back_button.set_no_show_all(True)
        self.back_button.connect("clicked", lambda *_: self._goto_tab("albums"))
        bar.pack_end(self.back_button, False, False, 0)

        self.empty_bin_button = Gtk.Button(label="Empty bin")
        self.empty_bin_button.get_style_context().add_class("chip")
        self.empty_bin_button.set_no_show_all(True)
        self.empty_bin_button.connect("clicked", lambda *_: self._empty_bin())
        bar.pack_end(self.empty_bin_button, False, False, 0)

        self.sort_combo = Gtk.ComboBoxText()
        self.sort_combo.set_tooltip_text("Sort by")
        for index, (key, label) in enumerate(SORT_OPTIONS):
            self.sort_combo.append(key, label)
            if key == self.sort:
                self.sort_combo.set_active(index)
        if self.sort_combo.get_active() < 0:
            self.sort_combo.set_active(0)
        self.sort_combo.connect("changed", self._on_sort_changed)
        bar.pack_end(self.sort_combo, False, False, 0)

        segmented = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        segmented.get_style_context().add_class("segmented")
        self.grid_button = Gtk.ToggleButton(label="\u25a6")
        self.grid_button.get_style_context().add_class("seg-btn")
        self.grid_button.set_tooltip_text("Grid view")
        self.film_button = Gtk.ToggleButton(label="\u25a4")
        self.film_button.get_style_context().add_class("seg-btn")
        self.film_button.set_tooltip_text("Filmstrip view")
        self.film_button.set_active(self.view_mode == "film")
        self.grid_button.set_active(self.view_mode != "film")
        self.grid_button.connect("toggled", self._on_view_toggled, "grid")
        self.film_button.connect("toggled", self._on_view_toggled, "film")
        segmented.pack_start(self.grid_button, False, False, 0)
        segmented.pack_start(self.film_button, False, False, 0)
        bar.pack_end(segmented, False, False, 0)

        self.select_toggle = Gtk.ToggleButton(label="Select")
        self.select_toggle.get_style_context().add_class("chip")
        self.select_toggle.set_tooltip_text("Select multiple (Ctrl+A)")
        self.select_toggle.connect("toggled", self._on_select_toggled)
        bar.pack_end(self.select_toggle, False, False, 0)
        return bar

    def _build_albums_page(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.NONE)
        scroll.get_style_context().add_class("gallery-scroll")
        self.albums_flow = Gtk.FlowBox()
        self.albums_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.albums_flow.set_max_children_per_line(6)
        self.albums_flow.set_min_children_per_line(1)
        self.albums_flow.set_column_spacing(10)
        self.albums_flow.set_row_spacing(10)
        self.albums_flow.set_valign(Gtk.Align.START)
        self.albums_flow.get_style_context().add_class("gallery-flow")
        scroll.add(self.albums_flow)
        self.albums_empty = Gtk.Label(label="No albums yet \u2014 select items and choose \u201cAdd to album\u201d.")
        self.albums_empty.get_style_context().add_class("empty-sub")
        self.albums_empty.set_no_show_all(True)
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        wrapper.pack_start(scroll, True, True, 0)
        wrapper.pack_start(self.albums_empty, False, False, 12)
        return wrapper

    def _build_loading_overlay(self):
        self.loading_overlay = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.loading_overlay.get_style_context().add_class("loading-overlay")
        self.loading_overlay.set_halign(Gtk.Align.FILL)
        self.loading_overlay.set_valign(Gtk.Align.FILL)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_halign(Gtk.Align.CENTER)
        inner.set_valign(Gtk.Align.CENTER)
        self.loading_spinner = Gtk.Spinner()
        self.loading_spinner.set_size_request(28, 28)
        inner.pack_start(self.loading_spinner, False, False, 0)
        self.loading_label = Gtk.Label(label="Scanning your folders\u2026")
        self.loading_label.get_style_context().add_class("loading-label")
        inner.pack_start(self.loading_label, False, False, 0)
        self.loading_sub = Gtk.Label(label="0 files")
        self.loading_sub.get_style_context().add_class("loading-sub")
        self.loading_sub.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.loading_sub.set_max_width_chars(48)
        inner.pack_start(self.loading_sub, False, False, 0)
        self.loading_overlay.pack_start(inner, True, True, 0)
        self.overlay.add_overlay(self.loading_overlay)

    def _build_action_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bar.get_style_context().add_class("action-bar")
        self.action_count = Gtk.Label(label="0 selected")
        self.action_count.get_style_context().add_class("action-count")
        bar.pack_start(self.action_count, False, False, 0)

        self.ab_buttons = {}
        definitions = (
            ("favorite", "\u2665 Favorite", lambda: self._favorite_selection(True)),
            ("album", "\uff0b Album", lambda: self._add_selection_to_album()),
            ("copy", "\u29c9 Copy path", self._copy_selection_paths),
            ("wallpaper", "\U0001F5BC Wallpaper", self._wallpaper_selection),
            ("restore", "\u21ba Restore", self._restore_selection),
            ("bin", "\U0001F5D1 Bin", self._trash_selection),
            ("delete", "\u2716 Delete forever", self._delete_selection_forever),
            ("clear", "\u2715", lambda: self.gallery.clear_selection()),
        )
        for key, label, callback in definitions:
            button = Gtk.Button(label=label)
            button.get_style_context().add_class("ab-btn")
            if key in ("bin", "delete"):
                button.get_style_context().add_class("danger")
            button.connect("clicked", lambda _b, cb=callback: cb())
            bar.pack_start(button, False, False, 0)
            self.ab_buttons[key] = button

        self.action_revealer = Gtk.Revealer()
        self.action_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.action_revealer.set_transition_duration(170)
        self.action_revealer.add(bar)
        self.action_revealer.set_halign(Gtk.Align.CENTER)
        self.action_revealer.set_valign(Gtk.Align.END)
        self.action_revealer.set_margin_bottom(18)
        self.overlay.add_overlay(self.action_revealer)

    def _build_icon(self):
        try:
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 128, 128)
            cr = cairo.Context(surface)
            cr.set_antialias(cairo.ANTIALIAS_BEST)
            draw_logo(cr, 128, opaque=True)
            pixbuf = Gdk.pixbuf_get_from_surface(surface, 0, 0, 128, 128)
            if pixbuf is not None:
                self.set_icon(pixbuf)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Window frame: transparent clear, drag, resize
    # ------------------------------------------------------------------
    def _on_draw(self, _widget, cr):
        # Removing the window background first is what kills the black rounded
        # corner pixels on picom/LXQt - the CSS plate is painted by children.
        cr.save()
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.restore()
        return False

    def _on_header_press(self, widget, event):
        if event.button != 1:
            return False
        edge = None
        if not (self._maximized or self._fullscreen):
            try:
                ok, window_x, window_y = widget.translate_coordinates(self, event.x, event.y)
                edge = self._edge_at(window_x, window_y) if ok else None
            except Exception:
                edge = None
        if edge is not None:
            try:
                self.get_window().begin_resize_drag(edge, event.button,
                                                    int(event.x_root), int(event.y_root),
                                                    event.time)
                return True
            except Exception:
                pass
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self.toggle_maximize()
            return True
        try:
            window = self.get_window()
            if window is not None:
                window.begin_move_drag(event.button, int(event.x_root),
                                       int(event.y_root), event.time)
            return True
        except Exception:
            return False

    def _edge_at(self, x, y):
        width = self.get_allocated_width()
        height = self.get_allocated_height()
        left = x <= RESIZE_EDGE
        right = x >= width - RESIZE_EDGE
        top = y <= RESIZE_EDGE
        bottom = y >= height - RESIZE_EDGE
        if top and left:
            return Gdk.WindowEdge.NORTH_WEST
        if top and right:
            return Gdk.WindowEdge.NORTH_EAST
        if bottom and left:
            return Gdk.WindowEdge.SOUTH_WEST
        if bottom and right:
            return Gdk.WindowEdge.SOUTH_EAST
        if top:
            return Gdk.WindowEdge.NORTH
        if bottom:
            return Gdk.WindowEdge.SOUTH
        if left:
            return Gdk.WindowEdge.WEST
        if right:
            return Gdk.WindowEdge.EAST
        return None

    _CURSOR_NAMES = {
        "NORTH": "n-resize", "SOUTH": "s-resize",
        "EAST": "e-resize", "WEST": "w-resize",
        "NORTH_WEST": "nw-resize", "NORTH_EAST": "ne-resize",
        "SOUTH_WEST": "sw-resize", "SOUTH_EAST": "se-resize",
    }

    def _cursor_for(self, edge):
        if edge is None:
            return None
        name = self._CURSOR_NAMES.get(str(edge).split(".")[-1])
        if not name:
            return None
        if name not in self._cursors:
            try:
                self._cursors[name] = Gdk.Cursor.new_from_name(self.get_display(), name)
            except Exception:
                self._cursors[name] = None
        return self._cursors.get(name)

    def _on_window_motion(self, _widget, event):
        if self._maximized or self._fullscreen:
            return False
        edge = self._edge_at(event.x, event.y)
        if edge != self._hover_edge:
            self._hover_edge = edge
            gdk_window = self.get_window()
            if gdk_window is not None:
                gdk_window.set_cursor(self._cursor_for(edge))
        return False

    def _on_window_leave(self, *_args):
        self._hover_edge = None
        gdk_window = self.get_window()
        if gdk_window is not None:
            gdk_window.set_cursor(None)
        return False

    def _on_window_press(self, _widget, event):
        if event.button != 1 or self._maximized or self._fullscreen:
            return False
        edge = self._edge_at(event.x, event.y)
        if edge is None:
            return False
        # leaves the click available to a header drag if no resize happens
        try:
            window = self.get_window()
            if window is not None:
                window.begin_resize_drag(edge, event.button, int(event.x_root),
                                         int(event.y_root), event.time)
            return True
        except Exception:
            return False

    def _on_window_state(self, _widget, event):
        was_maximized = self._maximized
        state = event.new_window_state
        self._maximized = bool(state & Gdk.WindowState.MAXIMIZED)
        self._fullscreen = bool(state & Gdk.WindowState.FULLSCREEN)
        if self._maximized and not was_maximized:
            try:
                self._saved_geometry = (self.get_size(), self.get_position())
            except Exception:
                self._saved_geometry = None
        if was_maximized and not self._maximized and self._saved_geometry:
            GLib.idle_add(self._restore_geometry)
        return False

    def _restore_geometry(self):
        if not self._saved_geometry:
            return False
        try:
            (width, height), (x, y) = self._saved_geometry
            if width > 200 and height > 150:
                self.resize(width, height)
            if x > -10000 and y > -10000:
                self.move(x, y)
        except Exception:
            pass
        return False

    def toggle_maximize(self):
        if self._fullscreen:
            self.unfullscreen()
        elif self._maximized:
            self.unmaximize()
        else:
            try:
                self._saved_geometry = (self.get_size(), self.get_position())
            except Exception:
                pass
            self.maximize()

    def toggle_fullscreen(self):
        if self._fullscreen:
            self.unfullscreen()
        else:
            self.fullscreen()

    # ------------------------------------------------------------------
    # Header controls
    # ------------------------------------------------------------------
    def _on_opacity_changed(self, scale):
        value = float(scale.get_value())
        self.appearance.set_opacity(value)
        scale.set_tooltip_text("Glass opacity %d%%" % round(value * 100))

    def _on_opacity_commit(self, scale):
        self.store.set("opacity", round(float(scale.get_value()), 2))

    def _on_theme_clicked(self, _button):
        theme = self.appearance.toggle_theme()
        self.store.set("theme", theme)
        self.theme_button.set_label("\u263d" if theme == "light" else "\u2600")
        self.queue_draw()
        for widget in (self.gallery, self.details):
            widget.queue_draw()

    def _on_search_changed(self, _entry):
        if self._search_source is not None:
            GLib.source_remove(self._search_source)
        self._search_source = GLib.timeout_add(180, self._apply_search)

    def _apply_search(self):
        self._search_source = None
        self.search_text = self.search_entry.get_text().strip().lower()
        self._refresh(reset_scroll=True)
        return False

    def _on_sort_changed(self, combo):
        key = combo.get_active_id() or "date"
        if key == self.sort:
            return
        self.sort = key
        self.store.set("sort", key)
        self._refresh(reset_scroll=True)

    def _on_view_toggled(self, button, mode):
        if not button.get_active():
            return
        self.view_mode = mode
        self.store.set("view", mode)
        self.gallery.set_view_mode("film" if mode == "film" else "grid")

    def _on_select_toggled(self, button):
        self._select_mode = bool(button.get_active())
        self.gallery.set_select_mode(self._select_mode)
        if not self._select_mode:
            self.gallery.clear_selection()

    def _set_select_mode(self, enabled: bool):
        self.select_toggle.set_active(bool(enabled))
        self._select_mode = bool(enabled)
        self.gallery.set_select_mode(self._select_mode)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------
    @staticmethod
    def _dispatch(func, *args):
        GLib.idle_add(func, *args)

    def _scan_roots(self):
        home = home_dir()
        roots = [home]
        for folder in default_roots(home) + self.store.custom_folders:
            if folder not in roots:
                roots.append(folder)
        return roots

    def _start_scan(self):
        self._scanning = True
        self.gallery.set_loading(True, "Scanning your folders\u2026")
        self._show_loading("Scanning your folders\u2026", "0 files")
        self.scanner.start(self._scan_roots())
        return False

    def _show_loading(self, title, subtitle):
        self._loading_visible = True
        self.loading_label.set_text(title)
        self.loading_sub.set_text(subtitle)
        self.loading_overlay.show_all()
        self.loading_spinner.start()

    def _hide_loading(self):
        if not self._loading_visible:
            return
        self._loading_visible = False
        self.loading_spinner.stop()
        self.loading_overlay.hide()

    def _on_scan_progress(self, count, folder):
        if not self._loading_visible:
            return
        self.loading_sub.set_text("%s files  \u00b7  %s" % (count, folder))
        self.loading_label.set_text("Scanning your folders\u2026")

    def _on_scan_batch(self, batch):
        for item in batch:
            existing = self._items.get(item.path)
            if existing is None:
                self._items[item.path] = item
            else:
                existing.size = item.size
                existing.mtime = item.mtime
        self._queue_refresh(220)
        return False

    def _on_scan_done(self):
        self._scanning = False
        self._albums_built = False
        self.gallery.set_loading(False)
        self._hide_loading()
        self._flush_refresh(force=True)
        self.thumbnailer.flush_meta()
        GLib.idle_add(self._trim_cache)
        return False

    def _trim_cache(self):
        try:
            self.thumbnailer.trim_disk_cache()
        except Exception:
            pass
        return False

    def _queue_refresh(self, delay=200):
        if self._refresh_source is not None:
            return
        self._refresh_source = GLib.timeout_add(delay, self._flush_refresh)

    def _flush_refresh(self, force=False):
        self._refresh_source = None
        if self._destroyed:
            return False
        try:
            self._refresh()
        except Exception:
            pass
        return False

    def _setup_monitors(self):
        self._monitors = []
        seen = set()
        for root in self._scan_roots():
            if root in seen or not os.path.isdir(root):
                continue
            seen.add(root)
            try:
                monitor = Gio.File.new_for_path(root).monitor_directory(
                    Gio.FileMonitorFlags.NONE, None)
            except Exception:
                continue
            monitor.connect("changed", self._on_fs_event, root)
            self._monitors.append(monitor)
        return False

    def _on_fs_event(self, _monitor, _file, _other, event_type, folder):
        if event_type not in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.CHANGED,
                              Gio.FileMonitorEvent.MOVED_IN, Gio.FileMonitorEvent.DELETED,
                              Gio.FileMonitorEvent.MOVED_OUT):
            return
        if folder in self._fs_sources:
            GLib.source_remove(self._fs_sources[folder])
        self._fs_sources[folder] = GLib.timeout_add(700, self._rescan_folder_debounced, folder)

    def _rescan_folder_debounced(self, folder):
        self._fs_sources.pop(folder, None)
        self._rescan_folder(folder)
        return False

    def _rescan_folder(self, folder):
        def worker():
            try:
                found = list(iter_folder(folder))
            except Exception:
                found = []
            GLib.idle_add(self._merge_folder, folder, found)

        threading.Thread(target=worker, name="lumen-watch", daemon=True).start()

    def _merge_folder(self, folder, found):
        if self._destroyed:
            return False
        fresh = {item.path: item for item in found}
        for path in [p for p, i in self._items.items() if i.folder == folder and p not in fresh]:
            del self._items[path]
        self._items.update(fresh)
        self._queue_refresh(120)
        self._load_trash()
        return False

    # ------------------------------------------------------------------
    # Data shaping
    # ------------------------------------------------------------------
    def _base_items(self):
        if self.tab == "trash":
            return list(self.trash_items)
        items = list(self._items.values())
        if self.tab == "images":
            return [i for i in items if i.kind == "image"]
        if self.tab == "videos":
            return [i for i in items if i.kind == "video"]
        if self.tab == "documents":
            roots = documents_roots()
            return [i for i in items
                    if i.kind == "document" or i.is_screenshot
                    or any(is_under(i.path, root) for root in roots)]
        if self.tab == "favorites":
            return [i for i in items if i.path in self.store.favorites]
        if self.tab == "folder" and self.filter_folder:
            return [i for i in items if is_under(i.path, self.filter_folder)]
        if self.tab == "album" and self.filter_album:
            paths = self._album_paths(self.filter_album)
            return [i for i in items if i.path in paths]
        return items

    def _album_paths(self, key):
        if key and key.startswith("album:"):
            return set(self.store.album_items(key.split(":", 1)[1]))
        if key and key.startswith("folder:"):
            folder = key.split(":", 1)[1]
            return {i.path for i in self._items.values() if i.folder == folder}
        if key and key.startswith("date:"):
            stamp = key.split(":", 1)[1]
            return {i.path for i in self._items.values()
                    if time.strftime("%Y-%m", time.localtime(i.mtime)) == stamp}
        if key and key.startswith("type:"):
            kind = key.split(":", 1)[1]
            return {i.path for i in self._items.values() if i.kind == kind}
        return set()

    def _sort_items(self, items):
        if self.sort == "name":
            items.sort(key=lambda i: i.name.lower())
        elif self.sort == "size":
            items.sort(key=lambda i: i.size, reverse=True)
        elif self.sort == "type":
            items.sort(key=lambda i: (i.kind, i.name.lower()))
        else:
            items.sort(key=lambda i: i.mtime, reverse=True)

    def _refresh(self, reset_scroll=False):
        if self._destroyed:
            return
        self._update_content_header()
        if self.tab == "albums":
            self.content_stack.set_visible_child_name("albums")
            self.gallery.set_loading(self._scanning)
            self.gallery.clear_selection()
            if not (self._scanning and self._albums_built):
                self._build_albums()
            self.page_title.set_text("Albums")
            self.page_sub.set_text("%d collection%s" % (
                len(self.store.album_names()), "" if len(self.store.album_names()) == 1 else "s"))
            self._update_counts()
            self._update_action_bar(0)
            return

        self.content_stack.set_visible_child_name("media")
        items = self._base_items()
        if self.search_text:
            items = [i for i in items if self.search_text in i.search_blob]
        self._sort_items(items)
        self.gallery.set_items(items, reset_scroll=reset_scroll)
        self.gallery.set_loading(self._scanning)
        self._update_titles(len(items))
        self._update_counts()
        self._update_action_bar(self.gallery.selection_count())
        self._sync_details()

    def _update_content_header(self):
        self.back_button.set_visible(self.tab == "album")
        self.empty_bin_button.set_visible(self.tab == "trash")
        self.sort_combo.set_visible(self.tab != "albums")
        if self.tab == "trash":
            self.sort_combo.set_sensitive(True)

    def _update_titles(self, count):
        if self.tab == "album" and self.filter_album:
            name = self.filter_album.split(":", 1)[1]
            title = name if not self.filter_album.startswith("album:") else name
            subtitle = "Collection"
        elif self.tab == "folder" and self.filter_folder:
            title = os.path.basename(self.filter_folder.rstrip(os.sep)) or self.filter_folder
            subtitle = "Folder"
        else:
            title, subtitle = PAGE_TITLES.get(self.tab, PAGE_TITLES["all"])
        self.page_title.set_text(title)
        bits = ["%d item%s" % (count, "" if count == 1 else "s")]
        if self.search_text:
            bits.append("matching \u201c%s\u201d" % self.search_text)
        elif self._scanning:
            bits.append("scanning\u2026")
        elif subtitle:
            bits.append(subtitle)
        self.page_sub.set_text("  \u00b7  ".join(bits))

    def _update_counts(self):
        items = list(self._items.values())
        documents_roots_list = documents_roots()
        counts = {
            "all": len(items),
            "images": 0,
            "videos": 0,
            "documents": 0,
            "favorites": 0,
            "albums": len(self.store.album_names()),
            "trash": len(self.trash_items),
        }
        favourites = self.store.favorites
        for item in items:
            if item.kind == "image":
                counts["images"] += 1
            elif item.kind == "video":
                counts["videos"] += 1
            if (item.kind == "document" or item.is_screenshot
                    or any(is_under(item.path, root) for root in documents_roots_list)):
                counts["documents"] += 1
            if item.path in favourites:
                counts["favorites"] += 1
        self.sidebar.set_counts(counts)
        self.bin_button.set_tooltip_text(
            "Recycle Bin \u00b7 %d item%s" % (counts["trash"], "" if counts["trash"] == 1 else "s"))
        if self.tab == "trash" and counts["trash"] == 0:
            self.gallery.set_empty_message(
                "Bin is empty", "Files you remove will wait here so you can restore them.")

    def _sync_details(self):
        selection = self.gallery.get_selection()
        if len(selection) == 1:
            item = selection[0]
            self._focus_item = item
            self.details.set_item(item, self.store.is_favorite(item.path))
            if self.details_open:
                self.details_revealer.set_reveal_child(True)
        else:
            self.details.set_item(None)
            self.details_revealer.set_reveal_child(self.details_open and len(selection) == 1)
        if len(selection) > 1:
            self.details.set_item(selection[0], self.store.is_favorite(selection[0].path))
            self.details.sub_label.set_text("%d items selected" % len(selection))

    # ------------------------------------------------------------------
    # Navigation / selection
    # ------------------------------------------------------------------
    def _goto_tab(self, key):
        self.tab = key
        self.filter_folder = None
        self.filter_album = None
        if key == "albums":
            self._albums_built = False
        if key in PAGE_TITLES:
            self.store.set("last_tab", key)
        self.sidebar.select(key if key in ("all", "images", "videos", "documents",
                                           "albums", "favorites", "trash") else "all", silent=True)
        self._refresh(reset_scroll=True)

    def _on_navigate(self, _sidebar, key):
        if key.startswith("folder:"):
            self.tab = "folder"
            self.filter_folder = key.split(":", 1)[1]
            self.filter_album = None
            self._refresh(reset_scroll=True)
            return
        if key.startswith("album:"):
            self.tab = "album"
            self.filter_album = key
            self.filter_folder = None
            self._refresh(reset_scroll=True)
            return
        self._goto_tab(key)

    def _on_item_activated(self, _gallery, item):
        self._open_viewer(item)

    def _open_viewer(self, item=None):
        items = self.gallery.items
        if not items:
            return
        if item is None:
            selection = self.gallery.get_selection()
            item = selection[0] if selection else items[0]
        index = 0
        for position, candidate in enumerate(items):
            if candidate.path == item.path:
                index = position
                break
        if item.kind == "document":
            self._open_with_default(item.path)
            return
        self.viewer.show_items(items, index)

    def _on_item_focused(self, _gallery, item):
        if self.gallery.selection_count() == 1:
            self._focus_item = item
            self.details.set_item(item, self.store.is_favorite(item.path))
            if self.details_open:
                self.details_revealer.set_reveal_child(True)
            else:
                self.details.sub_label.set_text("Press i for details")

    def _on_selection_changed(self, _gallery):
        count = self.gallery.selection_count()
        self._update_action_bar(count)
        if count == 1:
            item = self.gallery.get_selection()[0]
            self._focus_item = item
            self.details.set_item(item, self.store.is_favorite(item.path))
            self.details_open = True
            self.details_revealer.set_reveal_child(True)
        elif count == 0:
            self._focus_item = None
            self.details.set_item(None)
            self.details_revealer.set_reveal_child(False)
        else:
            self.details.set_item(self.gallery.get_selection()[0],
                                  self.store.is_favorite(self.gallery.get_selection()[0].path))
            self.details.sub_label.set_text("%d items selected" % count)
            self.details_revealer.set_reveal_child(True)
            self.details_open = True

    def _update_action_bar(self, count):
        self.action_count.set_text("%d selected" % count)
        self.action_revealer.set_reveal_child(count > 0)
        trash_mode = self.tab == "trash"
        for key in ("favorite", "album", "wallpaper", "bin"):
            self.ab_buttons[key].set_visible(not trash_mode)
        for key in ("restore", "delete"):
            self.ab_buttons[key].set_visible(trash_mode)
        self.ab_buttons["clear"].set_visible(True)
        self.ab_buttons["copy"].set_visible(True)
        return False

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _selection(self):
        chosen = self.gallery.get_selection()
        if chosen:
            return chosen
        if self._focus_item is not None:
            return [self._focus_item]
        return []

    def _favorite_selection(self, force_state=None):
        items = self._selection()
        if not items:
            return
        paths = [i.path for i in items]
        if force_state is None:
            state = not self.store.is_favorite(paths[0])
        else:
            state = force_state and not all(self.store.is_favorite(p) for p in paths)
        self.store.set_favorite(paths, state)
        self.gallery.refresh_favorites()
        current = items[0]
        self.details.refresh_favorite(self.store.is_favorite(current.path))
        self.details.set_item(current, self.store.is_favorite(current.path))
        self.toast("%d item%s %s" % (len(paths), "" if len(paths) == 1 else "s",
                                     "starred" if state else "removed from favorites"))
        if self.tab == "favorites" and not state:
            self._refresh()

    def _copy_selection_paths(self):
        items = self._selection()
        if not items:
            return
        text = "\n".join(i.path for i in items)
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        clipboard.store()
        self.toast("Path copied \u2014 paste it to share")

    def _wallpaper_selection(self):
        items = self._selection()
        if not items:
            return
        item = items[0]
        if item.kind != "image":
            self.toast("Pick an image to use as wallpaper")
            return
        self._set_wallpaper(item.path)

    def _trash_selection(self):
        items = self._selection()
        if not items or self.tab == "trash":
            return
        paths = [i.path for i in items]
        moved, failed = self.store.trash_paths(paths)
        if moved:
            self.gallery.remove_items(paths)
            for path in paths:
                self._items.pop(path, None)
            self._load_trash()
            self._refresh()
            self.toast("%d item%s moved to the Recycle Bin" % (moved, "" if moved == 1 else "s"))
        if failed:
            self.toast("Could not move %d item%s" % (len(failed), "" if len(failed) == 1 else "s"))

    def _restore_selection(self):
        items = self._selection()
        if not items:
            return
        restored, errors = 0, 0
        folders = set()
        for item in items:
            ok, result = self.store.restore(item)
            if ok:
                restored += 1
                folders.add(os.path.dirname(result))
            else:
                errors += 1
        self._load_trash()
        for folder in folders:
            self._rescan_folder(folder)
        self.gallery.clear_selection()
        self._refresh()
        if restored:
            self.toast("Restored %d item%s" % (restored, "" if restored == 1 else "s"))
        if errors:
            self.toast("Could not restore %d item%s" % (errors, "" if errors == 1 else "s"))

    def _delete_selection_forever(self):
        items = self._selection()
        if not items:
            return
        if not self._confirm("Delete permanently?",
                             "This removes %d item%s for good." % (
                                 len(items), "" if len(items) == 1 else "s"),
                             "Delete"):
            return
        removed = 0
        for item in items:
            if self.store.delete_forever(item):
                removed += 1
        self._load_trash()
        self._refresh()
        self.toast("Deleted %d item%s permanently" % (removed, "" if removed == 1 else "s"))

    def _empty_bin(self):
        if not self.store.list_trash():
            self.toast("The bin is already empty")
            return
        if not self._confirm("Empty the Recycle Bin?",
                             "Everything inside the bin will be deleted permanently.",
                             "Empty bin"):
            return
        removed = self.store.empty_trash()
        self._load_trash()
        self._refresh()
        self.toast("Bin emptied \u00b7 %d item%s removed" % (removed, "" if removed == 1 else "s"))

    def _add_selection_to_album(self):
        items = self._selection()
        if not items:
            return
        paths = [i.path for i in items]
        name = self._album_dialog(len(paths))
        if not name:
            return
        added = self.store.add_to_album(name, paths)
        self.toast("Added %d item%s to \u201c%s\u201d" % (added, "" if added == 1 else "s", name))
        if self.tab == "albums":
            self._refresh()

    def _album_dialog(self, count):
        dialog = Gtk.Dialog(title="Add to album", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Add", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(14)
        label = Gtk.Label(label="Add %d item%s to:" % (count, "" if count == 1 else "s"))
        label.set_xalign(0.0)
        box.pack_start(label, False, False, 0)

        names = self.store.album_names()
        combo = Gtk.ComboBoxText()
        for name in names:
            combo.append_text(name)
        if names:
            combo.set_active(0)
        box.pack_start(combo, False, False, 0)

        entry = Gtk.Entry()
        entry.set_placeholder_text("New album name")
        entry.set_activates_default(True)
        box.pack_start(entry, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        chosen = entry.get_text().strip() or (combo.get_active_text() or "")
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        return chosen or None

    def _rename_item(self, item):
        current = item.name
        name = self._entry_dialog("Rename", "New name", current)
        if not name or name == current:
            return
        target = os.path.join(item.folder, name)
        if os.path.exists(target):
            self.toast("A file with that name already exists")
            return
        try:
            os.rename(item.path, target)
        except OSError as error:
            self.toast("Rename failed: %s" % error)
            return
        self._items.pop(item.path, None)
        replacement = MediaItem(path=target, name=name, folder=item.folder,
                                ext=os.path.splitext(name)[1].lower(),
                                kind=item.kind,
                                size=item.size, mtime=time.time())
        self._items[target] = replacement
        if self.store.is_favorite(item.path):
            self.store.set_favorite([item.path], False)
            self.store.set_favorite([target], True)
        self.gallery.replace_item(item.path, replacement)
        self._refresh()
        self.toast("Renamed to \u201c%s\u201d" % name)

    def _copy_or_move(self, items, move=False):
        if not items:
            return
        folder = self._choose_folder("Move to folder" if move else "Copy to folder")
        if not folder:
            return
        done, failed = 0, 0
        targets = []
        for item in items:
            try:
                target = unique_path(folder, item.name)
                if move:
                    shutil.move(item.path, target)
                    self._items.pop(item.path, None)
                else:
                    shutil.copy2(item.path, target)
                targets.append(target)
                done += 1
            except OSError:
                failed += 1
        self._rescan_folder(folder)
        if move:
            self.gallery.clear_selection()
            self._refresh()
        self.toast("%s %d item%s%s" % (
            "Moved" if move else "Copied", done, "" if done == 1 else "s",
            "" if not failed else " \u00b7 %d failed" % failed))

    def _set_wallpaper(self, path):
        candidates = (
            ("feh", ["feh", "--bg-scale", path]),
            ("pcmanfm-qt", ["pcmanfm-qt", "--set-wallpaper=" + path]),
        )
        for executable, command in candidates:
            if shutil.which(executable):
                try:
                    subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    self.toast("Wallpaper updated")
                    return
                except OSError:
                    continue
        if shutil.which("gsettings"):
            try:
                uri = "file://" + urllib.parse.quote(path)
                subprocess.Popen(
                    ["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.toast("Wallpaper updated (gsettings)")
                return
            except OSError:
                pass
        self.toast("Install feh or pcmanfm-qt to set the wallpaper")

    def _open_with_default(self, path):
        uri = GLib.filename_to_uri(path, None)
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
            return
        except Exception:
            pass
        try:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            self.toast("No application registered for this file")

    def _reveal(self, path):
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(folder, None), None)
        except Exception:
            try:
                subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError:
                self.toast("Could not open the folder")

    def _open_with_menu(self, item):
        menu = Gtk.Menu()
        mime = "application/octet-stream"
        apps = []
        try:
            from core.scanner import content_type
            mime = content_type(item.path)
            apps = Gio.AppInfo.get_all_for_type(mime) or []
        except Exception:
            apps = []
        default = None
        try:
            default = Gio.AppInfo.get_default_for_type(mime, False) if apps else None
        except Exception:
            default = None
        if default is not None:
            entry = Gtk.MenuItem(label="%s (default)" % default.get_display_name())
            entry.connect("activate", lambda *_: self._open_with_default(item.path))
            menu.append(entry)
            menu.append(Gtk.SeparatorMenuItem())
        if not apps:
            placeholder = Gtk.MenuItem(label="Default application")
            placeholder.connect("activate", lambda *_: self._open_with_default(item.path))
            menu.append(placeholder)
        for app in apps[:12]:
            if default is not None and app.get_id() == default.get_id():
                continue
            entry = Gtk.MenuItem(label=app.get_display_name())
            entry.connect("activate", lambda _w, a=app: self._launch_app(a, item.path))
            menu.append(entry)
        menu.show_all()
        return menu

    def _launch_app(self, app, path):
        try:
            app.launch_uris([GLib.filename_to_uri(path, None)], None)
        except Exception as error:
            self.toast("Could not launch: %s" % error)

    def _share(self, items):
        paths = [i.path for i in items]
        if not paths:
            return
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text("\n".join(paths), -1)
        clipboard.store()
        self.toast("Path copied \u2014 share it anywhere")

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------
    def _on_context_menu(self, _gallery, item, event):
        menu = Gtk.Menu()
        trash_mode = self.tab == "trash"
        selection = self._selection()
        if item not in selection:
            selection = [item]

        def add(label, callback, sensitive=True):
            entry = Gtk.MenuItem(label=label)
            entry.set_sensitive(sensitive)
            entry.connect("activate", lambda *_: callback())
            menu.append(entry)

        if trash_mode:
            add("Restore", self._restore_selection)
            add("Delete permanently", self._delete_selection_forever)
            menu.append(Gtk.SeparatorMenuItem())
            add("Copy path", self._copy_selection_paths)
            add("Properties", lambda: self._show_details_for(item))
        else:
            add("Open", lambda: self._open_viewer(item))
            open_item = Gtk.MenuItem(label="Open with\u2026")
            open_item.set_submenu(self._open_with_menu(item))
            menu.append(open_item)
            add("Show in folder", lambda: self._reveal(item.path))
            menu.append(Gtk.SeparatorMenuItem())
            add("Add to Favorites" if not self.store.is_favorite(item.path)
                else "Remove from Favorites", lambda: self._favorite_selection())
            add("Add to album\u2026", self._add_selection_to_album)
            add("Copy path", self._copy_selection_paths)
            add("Share (copy path)", lambda: self._share(selection))
            menu.append(Gtk.SeparatorMenuItem())
            add("Rename\u2026", lambda: self._rename_item(item), len(selection) == 1)
            add("Copy to\u2026", lambda: self._copy_or_move(selection, move=False))
            add("Move to\u2026", lambda: self._copy_or_move(selection, move=True))
            add("Set as wallpaper", lambda: self._set_wallpaper(item.path), item.kind == "image")
            menu.append(Gtk.SeparatorMenuItem())
            add("Move to Bin", self._trash_selection)
            add("Properties", lambda: self._show_details_for(item))
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _show_details_for(self, item):
        self._focus_item = item
        self.details.set_item(item, self.store.is_favorite(item.path))
        self.details_open = True
        self.details_revealer.set_reveal_child(True)

    def _on_details_action(self, _panel, action, item):
        if action == "details-close":
            self._toggle_details(False)
            return
        if item is None:
            return
        if action == "open":
            self._open_viewer(item)
        elif action == "reveal":
            self._reveal(item.path)
        elif action == "trash":
            if self.tab == "trash":
                self.gallery.select_only(item)
                self._delete_selection_forever()
            else:
                self.gallery.select_only(item)
                self._trash_selection()

    def _toggle_details(self, force=None):
        state = (not self.details_open) if force is None else bool(force)
        self.details_open = state
        self.store.set("details_open", state)
        self.details_revealer.set_reveal_child(state)

    def _toggle_favorite(self, item):
        if item is None:
            return
        state = self.store.toggle_favorite(item.path)
        self.gallery.refresh_favorites()
        self.details.refresh_favorite(state)
        self.details.set_item(item, state)
        self.toast("Favorited" if state else "Removed from favorites")
        if self.tab == "favorites" and not state:
            self._refresh()

    def _on_viewer_closed(self, _viewer):
        self.details_revealer.set_reveal_child(
            self.details_open and self.gallery.selection_count() == 1)
        return False

    def _on_viewer_action(self, _viewer, action, item):
        if item is None:
            return
        if action == "favorite":
            self.store.toggle_favorite(item.path)
            self.gallery.refresh_favorites()
            self.details.set_item(item, self.store.is_favorite(item.path))
            self.viewer._update_bar()
            self.toast("Favorited" if self.store.is_favorite(item.path) else "Removed from favorites")
        elif action == "open":
            self._open_with_default(item.path)
        elif action == "reveal":
            self._reveal(item.path)
        elif action == "trash":
            self.gallery.select_only(item)
            self._trash_selection()
            self.viewer.close()

    # ------------------------------------------------------------------
    # Albums page
    # ------------------------------------------------------------------
    def _album_groups(self):
        items = list(self._items.values())
        by_folder = {}
        by_month = {}
        for item in items:
            by_folder.setdefault(item.folder, []).append(item)
            month = time.strftime("%Y-%m", time.localtime(item.mtime))
            by_month.setdefault(month, []).append(item)
        return by_folder, by_month

    def _build_albums(self):
        for child in list(self.albums_flow.get_children()):
            self.albums_flow.remove(child)

        by_folder, by_month = self._album_groups()
        cards = 0

        new_card = self._new_album_card()
        self.albums_flow.add(new_card)
        cards += 1

        for name in self.store.album_names():
            paths = set(self.store.album_items(name))
            members = [item for item in self._items.values() if item.path in paths]
            card = self._album_card("album:" + name, name, members,
                                    subtitle="%d item%s" % (len(members),
                                                             "" if len(members) == 1 else "s"))
            self.albums_flow.add(card)
            cards += 1

        folder_groups = sorted(by_folder.items(), key=lambda kv: len(kv[1]), reverse=True)
        for folder, members in folder_groups[:12]:
            if len(members) < 2:
                continue
            title = os.path.basename(folder.rstrip(os.sep)) or folder
            self.albums_flow.add(self._album_card("folder:" + folder, title, members))
            cards += 1

        month_groups = sorted(by_month.items(), reverse=True)[:8]
        for month, members in month_groups:
            if len(members) < 2:
                continue
            try:
                label = time.strftime("%B %Y", time.strptime(month, "%Y-%m"))
            except ValueError:
                label = month
            self.albums_flow.add(self._album_card("date:" + month, label, members))
            cards += 1

        for kind, label in (("image", "Images"), ("video", "Videos"), ("document", "Documents")):
            members = [i for i in self._items.values() if i.kind == kind]
            if len(members) < 2:
                continue
            self.albums_flow.add(self._album_card("type:" + kind, label, members))
            cards += 1

        self._albums_built = True
        self.albums_flow.show_all()
        if cards <= 1:
            self.albums_empty.show()
        else:
            self.albums_empty.hide()

    def _new_album_card(self):
        card = AlbumCard("__new__", "\uff0b New album", 0, [],
                         on_open=lambda _key: self._new_album_dialog(),
                         subtitle="Group your own memories")
        card.get_style_context().add_class("album-new")
        return card

    def _album_card(self, key, title, members, subtitle=None):
        covers = []
        for item in members[:4]:
            covers.append(self.thumbnailer.cached(item, 180))
        card = AlbumCard(key, title, len(members), covers,
                         on_open=self._open_album, subtitle=subtitle)
        for slot, item in enumerate(members[:4]):
            if covers[slot] is not None:
                continue
            self.thumbnailer.request(
                item, lambda pixbuf, c=card, s=slot: self._cover_ready(c, s, pixbuf), 180)
        return card

    def _cover_ready(self, card, slot, pixbuf):
        if pixbuf is not None and not self._destroyed:
            card.set_cover(slot, pixbuf)
        return False

    def _open_album(self, key):
        if key == "__new__":
            self._new_album_dialog()
            return
        self.tab = "album"
        self.filter_album = key
        self.filter_folder = None
        self.sidebar.select("albums", silent=True)
        self._refresh(reset_scroll=True)

    def _new_album_dialog(self):
        name = self._entry_dialog("New album", "Album name", "")
        if not name:
            return
        if self.store.create_album(name):
            self.toast("Album \u201c%s\u201d created" % name)
            self._refresh()
        else:
            self.toast("That album already exists")

    # ------------------------------------------------------------------
    # Small dialogs and helpers
    # ------------------------------------------------------------------
    def _entry_dialog(self, title, label_text, initial):
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(14)
        label = Gtk.Label(label=label_text)
        label.set_xalign(0.0)
        box.pack_start(label, False, False, 0)
        entry = Gtk.Entry(text=initial)
        entry.set_activates_default(True)
        entry.select_region(0, -1)
        box.pack_start(entry, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        text = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        return text

    def _choose_folder(self, title):
        dialog = Gtk.FileChooserDialog(title=title, transient_for=self,
                                       action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Choose", Gtk.ResponseType.OK)
        folder = os.path.join(home_dir(), "Pictures")
        if os.path.isdir(folder):
            dialog.set_current_folder(folder)
        response = dialog.run()
        chosen = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not chosen:
            return None
        return chosen

    def _confirm(self, title, message, accept_label="OK"):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.WARNING,
                                   buttons=Gtk.ButtonsType.NONE, text=title)
        dialog.format_secondary_text(message)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button(accept_label, Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def _add_folder_dialog(self):
        folder = self._choose_folder("Add folder to LUMEN")
        if not folder:
            return
        if self.store.add_folder(folder):
            self.sidebar.set_folders(self.store.custom_folders)
            self.toast("Added %s" % os.path.basename(folder.rstrip(os.sep)))
            self._rescan_folder(folder)
            self._start_scan()
        else:
            self.toast("Folder is already in the library")

    def _on_remove_folder(self, _sidebar, folder):
        self.store.remove_folder(folder)
        self.sidebar.set_folders(self.store.custom_folders)
        self._start_scan()
        self.toast("Removed from library")

    def _load_trash(self):
        try:
            self.trash_items = self.store.list_trash()
        except Exception:
            self.trash_items = []

    def _open_start_path(self, path):
        path = os.path.expanduser(path)
        if os.path.isdir(path):
            self.store.add_folder(path)
            self.sidebar.set_folders(self.store.custom_folders)
            self._start_scan()
            return False
        if os.path.isfile(path):
            kind = kind_of(os.path.splitext(path)[1].lower())
            if kind is None:
                return False
            try:
                st = os.stat(path)
            except OSError:
                return False
            item = MediaItem(path=path, name=os.path.basename(path),
                             folder=os.path.dirname(path),
                             ext=os.path.splitext(path)[1].lower(), kind=kind,
                             size=st.st_size, mtime=st.st_mtime)
            self._items[path] = item
            self._refresh()
            self._open_viewer(item)
        return False

    def toast(self, text, timeout=2600):
        self.toast_label.set_text(text)
        self.toast_revealer.set_reveal_child(True)
        if self._toast_source is not None:
            GLib.source_remove(self._toast_source)
        self._toast_source = GLib.timeout_add(timeout, self._hide_toast)

    def _hide_toast(self):
        self._toast_source = None
        self.toast_revealer.set_reveal_child(False)
        return False

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------
    def _on_key_press(self, _widget, event):
        if self.viewer.is_open():
            if self.viewer.handle_key(event):
                return True
        focus = self.get_focus()
        typing = isinstance(focus, (Gtk.Entry, Gtk.TextView))
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        key = event.keyval
        if key == Gdk.KEY_Escape:
            if self.gallery.selection_count():
                self.gallery.clear_selection()
                return True
            if self.details_open:
                self.details_open = False
                self.store.set("details_open", False)
                self.details_revealer.set_reveal_child(False)
                return True
            return False
        if ctrl and key in (Gdk.KEY_a, Gdk.KEY_A) and not typing:
            if self.tab != "albums":
                self._set_select_mode(True)
                self.gallery.select_all()
            return True
        if ctrl and key in (Gdk.KEY_f, Gdk.KEY_F):
            self.search_entry.grab_focus()
            return True
        if typing:
            return False
        if key in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            if self.gallery.selection_count():
                if self.tab == "trash":
                    self._delete_selection_forever()
                else:
                    self._trash_selection()
                return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            if self.gallery.selection_count() and self.tab != "albums":
                self._open_viewer()
                return True
        if key == Gdk.KEY_F5:
            self._start_scan()
            return True
        if key == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True
        if key == Gdk.KEY_i:
            self._toggle_details()
            return True
        return False

    # ------------------------------------------------------------------
    # Drag & drop
    # ------------------------------------------------------------------
    def _on_drag_data(self, _widget, _context, _x, _y, data, _info, _time):
        try:
            uris = data.get_uris()
        except Exception:
            uris = []
        for uri in uris or []:
            path = GLib.filename_from_uri(uri)[0]
            if os.path.isdir(path):
                self.store.add_folder(path)
                self.sidebar.set_folders(self.store.custom_folders)
                self._start_scan()
                return
            if os.path.isfile(path):
                self._open_start_path(path)
                return

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------
    def _on_destroy(self, _widget):
        self._destroyed = True
        try:
            self.scanner.stop()
        except Exception:
            pass
        try:
            self.gallery.shutdown()
        except Exception:
            pass
        try:
            self.thumbnailer.flush_meta()
            self.thumbnailer.shutdown()
        except Exception:
            pass
        for monitor in self._monitors:
            try:
                monitor.cancel()
            except Exception:
                pass
        self._monitors = []
