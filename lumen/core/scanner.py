"""LUMEN - filesystem discovery and file metadata helpers.

This module is deliberately free of any GTK import so the scanning logic can
be exercised head-less.  :class:`Scanner` wraps the pure generator in a worker
thread and pushes batches to the UI through a caller supplied dispatcher
(normally ``GLib.idle_add``).
"""

from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import dataclass

IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".svg", ".heic", ".heif", ".avif", ".jxl", ".ico",
})
VIDEO_EXTS = frozenset({
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v",
    ".3gp", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".ogv",
})
DOC_EXTS = frozenset({
    ".pdf", ".txt", ".md", ".rtf", ".doc", ".docx", ".odt",
    ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp",
    ".csv", ".epub", ".xps", ".djvu",
})

#: Directories that are never descended into (matched case-insensitively).
SKIP_DIR_NAMES = frozenset({
    ".cache", ".local", ".config", ".git", ".hg", ".svn", ".thumbnails",
    ".npm", ".cargo", ".rustup", ".mozilla", ".steam", ".var", ".dbus",
    ".gvfs", ".Trash".lower(), ".trash", "node_modules", "venv", "__pycache__",
    "site-packages", "snap", "lost+found", ".thumbcache", ".dropbox.cache",
    ".wine", ".playonlinux", "steamapps", "compatdata",
})
SKIP_DIR_SUFFIXES = (".venv", "-venv", "_venv")

SCREENSHOT_HINTS = ("screenshot", "screen shot", "screen_shot", "snipping", "snip_")

MAX_FILES = 20000
MAX_DEPTH = 12

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(num: float) -> str:
    """Return a short human readable size such as ``4.2 MB``."""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "-"
    if value <= 0:
        return "0 B"
    for unit in _SIZE_UNITS:
        if value < 1024 or unit == _SIZE_UNITS[-1]:
            if unit == "B":
                return "%d %s" % (int(value), unit)
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f TB" % value


def human_duration(seconds: float) -> str:
    """``93`` -> ``1:33``, ``3672`` -> ``1:01:12``."""
    if not seconds or seconds <= 0:
        return ""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def kind_of(ext: str):
    """Classify an extension (with leading dot, any case)."""
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in DOC_EXTS:
        return "document"
    return None


def home_dir() -> str:
    return os.path.expanduser("~")


def default_roots(home: str = None) -> list:
    """The folders LUMEN scans by default (only those that exist)."""
    home = home or home_dir()
    candidates = [
        os.path.join(home, "Pictures"),
        os.path.join(home, "Downloads"),
        os.path.join(home, "DCIM"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Videos"),
    ]
    return [p for p in candidates if os.path.isdir(p)]


def documents_roots(home: str = None) -> list:
    """Folders treated as 'Documents' media (Documents + screenshots)."""
    home = home or home_dir()
    out = [os.path.join(home, "Documents")]
    return [p for p in out if os.path.isdir(p)]


def is_under(path: str, folder: str) -> bool:
    """True when *path* lives inside *folder*."""
    try:
        path = os.path.abspath(path)
        folder = os.path.abspath(folder)
    except (TypeError, ValueError):
        return False
    return path == folder or path.startswith(folder + os.sep)


@dataclass
class MediaItem:
    """A single picture, clip or document on disk."""

    path: str
    name: str
    folder: str
    ext: str
    kind: str
    size: int
    mtime: float
    width: int = 0
    height: int = 0
    duration: float = 0.0

    # -- convenience -----------------------------------------------------
    @property
    def parent_name(self) -> str:
        return os.path.basename(self.folder.rstrip(os.sep)) or self.folder

    @property
    def date_text(self) -> str:
        return time.strftime("%d %b %Y  %H:%M", time.localtime(self.mtime))

    @property
    def date_short(self) -> str:
        return time.strftime("%d %b %Y", time.localtime(self.mtime))

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
        low = self.name.lower()
        return any(hint in low for hint in SCREENSHOT_HINTS)

    @property
    def dimensions(self) -> str:
        if self.width and self.height:
            return "%d x %d" % (self.width, self.height)
        return "-"

    @property
    def search_blob(self) -> str:
        """Pre-lowercased haystack for the live search box."""
        return "%s\n%s\n%s\n%s" % (
            self.name.lower(),
            self.parent_name.lower(),
            self.date_short.lower(),
            self.kind,
        )

    def refresh(self) -> bool:
        """Re-stat the file, returns False when it vanished."""
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        self.size = st.st_size
        self.mtime = st.st_mtime
        return True


def _classify_name(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return kind_of(ext)


def iter_media(roots, *, max_files: int = MAX_FILES, max_depth: int = MAX_DEPTH,
               excluded=(), progress=None):
    """Yield :class:`MediaItem` for every supported file under *roots*.

    *excluded* holds lower-case directory names that must be pruned.
    Directories are de-duplicated by inode so overlapping roots such as
    ``~/Pictures`` and ``~/Pictures/2024`` are only walked once.
    """
    excluded = {e.lower() for e in excluded}
    seen_dirs = set()
    found = 0

    for root in roots:
        if not root:
            continue
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        stack = [(root, 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                st = os.stat(folder)
            except OSError:
                continue
            ident = (st.st_dev, st.st_ino)
            if ident in seen_dirs:
                continue
            seen_dirs.add(ident)

            try:
                with os.scandir(folder) as it:
                    entries = list(it)
            except OSError:
                continue

            subdirs = []
            for entry in entries:
                name = entry.name
                if not name or name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if depth + 1 > max_depth:
                            continue
                        low = name.lower()
                        if low in SKIP_DIR_NAMES or low in excluded:
                            continue
                        if low.endswith(SKIP_DIR_SUFFIXES):
                            continue
                        subdirs.append((entry.path, depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    kind = _classify_name(name)
                    if kind is None:
                        continue
                    st_file = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                found += 1
                yield MediaItem(
                    path=entry.path,
                    name=name,
                    folder=folder,
                    ext=os.path.splitext(name)[1].lower(),
                    kind=kind,
                    size=st_file.st_size,
                    mtime=st_file.st_mtime,
                )
                if progress is not None and found % 200 == 0:
                    try:
                        progress(found, folder)
                    except Exception:
                        pass
                if found >= max_files:
                    return

            stack.extend(reversed(subdirs))


def iter_folder(folder: str):
    """Yield items from a single (non recursive) folder - used by file monitors."""
    folder = os.path.abspath(os.path.expanduser(folder))
    try:
        with os.scandir(folder) as it:
            entries = list(it)
    except OSError:
        return
    for entry in entries:
        if not entry.name or entry.name.startswith("."):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            kind = _classify_name(entry.name)
            if kind is None:
                continue
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        yield MediaItem(
            path=entry.path,
            name=entry.name,
            folder=folder,
            ext=os.path.splitext(entry.name)[1].lower(),
            kind=kind,
            size=st.st_size,
            mtime=st.st_mtime,
        )


class Scanner:
    """Background scanner that streams batches to the GTK main loop."""

    def __init__(self, dispatch, on_batch, on_done, on_progress=None):
        self._dispatch = dispatch          # callable(fn, *args) -> idle source
        self._on_batch = on_batch
        self._on_done = on_done
        self._on_progress = on_progress
        self._generation = 0
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    # -- public ----------------------------------------------------------
    @property
    def generation(self):
        return self._generation

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, roots, extra_roots=(), max_files: int = MAX_FILES):
        roots = [os.path.abspath(os.path.expanduser(r)) for r in
                 list(roots) + list(extra_roots) if r]
        cleaned = []
        for root in roots:
            if root not in cleaned and os.path.isdir(root):
                cleaned.append(root)
        self.stop()
        with self._lock:
            self._stop = threading.Event()
            self._generation += 1
            generation = self._generation
            thread = threading.Thread(
                target=self._run,
                args=(generation, cleaned, max_files, self._stop),
                name="lumen-scan",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return generation

    def stop(self):
        with self._lock:
            self._stop.set()
            self._thread = None
        # a new generation makes any in-flight batch results stale

    # -- internals -------------------------------------------------------
    def _run(self, generation, roots, max_files, stop_event):
        batch = []
        try:
            for item in iter_media(
                roots,
                max_files=max_files,
                progress=lambda count, folder: self._progress(generation, count, folder),
            ):
                if stop_event.is_set() or generation != self._generation:
                    return
                batch.append(item)
                if len(batch) >= 40:
                    self._emit(generation, batch)
                    batch = []
            if batch and not stop_event.is_set() and generation == self._generation:
                self._emit(generation, batch)
        except Exception:
            # scanning must never take the UI down
            pass
        finally:
            if not stop_event.is_set() and generation == self._generation:
                self._dispatch(self._finish, generation)

    def _emit(self, generation, batch):
        if generation != self._generation or self._stop.is_set():
            return
        self._dispatch(self._deliver, generation, batch)

    def _progress(self, generation, count, folder):
        if self._on_progress is None or generation != self._generation:
            return
        self._dispatch(self._report, generation, count, folder)

    def _deliver(self, generation, batch):
        if generation != self._generation:
            return False
        try:
            self._on_batch(batch)
        except Exception:
            pass
        return False

    def _report(self, generation, count, folder):
        if generation != self._generation:
            return False
        try:
            self._on_progress(count, folder)
        except Exception:
            pass
        return False

    def _finish(self, generation):
        if generation != self._generation:
            return False
        try:
            self._on_done()
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# EXIF / metadata helpers (pure Python, no optional dependencies)
# ---------------------------------------------------------------------------

_BYTE_ORDERS = {b"II": "<", b"MM": ">"}
_EXIF_DATE_TAGS = {0x0132, 0x9003, 0x9004}     # DateTime, DateTimeOriginal/Digitized


def _read_ifd(tiff: bytes, offset: int, bo: str, depth: int = 0):
    """Return ``{tag: (type, count, value_bytes, value_offset)}`` for one IFD."""
    out = {}
    if depth > 2 or offset <= 0 or offset + 2 > len(tiff):
        return out
    try:
        (count,) = struct.unpack(bo + "H", tiff[offset:offset + 2])
    except struct.error:
        return out
    base = offset + 2
    for index in range(min(count, 256)):
        pos = base + index * 12
        if pos + 12 > len(tiff):
            break
        try:
            tag, typ, num = struct.unpack(bo + "HHI", tiff[pos:pos + 8])
        except struct.error:
            break
        value_field = tiff[pos + 8:pos + 12]
        sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
        size = sizes.get(typ, 1) * num
        if size <= 4:
            raw = value_field[:size]
        else:
            try:
                (value_offset,) = struct.unpack(bo + "I", value_field)
            except struct.error:
                continue
            if value_offset + size > len(tiff):
                continue
            raw = tiff[value_offset:value_offset + size]
        out[int(tag)] = (int(typ), int(num), raw)
    return out


def _clean_exif_date(raw: bytes):
    text = raw.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
    if len(text) < 10:
        return None
    # EXIF uses "YYYY:MM:DD HH:MM:SS"
    if text[4] == ":" and text[7] == ":":
        text = text[:4] + "-" + text[5:7] + "-" + text[8:]
    return text


def read_exif_date(path: str):
    """Best effort EXIF date string (``YYYY-MM-DD HH:MM:SS``) or ``None``."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(256 * 1024)
    except OSError:
        return None
    marker = head.find(b"Exif\x00\x00")
    if marker < 0 or marker + 12 > len(head):
        return None
    tiff = head[marker + 6:]
    if len(tiff) < 8:
        return None
    bo = _BYTE_ORDERS.get(tiff[:2])
    if bo is None:
        return None
    try:
        (ifd0,) = struct.unpack(bo + "I", tiff[4:8])
    except struct.error:
        return None

    found = {}
    primary = _read_ifd(tiff, ifd0, bo)
    for tag, value in primary.items():
        if tag in _EXIF_DATE_TAGS:
            found[tag] = value[2]
    exif_ifd = primary.get(0x8769)
    if exif_ifd is not None:
        try:
            offset = struct.unpack(bo + "I", exif_ifd[2][:4])[0]
        except struct.error:
            offset = 0
        if offset:
            for tag, value in _read_ifd(tiff, offset, bo, depth=1).items():
                if tag in _EXIF_DATE_TAGS:
                    found.setdefault(tag, value[2])

    for tag in (0x9003, 0x9004, 0x0132):
        raw = found.get(tag)
        if not raw:
            continue
        cleaned = _clean_exif_date(raw)
        if cleaned:
            return cleaned
    return None


def _parse_mvhd(data: bytes, index: int) -> float:
    """Duration in seconds from an ISO-BMFF ``mvhd`` payload."""
    base = index + 4                       # skip the box type itself
    if base + 20 > len(data):
        return 0.0
    version = data[base]
    try:
        if version == 1:
            if base + 32 > len(data):
                return 0.0
            timescale = struct.unpack(">I", data[base + 20:base + 24])[0]
            duration = struct.unpack(">Q", data[base + 24:base + 32])[0]
        else:
            timescale = struct.unpack(">I", data[base + 12:base + 16])[0]
            duration = struct.unpack(">I", data[base + 16:base + 20])[0]
    except struct.error:
        return 0.0
    if not timescale or duration in (0, 0xFFFFFFFF):
        return 0.0
    seconds = duration / float(timescale)
    return seconds if 0 < seconds < 360000 else 0.0


def mp4_duration(path: str) -> float:
    """Read clip length from an MP4/MOV ``mvhd`` box - pure stdlib, no ffprobe."""
    try:
        total = os.path.getsize(path)
    except OSError:
        return 0.0

    def scan(window_start: int, window_size: int) -> float:
        try:
            with open(path, "rb") as handle:
                handle.seek(window_start)
                blob = handle.read(window_size)
        except OSError:
            return 0.0
        index = blob.find(b"mvhd")
        while index != -1:
            seconds = _parse_mvhd(blob, index)
            if seconds:
                return seconds
            index = blob.find(b"mvhd", index + 4)
        return 0.0

    seconds = scan(0, 2 * 1024 * 1024)
    if seconds:
        return seconds
    if total > 2 * 1024 * 1024:
        tail = min(total, 4 * 1024 * 1024)
        seconds = scan(total - tail, tail)
    return seconds or 0.0


MP4_EXTS = frozenset({".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".f4v"})


def content_type(path: str) -> str:
    """Cheap mime guess used by 'Open with' / trashinfo."""
    import mimetypes
    guessed, _encoding = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"
