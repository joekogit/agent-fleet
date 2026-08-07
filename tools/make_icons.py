#!/usr/bin/env python3
"""Rasterize the Agent Fleet mark to PNG at the sizes Chrome needs for a Dock icon.

Chrome builds an installed app's icon from the web app manifest's PNG icons, not
from an SVG favicon — so the mark has to exist as real bitmaps or the Dock falls
back to a generic tile.

Geometry here mirrors `fleet/static/icon.svg` and the inline `.brand-mark` in
`index.html` exactly, in the same 32-unit space. If you change one, change all
three and re-run this script.

Stdlib only, like the rest of the project: shapes are signed-distance fields,
antialiased by converting distance to coverage, and the PNG is written by hand
with zlib.

    python3 tools/make_icons.py
"""
import math
import os
import struct
import zlib

VIEWBOX = 32.0
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fleet", "static")
SIZES = (192, 512, 1024)

BG = (0x0F, 0x12, 0x16)
BORDER = (0x2A, 0x30, 0x3A)
AMBER = (0xFF, 0xB0, 0x20)
GREEN = (0x3D, 0xD6, 0x8C)
GREY = (0x5A, 0x64, 0x73)


def rounded_rect_sdf(px, py, cx, cy, hw, hh, r):
    qx = abs(px - cx) - (hw - r)
    qy = abs(py - cy) - (hh - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - r


def circle_sdf(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r


# (kind, geometry, colour, alpha, bounding box in icon units)
def _rrect(cx, cy, hw, hh, r, colour, alpha):
    pad = 1.0
    bbox = (cx - hw - pad, cy - hh - pad, cx + hw + pad, cy + hh + pad)
    return ("rrect", (cx, cy, hw, hh, r), colour, alpha, bbox)


def _circle(cx, cy, r, colour, alpha):
    pad = 1.0
    bbox = (cx - r - pad, cy - r - pad, cx + r + pad, cy + r + pad)
    return ("circle", (cx, cy, r), colour, alpha, bbox)


# Painted in order. The border is a ring: the tile's outer edge minus its inset.
SHAPES = [
    _rrect(16, 16, 16, 16, 7, BG, 1.0),
    ("ring", ((16, 16, 16, 16, 7), (16, 16, 15, 15, 6)), BORDER, 1.0, (0, 0, 32, 32)),
    _circle(9, 9.5, 2.6, AMBER, 1.0),
    _rrect(19.5, 9.5, 5.5, 1.3, 1.3, AMBER, 0.55),
    _circle(9, 16.5, 2.6, GREEN, 1.0),
    _rrect(18.0, 16.5, 4.0, 1.3, 1.3, GREEN, 0.45),
    _circle(9, 23.5, 2.6, GREY, 1.0),
    _rrect(16.5, 23.5, 2.5, 1.3, 1.3, GREY, 0.45),
]


def coverage(distance, px_units):
    """Signed distance -> antialiased coverage, one sample per pixel."""
    return min(1.0, max(0.0, 0.5 - distance / px_units))


def render(size):
    px_units = VIEWBOX / size           # one output pixel, in icon units
    half = px_units * 0.5
    out = bytearray()

    for py in range(size):
        y = (py + 0.5) * px_units
        row = bytearray()
        for px in range(size):
            x = (px + 0.5) * px_units
            r = g = b = 0.0
            a = 0.0

            for kind, geom, colour, alpha, bbox in SHAPES:
                if not (bbox[0] - half <= x <= bbox[2] + half and bbox[1] - half <= y <= bbox[3] + half):
                    continue

                if kind == "rrect":
                    cov = coverage(rounded_rect_sdf(x, y, *geom), px_units)
                elif kind == "circle":
                    cov = coverage(circle_sdf(x, y, *geom), px_units)
                else:  # ring
                    outer, inner = geom
                    cov = coverage(rounded_rect_sdf(x, y, *outer), px_units) - coverage(
                        rounded_rect_sdf(x, y, *inner), px_units
                    )
                    cov = max(0.0, cov)

                src_a = cov * alpha
                if src_a <= 0.0:
                    continue

                sr, sg, sb = (c / 255.0 for c in colour)
                # Straight-alpha "over", accumulating premultiplied then un-premultiplying at the end.
                r = sr * src_a + r * (1.0 - src_a)
                g = sg * src_a + g * (1.0 - src_a)
                b = sb * src_a + b * (1.0 - src_a)
                a = src_a + a * (1.0 - src_a)

            if a > 0.0:
                # r/g/b above are already composited against transparent black, so
                # divide out the alpha to recover straight colour for the PNG.
                row += bytes((
                    min(255, int(r / a * 255.0 + 0.5)),
                    min(255, int(g / a * 255.0 + 0.5)),
                    min(255, int(b / a * 255.0 + 0.5)),
                    min(255, int(a * 255.0 + 0.5)),
                ))
            else:
                row += b"\x00\x00\x00\x00"

        out += b"\x00" + row        # PNG filter type 0 for each scanline
    return bytes(out)


def write_png(path, size, raw):
    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)   # 8-bit RGBA
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as fh:
        fh.write(png)
    return len(png)


def main():
    for size in SIZES:
        path = os.path.join(OUT_DIR, "icon-%d.png" % size)
        written = write_png(path, size, render(size))
        print("wrote %s (%d bytes)" % (path, written))


if __name__ == "__main__":
    main()
