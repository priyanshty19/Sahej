#!/usr/bin/env python3
"""
Generate Sahej PWA icons as PNGs — pure stdlib (zlib + struct), no Pillow.

Run:  python3 tools/make_icons.py
Writes web/icon-512.png, web/icon-192.png, web/apple-touch-icon.png (180),
web/favicon-32.png.

Design: full-bleed teal square (maskable-safe) with the Sahej motif — a visit
timeline: three dots on a vertical rail, each with a checklist bar; the middle
dot is gold (the deadline).
"""
import os
import struct
import zlib

WEB = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web"))

TEAL = (15, 123, 108)
WHITE = (250, 250, 248)
GOLD = (222, 168, 62)


def _chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png(path, w, h, pixels):
    """pixels: list of rows, each row a bytearray of RGB triples."""
    raw = b"".join(b"\x00" + bytes(row) for row in pixels)
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw, 9))
           + _chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def _capsule_cov(px, py, x0, y0, x1, y1, r):
    """Anti-aliased coverage of point (px,py) by a capsule (segment + radius)."""
    dx, dy = x1 - x0, y1 - y0
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / seg2))
    cx, cy = x0 + t * dx, y0 + t * dy
    dist = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
    return max(0.0, min(1.0, r + 0.5 - dist))


def make_icon(size, path):
    S = float(size)
    rail_x = 0.30 * S
    rows_y = [0.28 * S, 0.50 * S, 0.72 * S]
    dot_r = 0.062 * S
    bar_r = 0.030 * S
    bar_x0, bar_x1 = rail_x + 0.14 * S, 0.76 * S
    rail = (rail_x, rows_y[0], rail_x, rows_y[2], 0.020 * S)

    shapes = [(*rail, WHITE)]
    for i, y in enumerate(rows_y):
        color = GOLD if i == 1 else WHITE
        shapes.append((rail_x, y, rail_x, y, dot_r, color))          # dot
        shapes.append((bar_x0, y, bar_x1, y, bar_r, WHITE))          # bar

    pixels = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r, g, b = TEAL
            for (x0, y0, x1, y1, rad, col) in shapes:
                cov = _capsule_cov(px + 0.5, py + 0.5, x0, y0, x1, y1, rad)
                if cov > 0:
                    r = int(r + (col[0] - r) * cov)
                    g = int(g + (col[1] - g) * cov)
                    b = int(b + (col[2] - b) * cov)
            row += bytes((r, g, b))
        pixels.append(row)
    write_png(path, size, size, pixels)
    print(f"  wrote {os.path.relpath(path)}")


if __name__ == "__main__":
    make_icon(512, os.path.join(WEB, "icon-512.png"))
    make_icon(192, os.path.join(WEB, "icon-192.png"))
    make_icon(180, os.path.join(WEB, "apple-touch-icon.png"))
    make_icon(32, os.path.join(WEB, "favicon-32.png"))
    print("done.")
