#!/usr/bin/env python3
"""LUMEN - Your Memories, Clearly.

A glassy, zero-dependency gallery for GTK 3 desktops (tested on Debian 13 /
LXQt with picom).  Uses only the Python standard library, PyGObject (Gtk, Gdk,
Gio, GLib, GdkPixbuf, Pango) and Cairo.

    python3 main.py                 # launch
    python3 main.py ~/Pictures      # launch scanning a folder
    python3 main.py session.jpg     # launch and open a file
    python3 main.py --check         # verify the environment, no window
    python3 main.py --version
"""

from __future__ import annotations

import os
import signal
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

APP_NAME = "LUMEN"
APP_VERSION = "1.0"

USAGE = """LUMEN {version} - Your Memories, Clearly.

Usage: python3 main.py [options] [folder | file]

Options:
  -h, --help      show this help and exit
      --version   print the version
      --check     verify GTK / cairo / folders without opening a window

Keyboard:
  Ctrl+A select all      Ctrl+F search        F5 rescan
  Enter  open viewer     Esc close/clear     F11 fullscreen
  <- ->  previous/next (viewer)   scroll to zoom   drag to pan
""".format(version=APP_VERSION)


def check_environment():
    """Return (ok, lines) describing the runtime environment."""
    ok = True
    lines = ["LUMEN %s environment check" % APP_VERSION, ""]
    gtk = None
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GdkPixbuf", "2.0")
        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Gtk as gtk
        lines.append("  GTK 3 / PyGObject .......... OK")
    except Exception as error:
        ok = False
        lines.append("  GTK 3 / PyGObject .......... MISSING (%s)" % error)
        lines.append("      sudo apt install python3-gi gir1.2-gtk-3.0 python3-gi-cairo")
    try:
        import cairo
        lines.append("  Cairo (pycairo) ............ OK (%s)" % cairo.version)
    except Exception as error:
        ok = False
        lines.append("  Cairo (pycairo) ............ MISSING (%s)" % error)
        lines.append("      sudo apt install python3-cairo")

    try:
        from core.storage import config_dir, thumbs_dir
        from core.scanner import default_roots, home_dir
        lines.append("  Config folder .............. %s" % config_dir())
        lines.append("  Thumbnail cache ............ %s" % thumbs_dir())
        lines.append("  Scan roots ................. %d folder(s)" % len(default_roots()))
        for folder in default_roots():
            lines.append("      %s" % folder)
        if not default_roots():
            lines.append("      (no standard folders found under %s)" % home_dir())
    except Exception as error:
        ok = False
        lines.append("  Core modules ............... FAILED (%s)" % error)

    css = ""
    try:
        with open(os.path.join(BASE_DIR, "style.css"), "r", encoding="utf-8") as handle:
            css = handle.read()
        lines.append("  Stylesheet ................. OK (%d bytes)" % len(css))
    except OSError as error:
        ok = False
        lines.append("  Stylesheet ................. MISSING (%s)" % error)

    if gtk is not None and css:
        try:
            provider = gtk.CssProvider()
            provider.load_from_data(css.encode("utf-8"))
            lines.append("  GTK CSS parse .............. OK")
        except Exception as error:
            ok = False
            lines.append("  GTK CSS parse .............. FAILED (%s)" % error)

    import shutil as _shutil
    for tool, purpose in (
        ("ffmpegthumbnailer", "video poster frames"),
        ("ffprobe", "video durations"),
        ("feh", "set wallpaper"),
        ("pcmanfm-qt", "set wallpaper (LXQt)"),
    ):
        found = _shutil.which(tool)
        lines.append("  %-19s ...... %s (%s)" % (tool, "found" if found else "not installed", purpose))

    lines.append("")
    lines.append("  Result: %s" % ("ready to launch" if ok else "missing requirements"))
    return ok, lines


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)

    if "-h" in args or "--help" in args:
        print(USAGE)
        return 0
    if "--version" in args:
        print("LUMEN %s" % APP_VERSION)
        return 0
    if "--check" in args:
        ok, lines = check_environment()
        print("\n".join(lines))
        return 0 if ok else 1

    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import GLib, Gtk
    except Exception as error:
        sys.stderr.write(
            "LUMEN needs GTK 3 and PyGObject.\n\n"
            "  sudo apt install python3-gi gir1.2-gtk-3.0 python3-gi-cairo\n\n"
            "Details: %s\n" % error)
        return 1

    argv_gtk = ["lumen"] + args
    try:
        ready = Gtk.init_check(argv_gtk)
    except Exception:
        ready = True
    if not ready:
        sys.stderr.write(
            "LUMEN: no display found. Start it inside your desktop session "
            "(DISPLAY / WAYLAND_DISPLAY must be set).\n")
        return 1

    GLib.set_prgname("lumen")
    GLib.set_application_name(APP_NAME)

    from core.storage import Storage, ensure_dirs
    from ui.window import LumenWindow

    ensure_dirs()
    store = Storage()
    start_path = args[0] if args else None
    window = LumenWindow(store, start_path=start_path)
    window.connect("destroy", Gtk.main_quit)

    def _interrupt():
        Gtk.main_quit()
        return False

    try:
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _interrupt)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _interrupt)
    except Exception:
        pass

    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
