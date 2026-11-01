#!/usr/bin/env python3
"""LUMEN logo generator - pure Cairo, no external image assets.

The mark is a minimal aperture ring holding a sun over two mountain peaks,
filled with the brand gradient (#7C3AED -> #F59E0B).

    python3 gen_icon.py                 # writes lumen.png (512x512)
    python3 gen_icon.py out.png 256     # custom path / size
"""

from __future__ import annotations

import math
import os
import sys

import cairo

BRAND_START = (0x7C / 255.0, 0x3A / 255.0, 0xED / 255.0)
BRAND_END = (0xF5 / 255.0, 0x9E / 255.0, 0x0B / 255.0)


def rounded_rect(cr, x, y, width, height, radius):
    """Append a rounded rectangle to the current path."""
    radius = min(radius, width / 2.0, height / 2.0)
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


def brand_gradient(cr, size, alpha=1.0):
    gradient = cairo.LinearGradient(0, 0, size, size)
    gradient.add_color_stop_rgba(0.0, BRAND_START[0], BRAND_START[1], BRAND_START[2], alpha)
    gradient.add_color_stop_rgba(0.55, 0.55, 0.30, 0.85, alpha)
    gradient.add_color_stop_rgba(1.0, BRAND_END[0], BRAND_END[1], BRAND_END[2], alpha)
    return gradient


def draw_logo(cr, size, opaque=False):
    """Paint the LUMEN aperture mark into the current context.

    The mark fills a circle of radius ``size * 0.47`` centred in the box.
    """
    cx = cy = size / 2.0
    radius = size * 0.47
    cr.save()

    # soft halo so the mark reads on dark and light backgrounds
    if opaque:
        halo = cairo.RadialGradient(cx, cy, radius * 0.2, cx, cy, radius)
        halo.add_color_stop_rgba(0, BRAND_START[0], BRAND_START[1], BRAND_START[2], 0.10)
        halo.add_color_stop_rgba(1, 0, 0, 0, 0.0)
        cr.set_source(halo)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.fill()

    # the disc
    cr.arc(cx, cy, radius, 0, 2 * math.pi)
    cr.clip()
    cr.set_source(brand_gradient(cr, size))
    cr.paint()

    # aperture blades: six translucent wedges around the rim
    cr.set_line_width(size * 0.012)
    cr.set_source_rgba(1, 1, 1, 0.22)
    for index in range(6):
        angle = index * math.pi / 3.0 + math.radians(18)
        cr.move_to(cx + math.cos(angle) * radius * 0.52,
                   cy + math.sin(angle) * radius * 0.52)
        cr.line_to(cx + math.cos(angle) * radius * 1.05,
                   cy + math.sin(angle) * radius * 1.05)
        cr.stroke()

    # horizon glow
    glow = cairo.LinearGradient(0, cy - radius * 0.1, 0, cy + radius)
    glow.add_color_stop_rgba(0, 0.08, 0.04, 0.18, 0.0)
    glow.add_color_stop_rgba(1, 0.08, 0.04, 0.18, 0.55)
    cr.set_source(glow)
    cr.rectangle(cx - radius, cy - radius * 0.1, radius * 2, radius * 1.2)
    cr.fill()

    # sun
    sun_x = cx + radius * 0.30
    sun_y = cy - radius * 0.34
    sun_r = radius * 0.20
    sun_glow = cairo.RadialGradient(sun_x, sun_y, sun_r * 0.4, sun_x, sun_y, sun_r * 2.4)
    sun_glow.add_color_stop_rgba(0, 1, 1, 1, 0.65)
    sun_glow.add_color_stop_rgba(1, 1, 1, 1, 0.0)
    cr.set_source(sun_glow)
    cr.arc(sun_x, sun_y, sun_r * 2.4, 0, 2 * math.pi)
    cr.fill()
    cr.set_source_rgba(1, 0.98, 0.92, 0.96)
    cr.arc(sun_x, sun_y, sun_r, 0, 2 * math.pi)
    cr.fill()

    # mountains (two overlapping peaks, negative space)
    cr.set_source_rgba(1, 1, 1, 0.92)
    cr.move_to(cx - radius * 0.92, cy + radius * 0.72)
    cr.line_to(cx - radius * 0.34, cy + radius * 0.06)
    cr.line_to(cx + radius * 0.04, cy + radius * 0.52)
    cr.line_to(cx + radius * 0.34, cy + radius * 0.20)
    cr.line_to(cx + radius * 0.92, cy + radius * 0.72)
    cr.close_path()
    cr.fill()

    cr.set_source_rgba(0.10, 0.06, 0.22, 0.35)
    cr.move_to(cx - radius * 0.34, cy + radius * 0.06)
    cr.line_to(cx - radius * 0.10, cy + radius * 0.34)
    cr.line_to(cx - radius * 0.34, cy + radius * 0.52)
    cr.close_path()
    cr.fill()

    # aperture ring
    cr.set_line_width(max(1.0, size * 0.035))
    ring = cairo.LinearGradient(0, 0, size, size)
    ring.add_color_stop_rgba(0, 1, 1, 1, 0.85)
    ring.add_color_stop_rgba(1, 1, 1, 1, 0.35)
    cr.set_source(ring)
    cr.arc(cx, cy, radius - max(1.0, size * 0.018), 0, 2 * math.pi)
    cr.stroke()

    cr.restore()


def draw_logo_badge(cr, size):
    """Circular chip with a soft cream plate - handy for header/brand use."""
    cr.save()
    cr.arc(size / 2.0, size / 2.0, size * 0.46, 0, 2 * math.pi)
    cr.set_source_rgba(1, 1, 1, 0.10)
    cr.fill()
    cr.restore()
    draw_logo(cr, size)


def render(size: int = 512):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    cr.set_antialias(cairo.ANTIALIAS_BEST)
    draw_logo(cr, size, opaque=True)
    cr.set_source_rgba(1, 1, 1, 0.0)
    return surface


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    output = "lumen.png"
    size = 512
    positional = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        if arg in ("-o", "--output") and index + 1 < len(argv):
            output = argv[index + 1]
            index += 2
            continue
        if arg in ("-s", "--size") and index + 1 < len(argv):
            size = int(argv[index + 1])
            index += 2
            continue
        positional.append(arg)
        index += 1

    if positional:
        output = positional[0]
    if len(positional) > 1:
        size = int(positional[1])

    size = max(16, min(2048, int(size)))
    surface = render(size)
    folder = os.path.dirname(os.path.abspath(output))
    if folder:
        os.makedirs(folder, exist_ok=True)
    surface.write_to_png(output)
    print("LUMEN icon written: %s (%dx%d)" % (output, size, size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
