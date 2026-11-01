"""LUMEN persistence: settings, favourites, albums and the recycle bin.

Files live under the usual XDG locations::

    ~/.config/lumen/settings.json
    ~/.config/lumen/favorites.json
    ~/.config/lumen/albums.json
    ~/.cache/lumen/thumbs/...

Trash operations use ``Gio.File.trash()`` when PyGObject is available and fall
back to a spec compliant implementation of the freedesktop trash
(``~/.local/share/Trash``) otherwise, so the module stays testable head-less.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass

try:  # pragma: no cover - exercised on the desktop
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    _HAVE_GIO = True
except Exception:  # pragma: no cover
    Gio = None
    GLib = None
    _HAVE_GIO = False

from .scanner import IMAGE_EXTS, VIDEO_EXTS, DOC_EXTS, kind_of, human_size

APP_DIR_NAME = "lumen"
DEFAULT_SETTINGS = {
    "theme": "dark",
    "opacity": 0.82,
    "sort": "date",
    "view": "grid",
    "custom_folders": [],
    "details_open": False,
    "sidebar_open": True,
    "last_tab": "all",
}


def _xdg(env_name: str, fallback: str) -> str:
    value = os.environ.get(env_name)
    if value:
        return value
    return os.path.join(os.path.expanduser("~"), fallback)


def config_dir() -> str:
    return os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), APP_DIR_NAME)


def cache_dir() -> str:
    return os.path.join(_xdg("XDG_CACHE_HOME", ".cache"), APP_DIR_NAME)


def thumbs_dir() -> str:
    return os.path.join(cache_dir(), "thumbs")


def data_dir() -> str:
    return os.path.join(_xdg("XDG_DATA_HOME", os.path.join(".local", "share")), APP_DIR_NAME)


def trash_root() -> str:
    return _xdg("XDG_DATA_HOME", os.path.join(".local", "share"))


def trash_files_dir() -> str:
    return os.path.join(trash_root(), "Trash", "files")


def trash_info_dir() -> str:
    return os.path.join(trash_root(), "Trash", "info")


def ensure_dirs():
    for path in (config_dir(), cache_dir(), thumbs_dir(), data_dir()):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass


def _atomic_write(path: str, payload: str):
    """Write JSON safely so a crash never leaves a truncated config."""
    folder = os.path.dirname(path) or "."
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@dataclass
class TrashItem:
    """A file that currently sits in ``~/.local/share/Trash``."""

    path: str
    name: str
    folder: str
    ext: str
    kind: str
    size: int
    mtime: float
    original: str = ""
    deleted: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0

    @property
    def parent_name(self) -> str:
        base = os.path.basename(os.path.dirname(self.original)) if self.original else ""
        return base or "Recycle Bin"

    @property
    def date_text(self) -> str:
        return self.deleted or time.strftime("%d %b %Y  %H:%M", time.localtime(self.mtime))

    @property
    def date_short(self) -> str:
        return (self.deleted or "")[:10]

    @property
    def size_text(self) -> str:
        return human_size(self.size)

    @property
    def kind_label(self) -> str:
        return {"image": "Image", "video": "Video", "document": "Document"}.get(self.kind, "File")

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def is_video(self) -> bool:
        return self.kind == "video"

    @property
    def is_document(self) -> bool:
        return self.kind == "document"

    @property
    def is_screenshot(self) -> bool:
        return False

    @property
    def dimensions(self) -> str:
        if self.width and self.height:
            return "%d x %d" % (self.width, self.height)
        return "-"

    @property
    def search_blob(self) -> str:
        return "%s\n%s\n%s" % (self.name.lower(), self.original.lower(), self.kind)


class Storage:
    """JSON backed application state shared by every UI component."""

    def __init__(self, base_config: str = None):
        self._config = base_config or config_dir()
        self._lock = threading.RLock()
        ensure_dirs()
        self.settings = self._load("settings.json", DEFAULT_SETTINGS, dict)
        for key, value in DEFAULT_SETTINGS.items():
            self.settings.setdefault(key, value)
        raw_favourites = self._load("favorites.json", [], list)
        self.favorites = {p for p in raw_favourites if isinstance(p, str)}
        raw_albums = self._load("albums.json", {}, dict)
        self.albums = {}
        for name, paths in raw_albums.items():
            if isinstance(name, str) and isinstance(paths, list):
                self.albums[name] = [p for p in paths if isinstance(p, str)]

    # -- low level -------------------------------------------------------
    def _path(self, name: str) -> str:
        return os.path.join(self._config, name)

    def _load(self, name: str, default, expected_type):
        path = self._path(name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return default.copy() if hasattr(default, "copy") else default
        if not isinstance(data, expected_type):
            return default.copy() if hasattr(default, "copy") else default
        return data

    def _save(self, name: str, data):
        with self._lock:
            _atomic_write(self._path(name), json.dumps(data, indent=1, ensure_ascii=False))

    def save_settings(self):
        self._save("settings.json", self.settings)

    def save_favorites(self):
        self._save("favorites.json", sorted(self.favorites))

    def save_albums(self):
        self._save("albums.json", self.albums)

    # -- settings --------------------------------------------------------
    def get(self, key: str, default=None):
        return self.settings.get(key, DEFAULT_SETTINGS.get(key, default))

    def set(self, key: str, value, save: bool = True):
        self.settings[key] = value
        if save:
            self.save_settings()

    @property
    def custom_folders(self) -> list:
        folders = self.settings.get("custom_folders") or []
        return [f for f in folders if isinstance(f, str)]

    def add_folder(self, folder: str) -> bool:
        folder = os.path.abspath(os.path.expanduser(folder))
        if not os.path.isdir(folder):
            return False
        folders = self.custom_folders
        if folder in folders:
            return False
        folders.append(folder)
        self.set("custom_folders", folders)
        return True

    def remove_folder(self, folder: str):
        folders = [f for f in self.custom_folders if f != folder]
        self.set("custom_folders", folders)

    # -- favourites ------------------------------------------------------
    def is_favorite(self, path: str) -> bool:
        return path in self.favorites

    def toggle_favorite(self, path: str) -> bool:
        """Returns the new favourite state."""
        with self._lock:
            if path in self.favorites:
                self.favorites.discard(path)
                state = False
            else:
                self.favorites.add(path)
                state = True
            self.save_favorites()
        return state

    def set_favorite(self, paths, state: bool):
        with self._lock:
            for path in paths:
                if state:
                    self.favorites.add(path)
                else:
                    self.favorites.discard(path)
            self.save_favorites()

    def prune_favorites(self) -> int:
        """Drop favourites whose file disappeared."""
        with self._lock:
            missing = [p for p in self.favorites if not os.path.exists(p)]
            for path in missing:
                self.favorites.discard(path)
            if missing:
                self.save_favorites()
        return len(missing)

    # -- albums ----------------------------------------------------------
    def album_names(self) -> list:
        return sorted(self.albums.keys(), key=lambda n: n.lower())

    def album_items(self, name: str) -> list:
        return list(self.albums.get(name, []))

    def create_album(self, name: str, paths=()) -> bool:
        name = (name or "").strip()
        if not name:
            return False
        with self._lock:
            bucket = self.albums.setdefault(name, [])
            for path in paths:
                if path not in bucket:
                    bucket.append(path)
            self.save_albums()
        return True

    def add_to_album(self, name: str, paths) -> int:
        name = (name or "").strip()
        if not name:
            return 0
        added = 0
        with self._lock:
            bucket = self.albums.setdefault(name, [])
            for path in paths:
                if path not in bucket:
                    bucket.append(path)
                    added += 1
            self.save_albums()
        return added

    def remove_from_album(self, name: str, paths):
        with self._lock:
            bucket = self.albums.get(name)
            if bucket is None:
                return
            for path in paths:
                if path in bucket:
                    bucket.remove(path)
            self.save_albums()

    def delete_album(self, name: str):
        with self._lock:
            self.albums.pop(name, None)
            self.save_albums()

    def album_cover_paths(self, name: str, limit: int = 4) -> list:
        covers = []
        for path in self.albums.get(name, []):
            if os.path.exists(path):
                covers.append(path)
            if len(covers) >= limit:
                break
        return covers

    # -- recycle bin -----------------------------------------------------
    def list_trash(self) -> list:
        """Read the freedesktop trash directories (honours $XDG_DATA_HOME)."""
        files_dir = trash_files_dir()
        info_dir = trash_info_dir()
        if not os.path.isdir(files_dir):
            return []
        try:
            names = os.listdir(files_dir)
        except OSError:
            return []

        items = []
        for name in names:
            full = os.path.join(files_dir, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            original = ""
            deleted = ""
            info_path = os.path.join(info_dir, name + ".trashinfo")
            try:
                with open(info_path, "r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        line = line.strip()
                        if line.startswith("Path="):
                            original = urllib.parse.unquote(line[5:])
                        elif line.startswith("DeletionDate="):
                            stamp = line.split("=", 1)[1]
                            deleted = stamp.replace("T", "  ")[:16]
            except OSError:
                pass
            ext = os.path.splitext(name)[1].lower()
            kind = kind_of(ext)
            if kind is None:
                kinds = (IMAGE_EXTS, VIDEO_EXTS, DOC_EXTS)
                kind = "image" if ext in kinds[0] else "video" if ext in kinds[1] else "document"
            items.append(TrashItem(
                path=full,
                name=name,
                folder=os.path.dirname(original) if original else files_dir,
                ext=ext,
                kind=kind,
                size=st.st_size if os.path.isfile(full) else 0,
                mtime=st.st_mtime,
                original=original,
                deleted=deleted,
            ))
        items.sort(key=lambda i: i.mtime, reverse=True)
        return items

    def trash_count(self) -> int:
        try:
            return len(os.listdir(trash_files_dir()))
        except OSError:
            return 0

    def trash_paths(self, paths):
        """Move files to the desktop trash.  Returns ``(moved, [(path, error)])``."""
        moved, failed = 0, []
        for path in paths:
            ok, error = self.trash_one(path)
            if ok:
                moved += 1
            else:
                failed.append((path, error))
        return moved, failed

    def trash_one(self, path: str):
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(path):
            return False, "File no longer exists"
        if _HAVE_GIO:
            try:
                gfile = Gio.File.new_for_path(path)
                if gfile.trash(None):
                    with self._lock:
                        self.favorites.discard(path)
                        self.save_favorites()
                    return True, ""
            except Exception as error:      # GLib.Error, permission problems
                fallback_reason = str(error)
            else:
                fallback_reason = "trash() returned False"
        else:
            fallback_reason = "Gio unavailable"
        try:
            self._manual_trash(path)
        except OSError as error:
            return False, "%s (%s)" % (error, fallback_reason)
        with self._lock:
            self.favorites.discard(path)
            self.save_favorites()
        return True, ""

    def _manual_trash(self, path: str):
        """Freedesktop trash implementation used when Gio is absent."""
        files_dir = trash_files_dir()
        info_dir = trash_info_dir()
        os.makedirs(files_dir, exist_ok=True)
        os.makedirs(info_dir, exist_ok=True)
        base = os.path.basename(path)
        stem, ext = os.path.splitext(base)
        target = base
        counter = 1
        while os.path.exists(os.path.join(files_dir, target)):
            target = "%s.%d%s" % (stem, counter, ext)
            counter += 1
        destination = os.path.join(files_dir, target)
        info_path = os.path.join(info_dir, target + ".trashinfo")
        payload = (
            "[Trash Info]\nPath=%s\nDeletionDate=%s\n"
            % (urllib.parse.quote(path), time.strftime("%Y-%m-%dT%H:%M:%S"))
        )
        _atomic_write(info_path, payload)
        try:
            shutil.move(path, destination)
        except OSError:
            try:
                os.unlink(info_path)
            except OSError:
                pass
            raise

    def restore(self, item: TrashItem):
        """Put a trashed file back where it came from."""
        source = item.path
        if not os.path.exists(source):
            return False, "File is gone from the bin"
        original = item.original
        if not original:
            return False, "No original location recorded"
        folder = os.path.dirname(original)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as error:
            return False, "Cannot create folder: %s" % error
        target = original
        if os.path.exists(target):
            stem, ext = os.path.splitext(original)
            counter = 1
            while os.path.exists(target):
                target = "%s (restored %d)%s" % (stem, counter, ext)
                counter += 1
        try:
            shutil.move(source, target)
        except OSError as error:
            return False, str(error)
        self._drop_trashinfo(item)
        return True, target

    def delete_forever(self, item: TrashItem) -> bool:
        try:
            if os.path.isdir(item.path) and not os.path.islink(item.path):
                shutil.rmtree(item.path)
            else:
                os.unlink(item.path)
        except OSError:
            return False
        self._drop_trashinfo(item)
        return True

    def _drop_trashinfo(self, item: TrashItem):
        try:
            os.unlink(os.path.join(trash_info_dir(), os.path.basename(item.path) + ".trashinfo"))
        except OSError:
            pass

    def empty_trash(self) -> int:
        removed = 0
        for item in self.list_trash():
            if self.delete_forever(item):
                removed += 1
        return removed


def unique_path(directory: str, name: str) -> str:
    """Return a non clashing path inside *directory*."""
    stem, ext = os.path.splitext(name)
    candidate = os.path.join(directory, name)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, "%s (%d)%s" % (stem, counter, ext))
        counter += 1
    return candidate


def new_album_id() -> str:
    return uuid.uuid4().hex
