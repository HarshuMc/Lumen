"""GdkPixbuf based thumbnail pipeline for LUMEN.

* thumbnails are decoded / scaled in worker threads (2 by default)
* results are cached on disk as JPEG (quality 85) under ``~/.cache/lumen/thumbs``
* an LRU memory cache keeps a handful of ready pixbufs for smooth scrolling
* video posters use ``ffmpegthumbnailer`` when it exists, never a bundled binary

Only GdkPixbuf (and optionally the system ``ffmpegthumbnailer`` / ``ffprobe``
commands) is used - no Pillow, no numpy, no bundled ffmpeg.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
from collections import OrderedDict

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from .scanner import MP4_EXTS, human_duration, mp4_duration  # noqa: E402,F401

THUMB_SIZE = 200
MEMORY_LIMIT = 16 * 1024 * 1024        # ~16 MB of pixbufs
DISK_LIMIT_FILES = 6000
JPEG_QUALITY = "85"
MAX_DECODE_BYTES = 160 * 1024 * 1024   # refuse absurd sources


def _dispatch_default(func, *args):
    GLib.idle_add(func, *args)


def tool_path(name: str):
    return shutil.which(name)


def load_scaled(path: str, size: int):
    """Decode *path* bounded to *size* x *size*, keeping the aspect ratio."""
    try:
        if os.path.getsize(path) > MAX_DECODE_BYTES:
            return None
    except OSError:
        return None
    try:
        return GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
    except (GLib.Error, ValueError, OSError):
        return None


def flatten(pixbuf):
    """Composite transparency onto a dark plate so JPEG keeps looking right."""
    if not pixbuf.get_has_alpha():
        return pixbuf
    width, height = pixbuf.get_width(), pixbuf.get_height()
    plate = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
    plate.fill(0x1C1C22FF)
    pixbuf.composite(plate, 0, 0, width, height, 0, 0, 1.0, 1.0,
                     GdkPixbuf.InterpType.BILINEAR, 255)
    return plate


def image_info(path: str):
    """(width, height, format) without decoding the pixels."""
    try:
        info = GdkPixbuf.Pixbuf.get_file_info(path)
    except (GLib.Error, ValueError, OSError):
        return (0, 0, "")
    fmt = info[0]
    return (int(info[1] or 0), int(info[2] or 0), fmt.get_name() if fmt else "")


def histogram(path: str, bins: int = 48):
    """RGB histograms of a downscaled copy - used by the details panel."""
    pixbuf = load_scaled(path, 96)
    if pixbuf is None:
        return None
    try:
        pixels = pixbuf.get_pixels()
        stride = pixbuf.get_rowstride()
        channels = pixbuf.get_n_channels()
        height = pixbuf.get_height()
        width = pixbuf.get_width()
    except Exception:
        return None
    red = [0] * bins
    green = [0] * bins
    blue = [0] * bins
    scale = bins / 256.0
    step = 1 if width < 160 else 2
    for y in range(0, height, 2):
        row = y * stride
        for x in range(0, width, step):
            offset = row + x * channels
            if offset + 2 >= len(pixels):
                continue
            red[min(bins - 1, int(pixels[offset] * scale))] += 1
            green[min(bins - 1, int(pixels[offset + 1] * scale))] += 1
            blue[min(bins - 1, int(pixels[offset + 2] * scale))] += 1
    peak = max(max(red), max(green), max(blue)) or 1
    return (red, green, blue, peak)


class Thumbnailer:
    """Threaded thumbnail provider with disk + memory caching."""

    def __init__(self, cache_root: str, size: int = THUMB_SIZE, workers: int = 2,
                 memory_limit: int = MEMORY_LIMIT, dispatch=None):
        self.cache_root = cache_root
        self.size = size
        self.memory_limit = memory_limit
        self._dispatch = dispatch or _dispatch_default
        self._lock = threading.Lock()
        self._memory = OrderedDict()          # key -> pixbuf
        self._memory_bytes = 0
        self._pending = {}                    # key -> [callbacks]
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._probed = {}                     # path -> duration (seconds)
        self._meta_dirty = False
        self._meta_path = os.path.join(os.path.dirname(cache_root.rstrip(os.sep)), "meta.json")
        self._video_tool = tool_path("ffmpegthumbnailer")
        self._ffmpeg_tool = tool_path("ffmpeg")
        self._probe_tool = tool_path("ffprobe")
        try:
            os.makedirs(self.cache_root, exist_ok=True)
        except OSError:
            pass
        self._load_meta()
        self._workers = []
        for index in range(max(1, workers)):
            thread = threading.Thread(target=self._worker, name="lumen-thumb-%d" % index,
                                      daemon=True)
            thread.start()
            self._workers.append(thread)

    # -- meta ------------------------------------------------------------
    def _load_meta(self):
        try:
            with open(self._meta_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                for key, value in list(data.items())[:4000]:
                    try:
                        self._probed[key] = float(value)
                    except (TypeError, ValueError):
                        continue
        except (OSError, ValueError):
            pass

    def flush_meta(self):
        if not self._meta_dirty:
            return
        self._meta_dirty = False
        try:
            os.makedirs(os.path.dirname(self._meta_path), exist_ok=True)
            with open(self._meta_path, "w", encoding="utf-8") as handle:
                json.dump({k: round(v, 2) for k, v in list(self._probed.items())[:4000]}, handle)
        except OSError:
            pass

    # -- cache keys ------------------------------------------------------
    def _key(self, path: str, mtime: float, size: int = None) -> str:
        raw = "%s|%d|%d" % (path, int(mtime), size or self.size)
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()

    def _disk_path(self, key: str) -> str:
        return os.path.join(self.cache_root, key[:2], key + ".jpg")

    # -- memory cache ----------------------------------------------------
    @staticmethod
    def _pixbuf_bytes(pixbuf) -> int:
        if pixbuf is None:
            return 0
        try:
            return pixbuf.get_rowstride() * pixbuf.get_height()
        except Exception:
            return 0

    def _remember(self, key: str, pixbuf):
        with self._lock:
            old = self._memory.pop(key, None)
            if old is not None:
                self._memory_bytes -= self._pixbuf_bytes(old)
            self._memory[key] = pixbuf
            self._memory_bytes += self._pixbuf_bytes(pixbuf)
            while self._memory_bytes > self.memory_limit and len(self._memory) > 1:
                _gone_key, gone = self._memory.popitem(last=False)
                self._memory_bytes -= self._pixbuf_bytes(gone)
        return pixbuf

    def cached(self, item, size: int = None):
        """Return a ready pixbuf from the memory cache, or ``None``."""
        try:
            key = self._key(item.path, item.mtime, size)
        except AttributeError:
            return None
        with self._lock:
            pixbuf = self._memory.get(key)
            if pixbuf is not None:
                self._memory.move_to_end(key)
            return pixbuf

    # -- public API ------------------------------------------------------
    def request(self, item, callback, size: int = None):
        """Ask for a thumbnail; *callback(pixbuf_or_None)* runs on the UI thread."""
        try:
            key = self._key(item.path, item.mtime, size)
        except AttributeError:
            return
        ready = self.cached(item, size)
        if ready is not None:
            self._dispatch(callback, ready)
            return

        with self._lock:
            callbacks = self._pending.get(key)
            if callbacks is not None:
                if callback is not None:
                    callbacks.append(callback)
                return
            self._pending[key] = [callback] if callback is not None else []
        self._queue.put((key, item.path, item.mtime, item.kind, size or self.size))

    def prefetch(self, item, size: int = None):
        self.request(item, None, size)

    def duration(self, item):
        """Known video duration in seconds (0.0 when unknown)."""
        with self._lock:
            value = self._probed.get(item.path)
        if value is None:
            return float(getattr(item, "duration", 0.0) or 0.0)
        return value

    def duration_text(self, item):
        return human_duration(self.duration(item))

    def probe_duration(self, path: str, callback=None):
        if not self._probe_tool:
            return
        with self._lock:
            if path in self._probed:
                if callback is not None:
                    self._dispatch(callback, self._probed[path])
                return
        self._queue.put(("__probe__", path, 0, "video", self.size, callback))

    # -- workers ---------------------------------------------------------
    def _worker(self):
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                break
            if task[0] == "__probe__":
                _key_name, path, _mtime, _kind, _size = task[:5]
                callback = task[5] if len(task) > 5 else None
                seconds = self._probe(path)
                if seconds:
                    with self._lock:
                        self._probed[path] = seconds
                        self._meta_dirty = True
                    if callback is not None:
                        self._dispatch(callback, seconds)
                continue

            key, path, mtime, kind, size = task
            pixbuf = None
            try:
                pixbuf = self._produce(key, path, mtime, kind, size)
            except Exception:
                pixbuf = None
            callbacks = []
            with self._lock:
                callbacks = self._pending.pop(key, [])
            if pixbuf is not None:
                self._remember(key, pixbuf)
            for callback in callbacks:
                if callback is not None:
                    self._dispatch(callback, pixbuf)

    def _produce(self, key, path, mtime, kind, size):
        disk = self._disk_path(key)
        if os.path.exists(disk):
            pixbuf = load_scaled(disk, max(size, 64))
            if pixbuf is not None:
                return pixbuf
            try:
                os.unlink(disk)
            except OSError:
                pass

        if kind == "video":
            pixbuf = self._video_thumbnail(path, size)
        else:
            pixbuf = load_scaled(path, size)
        if pixbuf is None:
            return None
        return self._store(disk, pixbuf)

    def _store(self, disk: str, pixbuf):
        try:
            os.makedirs(os.path.dirname(disk), exist_ok=True)
            out = flatten(pixbuf)
            out.savev(disk, "jpeg", ["quality"], [JPEG_QUALITY])
        except (GLib.Error, OSError, ValueError):
            pass
        return pixbuf

    def _video_thumbnail(self, path: str, size: int):
        """Poster frame via the system ffmpegthumbnailer or ffmpeg (never bundled)."""
        if not self._video_tool and not self._ffmpeg_tool:
            return None
        tmp = os.path.join(self.cache_root, "video-%s.jpg" % hashlib.sha1(
            ("%s|%s" % (path, os.path.getmtime(path) if os.path.exists(path) else 0))
            .encode("utf-8", "replace")).hexdigest())
        commands = []
        if self._video_tool:
            commands.append([self._video_tool, "-i", path, "-o", tmp,
                             "-s", str(max(256, size * 2)), "-q", "8", "-t", "12%"])
        if self._ffmpeg_tool:
            commands.append([self._ffmpeg_tool, "-loglevel", "error", "-y",
                             "-ss", "1", "-i", path, "-frames:v", "1",
                             "-vf", "scale='min(%d,iw)':-2" % max(256, size * 2), tmp])
        for command in commands:
            try:
                result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=25)
                if result.returncode == 0 and os.path.exists(tmp):
                    pixbuf = load_scaled(tmp, size)
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    if pixbuf is not None:
                        return pixbuf
            except (OSError, subprocess.SubprocessError):
                continue
        return None

    def _probe(self, path: str):
        """Clip length: ffprobe when present, otherwise the MP4 header parser."""
        seconds = None
        if self._probe_tool:
            try:
                result = subprocess.run(
                    [self._probe_tool, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", path],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12,
                )
                text = (result.stdout or b"").decode("utf-8", "ignore").strip().splitlines()
                if text:
                    seconds = float(text[0])
            except (OSError, ValueError, subprocess.SubprocessError):
                seconds = None
        if not seconds:
            ext = os.path.splitext(path)[1].lower()
            if ext in MP4_EXTS:
                seconds = mp4_duration(path) or None
        return seconds

    # -- housekeeping ----------------------------------------------------
    def trim_disk_cache(self, limit: int = DISK_LIMIT_FILES):
        """Delete the oldest cached thumbnails when the cache grows too big."""
        entries = []
        try:
            for folder in os.listdir(self.cache_root):
                sub = os.path.join(self.cache_root, folder)
                if not os.path.isdir(sub):
                    continue
                try:
                    names = os.listdir(sub)
                except OSError:
                    continue
                for name in names:
                    if not name.endswith(".jpg"):
                        continue
                    full = os.path.join(sub, name)
                    try:
                        entries.append((os.path.getmtime(full), full))
                    except OSError:
                        continue
        except OSError:
            return 0
        if len(entries) <= limit:
            return 0
        entries.sort()
        removed = 0
        for _stamp, full in entries[: len(entries) - limit]:
            try:
                os.unlink(full)
                removed += 1
            except OSError:
                pass
        return removed

    def shutdown(self):
        self._stop.set()
        self.flush_meta()
        for _ in self._workers:
            self._queue.put(None)


def load_preview(path: str, max_dimension: int = 2600):
    """Full-window preview for the lightbox (bounded for memory safety)."""
    return load_scaled(path, max_dimension)
