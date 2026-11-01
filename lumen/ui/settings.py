"""LUMEN appearance layer - theme tokens, CSS providers and palette helpers.

All colours live in :data:`THEMES` and are injected into GTK's CSS engine as
``@define-color lumen_*`` tokens.  ``style.css`` only references those tokens,
so switching theme or moving the opacity slider is a single provider swap.
Cairo drawing code asks :func:`palette` / :func:`color` for the same values.
"""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLE_FILE = os.path.join(PACKAGE_ROOT, "style.css")

THEMES = {
    "dark": {
        "bg": (28, 28, 34),
        "bg_solid": "#1B1B21",
        "text": "#F5F4FB",
        "text_dim": "rgba(245,244,251,0.56)",
        "border": "rgba(255,255,255,0.14)",
        "hover": "rgba(255,255,255,0.09)",
        "selected": "rgba(124,58,237,0.32)",
        "accent": "#7C3AED",
        "accent_soft": "rgba(124,58,237,0.22)",
        "accent2": "#F59E0B",
        "panel": "rgba(255,255,255,0.055)",
        "panel_soft": "rgba(255,255,255,0.032)",
        "header": "rgba(255,255,255,0.045)",
        "input": "rgba(255,255,255,0.075)",
        "scroll": "rgba(255,255,255,0.22)",
        "shadow": "rgba(0,0,0,0.55)",
        "plate": "rgba(255,255,255,0.06)",
        "viewer": "rgba(10,10,14,0.96)",
        "on_accent": "#FFFFFF",
    },
    "light": {
        "bg": (247, 247, 252),
        "bg_solid": "#F4F4FA",
        "text": "#17171F",
        "text_dim": "rgba(23,23,31,0.58)",
        "border": "rgba(23,23,31,0.12)",
        "hover": "rgba(23,23,31,0.06)",
        "selected": "rgba(124,58,237,0.16)",
        "accent": "#6D28D9",
        "accent_soft": "rgba(109,40,217,0.14)",
        "accent2": "#D97706",
        "panel": "rgba(255,255,255,0.62)",
        "panel_soft": "rgba(255,255,255,0.45)",
        "header": "rgba(255,255,255,0.55)",
        "input": "rgba(255,255,255,0.78)",
        "scroll": "rgba(23,23,31,0.20)",
        "shadow": "rgba(20,20,40,0.22)",
        "plate": "rgba(23,23,31,0.06)",
        "viewer": "rgba(12,12,18,0.94)",
        "on_accent": "#FFFFFF",
    },
}

TOKEN_KEYS = (
    "bg_solid", "text", "text_dim", "border", "hover", "selected",
    "accent", "accent_soft", "accent2", "panel", "panel_soft", "header",
    "input", "scroll", "shadow", "plate", "viewer", "on_accent",
)

_state = {"theme": "dark", "opacity": 0.82, "colors": {}}


def parse_color(value, default=(1.0, 1.0, 1.0, 1.0)):
    """Parse ``#rrggbb`` / ``rgba(...)`` / palette key into cairo floats."""
    if isinstance(value, (tuple, list)):
        if len(value) == 4:
            return tuple(float(v) for v in value)
        if len(value) == 3:
            return (float(value[0]), float(value[1]), float(value[2]), 1.0)
    if not isinstance(value, str) or not value:
        return default
    text = value.strip()
    if text.startswith("#"):
        raw = text[1:]
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        if len(raw) >= 6:
            try:
                return (int(raw[0:2], 16) / 255.0, int(raw[2:4], 16) / 255.0,
                        int(raw[4:6], 16) / 255.0, 1.0)
            except ValueError:
                return default
    if text.startswith("rgba(") or text.startswith("rgb("):
        body = text[text.index("(") + 1:text.rindex(")")]
        parts = [p.strip() for p in body.replace("/", ",").split(",") if p.strip()]
        try:
            values = [float(p[:-1]) / 100.0 if p.endswith("%") else float(p) for p in parts[:4]]
        except ValueError:
            return default
        while len(values) < 3:
            values.append(1.0)
        alpha = values[3] if len(values) > 3 else 1.0
        return (values[0] / 255.0, values[1] / 255.0, values[2] / 255.0, alpha)
    return default


def _refresh_colors(theme, opacity):
    pal = THEMES.get(theme) or THEMES["dark"]
    colors = {}
    for key in TOKEN_KEYS:
        colors[key] = parse_color(pal.get(key))
    bg = pal["bg"]
    colors["bg"] = (bg[0] / 255.0, bg[1] / 255.0, bg[2] / 255.0, float(opacity))
    colors["bg_flat"] = (bg[0] / 255.0, bg[1] / 255.0, bg[2] / 255.0, 1.0)
    _state["theme"] = theme
    _state["opacity"] = opacity
    _state["colors"] = colors


def palette():
    if not _state["colors"]:
        _refresh_colors("dark", 0.82)
    return _state["colors"]


def color(key, alpha=None):
    """Cairo RGBA for a palette key, optionally overriding alpha."""
    value = palette().get(key, (1.0, 1.0, 1.0, 1.0))
    if alpha is None:
        return value
    return (value[0], value[1], value[2], max(0.0, min(1.0, float(alpha))))


def current_theme():
    return _state["theme"]


def tokens_css(theme, opacity):
    pal = THEMES.get(theme) or THEMES["dark"]
    alpha = max(0.40, min(1.0, float(opacity)))
    bg = pal["bg"]
    lines = ["@define-color lumen_bg rgba(%d, %d, %d, %.3f);"
             % (bg[0], bg[1], bg[2], alpha)]
    for key in TOKEN_KEYS:
        lines.append("@define-color lumen_%s %s;" % (key, pal.get(key, pal["text"])))
    return "\n".join(lines)


class Appearance:
    """Owns the two CSS providers (tokens + stylesheet) for one screen."""

    def __init__(self, screen: Gdk.Screen, theme: str = "dark", opacity: float = 0.82):
        self.screen = screen
        self.theme = theme if theme in THEMES else "dark"
        self.opacity = max(0.40, min(1.0, float(opacity)))
        self._token_provider = None
        self._style_provider = None
        self._load_stylesheet()
        self.apply(self.theme, self.opacity)

    # -- providers -------------------------------------------------------
    def _load_stylesheet(self):
        try:
            with open(STYLE_FILE, "r", encoding="utf-8") as handle:
                css = handle.read()
        except OSError:
            css = "window.lumen { background: transparent; }"
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(css.encode("utf-8"))
        except Exception as error:
            sys.stderr.write(
                "LUMEN: GTK could not parse style.css (%s); using fallback chrome.\n" % error)
            provider = Gtk.CssProvider()
            provider.load_from_data(b"window.lumen { background: transparent; }")
        Gtk.StyleContext.add_provider_for_screen(
            self.screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._style_provider = provider

    def apply(self, theme: str = None, opacity: float = None):
        if theme is not None:
            self.theme = theme if theme in THEMES else "dark"
        if opacity is not None:
            self.opacity = max(0.40, min(1.0, float(opacity)))
        _refresh_colors(self.theme, self.opacity)
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(tokens_css(self.theme, self.opacity).encode("utf-8"))
        except Exception:
            return
        if self._token_provider is not None:
            Gtk.StyleContext.remove_provider_for_screen(self.screen, self._token_provider)
        Gtk.StyleContext.add_provider_for_screen(
            self.screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self._token_provider = provider

    def toggle_theme(self) -> str:
        self.theme = "light" if self.theme == "dark" else "dark"
        self.apply(self.theme, self.opacity)
        return self.theme

    def set_opacity(self, value: float):
        self.apply(self.theme, value)

    @property
    def is_dark(self) -> bool:
        return self.theme == "dark"
