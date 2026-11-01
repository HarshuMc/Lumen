"""LUMEN sidebar - glass navigation rail with live counts."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GObject, Gtk  # noqa: E402


SIDE_ITEMS = [
    ("all", "\U0001F4C1", "All Media"),
    ("images", "\U0001F5BC\uFE0F", "Images"),
    ("videos", "\U0001F3AC", "Videos"),
    ("documents", "\U0001F4C4", "Documents"),
    ("albums", "\U0001F5C2\uFE0F", "Albums"),
    ("favorites", "\u2B50", "Favorites"),
    ("trash", "\U0001F5D1\uFE0F", "Recycle Bin"),
]


class SideRow(Gtk.ListBoxRow):
    """A single navigation row: icon, label and a count pill."""

    def __init__(self, key: str, icon: str, label: str):
        Gtk.ListBoxRow.__init__(self)
        self.key = key
        self.set_name("side-" + key)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.get_style_context().add_class("side-row")
        icon_label = Gtk.Label(label=icon)
        icon_label.get_style_context().add_class("side-icon")
        icon_label.set_xalign(0.0)
        box.pack_start(icon_label, False, False, 0)

        text = Gtk.Label(label=label)
        text.get_style_context().add_class("side-label")
        text.set_xalign(0.0)
        text.set_ellipsize(3)
        box.pack_start(text, True, True, 0)

        self.count_label = Gtk.Label(label="")
        self.count_label.get_style_context().add_class("side-count")
        self.count_label.set_no_show_all(True)
        box.pack_end(self.count_label, False, False, 0)

        hint = Gtk.Label()
        hint.get_style_context().add_class("folder-row")
        self._folder_hint = hint

        self.add(box)

    def set_count(self, count: int):
        if count > 0:
            self.count_label.set_text(str(count) if count < 10000 else "9k+")
            self.count_label.show()
        else:
            self.count_label.hide()


class Sidebar(Gtk.Box):
    """Left rail; emits ``navigate`` with the selected tab key."""

    __gsignals__ = {
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "add-folder": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "remove-folder": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.set_size_request(220, -1)
        self.get_style_context().add_class("sidebar")
        self._rows = {}
        self._active = None
        self._custom_rows = []
        self._syncing = False

        library = Gtk.Label(label="LIBRARY")
        library.get_style_context().add_class("section-label")
        library.set_xalign(0.0)
        self.pack_start(library, False, False, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.get_style_context().add_class("side-list")
        self.listbox.connect("row-selected", self._on_row_selected)
        for key, icon, label in SIDE_ITEMS:
            row = SideRow(key, icon, label)
            self._rows[key] = row
            self.listbox.add(row)
        self.pack_start(self.listbox, False, False, 0)

        self.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 10)

        folders_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        folder_label = Gtk.Label(label="FOLDERS")
        folder_label.get_style_context().add_class("section-label")
        folder_label.set_xalign(0.0)
        folders_head.pack_start(folder_label, True, True, 0)
        self.add_folder_btn = Gtk.Button(label="+")
        self.add_folder_btn.get_style_context().add_class("icon-btn")
        self.add_folder_btn.set_tooltip_text("Add a folder to the library")
        self.add_folder_btn.connect("clicked", lambda *_: self.emit("add-folder"))
        folders_head.pack_end(self.add_folder_btn, False, False, 0)
        self.pack_start(folders_head, False, False, 0)

        self.folders_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        folders_scroll = Gtk.ScrolledWindow()
        folders_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        folders_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        folders_scroll.get_style_context().add_class("gallery-scroll")
        folders_scroll.add(self.folders_box)
        folders_scroll.set_size_request(-1, 96)
        self.pack_start(folders_scroll, True, True, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        footer.get_style_context().add_class("sidebar-footer")
        hint = Gtk.Label(label="LUMEN 1.0 \u00b7 Your Memories, Clearly.")
        hint.get_style_context().add_class("sidebar-hint")
        hint.set_xalign(0.0)
        hint.set_line_wrap(True)
        footer.pack_start(hint, False, False, 0)
        self.pack_end(footer, False, False, 0)

        self.show_all()
        self.select("all", silent=True)

    # -- public ----------------------------------------------------------
    def select(self, key: str, silent: bool = False):
        row = self._rows.get(key)
        if row is None or self._active == key:
            return
        self._active = key
        self._syncing = True
        self.listbox.select_row(row)
        self._syncing = False
        if not silent:
            self.emit("navigate", key)

    @property
    def active(self) -> str:
        return self._active

    def set_counts(self, counts: dict):
        for key, row in self._rows.items():
            row.set_count(int(counts.get(key, 0) or 0))

    def set_folders(self, folders):
        for child in list(self.folders_box.get_children()):
            self.folders_box.remove(child)
        self._custom_rows = []
        if not folders:
            empty = Gtk.Label(label="No custom folders")
            empty.get_style_context().add_class("sidebar-hint")
            empty.set_xalign(0.0)
            self.folders_box.pack_start(empty, False, False, 0)
        for folder in folders:
            self.folders_box.pack_start(self._make_folder_row(folder), False, False, 0)
        self.folders_box.show_all()

    # -- internals -------------------------------------------------------
    def _make_folder_row(self, folder: str):
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.get_style_context().add_class("folder-row")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Label(label="\U0001F4C2")
        icon.set_xalign(0.0)
        box.pack_start(icon, False, False, 0)
        label = Gtk.Label(label=os.path.basename(folder.rstrip(os.sep)) or folder)
        label.set_xalign(0.0)
        label.set_ellipsize(3)
        label.set_tooltip_text(folder)
        box.pack_start(label, True, True, 0)
        button.add(box)
        button.connect("clicked", lambda _b: self.emit("navigate", "folder:" + folder))
        button.connect("button-press-event", self._on_folder_button, folder)
        self._custom_rows.append(button)
        return button

    def _on_folder_button(self, _widget, event, folder):
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        item = Gtk.MenuItem(label="Remove from library")
        item.connect("activate", lambda *_: self.emit("remove-folder", folder))
        menu.append(item)
        open_item = Gtk.MenuItem(label="Open in file manager")
        open_item.connect("activate", lambda *_: self._open_folder(folder))
        menu.append(open_item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    @staticmethod
    def _open_folder(folder):
        try:
            import gi as _gi
            _gi.require_version("Gio", "2.0")
            from gi.repository import Gio
            Gio.AppInfo.launch_default_for_uri("file://" + folder, None)
        except Exception:
            pass

    def _on_row_selected(self, _listbox, row):
        if self._syncing or row is None:
            return
        key = getattr(row, "key", None)
        if not key or key == self._active:
            return
        self._active = key
        self.emit("navigate", key)
