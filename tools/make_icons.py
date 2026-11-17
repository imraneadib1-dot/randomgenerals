# -*- coding: utf-8 -*-
"""Render the app icons for the installable app, from the logo geometry.

    python tools/make_icons.py

Drawn rather than exported so the icons stay in step with static/logo.svg
without a design tool in the loop - the crescent's two arcs are the same
ones written down in that file.

Two families are produced:

  icon-<n>.png       the mark on its tile, edge to edge
  maskable-<n>.png   the same mark inside a safe zone

Both are needed, and they are not interchangeable. Android crops a
maskable icon to whatever shape the launcher uses - a circle, a squircle,
a rounded square - so anything within about 10% of the edge can be cut
off. A single icon used for both either floats too small on iOS or loses
its outer ring on Android.
"""
import math
import os
import sys

from PIL import Image, ImageDraw

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "static", "icons")

# From static/favicon.svg: the original RandomGenerals mark - a pink-red
# crescent with a navy shadow, on a periwinkle tile.
GROUND = (112, 120, 242, 255)      # #7078f2
MARK = (255, 31, 90, 255)          # #ff1f5a
SHADOW = (59, 69, 148, 255)        # #3b4594

# The crescent, in the 200-unit box static/logo.svg uses. It is the
# region inside the OUTER circle and outside the INNER one, and the line
# is drawn as that region minus a hollow (the same two circles, outer
# shrunk and inner grown by the line width) - so the horns come to sharp
# points exactly where the circles cross, as in the original logo, and
# the shadow shows through the hollow.
OUTER = (72.5, 100.0, 73.5)       # centre x, centre y, radius
INNER = (42.3, 100.0, 73.7)
LINE = 9.0                        # line width, same units
SHADOW_OFFSET = (7.0, 8.0)


def _crossing(c1, c2):
    """Where two circles centred on the same horizontal line cross:
    -> (x, half height)."""
    (x1, _, r1), (x2, _, r2) = c1, c2
    d = x1 - x2
    a = (d * d + r2 * r2 - r1 * r1) / (2 * d)
    return x2 + a, math.sqrt(max(0.0, r2 * r2 - a * a))


def _side(c, x, h, steps, reverse=False):
    """Points on circle c from (x, cy-h) round its right side to (x, cy+h)."""
    cx, cy, r = c
    a = math.atan2(h, x - cx)
    pts = [(cx + r * math.cos(t), cy + r * math.sin(t))
           for t in (-a + 2 * a * i / steps for i in range(steps + 1))]
    return list(reversed(pts)) if reverse else pts


def crescent(outer, inner, steps=120):
    """Closed outline of the region inside `outer` and outside `inner`."""
    x, h = _crossing(outer, inner)
    return _side(outer, x, h, steps) + _side(inner, x, h, steps, reverse=True)


def crescent_shapes(line=LINE):
    """(filled crescent, hollow) outlines for a given line width."""
    ox, oy, orad = OUTER
    ix, iy, irad = INNER
    return (crescent(OUTER, INNER),
            crescent((ox, oy, orad - line), (ix, iy, irad + line)))


def svg_path(line=LINE):
    """The same shape as an SVG path (evenodd: crescent, then hollow)."""
    out = []
    for pts in crescent_shapes(line):
        out.append("M" + " L".join("%.2f %.2f" % p for p in pts[::4] + [pts[-1]]) + "Z")
    return " ".join(out)


def draw_icon(size, inset_fraction, radius_fraction):
    """One icon. `inset_fraction` is the empty margin around the mark."""
    scale = 4                      # supersampled, then downsized smoothly
    px = size * scale
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    corner = int(px * radius_fraction)
    draw.rounded_rectangle([0, 0, px - 1, px - 1], radius=corner, fill=GROUND)

    box = px * (1 - 2 * inset_fraction)
    origin = px * inset_fraction
    unit = box / 200.0
    # A heavier line at 16-48px so the crescent survives being tiny.
    line = LINE * (1.45 if size <= 48 else 1.0)
    body, hollow = crescent_shapes(line)
    for (dx, dy), colour in ((SHADOW_OFFSET, SHADOW), ((0.0, 0.0), MARK)):
        layer = Image.new("RGBA", (px, px), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        place = lambda pts: [(origin + (x + dx) * unit, origin + (y + dy) * unit) for x, y in pts]
        ld.polygon(place(body), fill=colour)
        ld.polygon(place(hollow), fill=(0, 0, 0, 0))      # cut the hollow out
        img.alpha_composite(layer)

    return img.resize((size, size), Image.LANCZOS)


IOS_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "mobile", "ios-icons")


def make_ios():
    """The App Store icon set.

    1024 is the one Xcode actually requires now - a single source image
    it downsamples from. The rest are emitted anyway because older
    project templates still expect a filled asset catalogue, and a
    missing size is a build error rather than a warning.

    NO TRANSPARENCY and NO ROUNDED CORNERS: App Store Connect rejects an
    icon with an alpha channel, and iOS applies its own corner radius -
    supplying one produces a visibly double-rounded icon.
    """
    os.makedirs(IOS_DIR, exist_ok=True)
    made = []
    for size in (1024, 180, 167, 152, 120, 87, 80, 76, 60, 58, 40, 29, 20):
        img = draw_icon(size, inset_fraction=0.10, radius_fraction=0.0)
        # Flatten onto the ground: an alpha channel is an automatic
        # rejection from App Store Connect.
        flat = Image.new("RGB", (size, size), GROUND[:3])
        flat.paste(img, (0, 0), img)
        path = os.path.join(IOS_DIR, "AppIcon-%d.png" % size)
        flat.save(path)
        made.append(path)
    return made


WIN_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "desktop", "resources")


def make_windows():
    """desktop/resources/icon.ico - the Windows app icon.

    THIS FILE DID NOT EXIST, and two places already pointed at it:
    BrowserWindow's `icon` option and electron-builder.yml. Neither
    errors when it is missing - Electron falls back to its own default -
    so the app shipped, ran, and showed a generic Electron logo in the
    title bar, the taskbar and Alt-Tab.

    ONE .ico HOLDS SEVERAL SIZES, and that is the whole point of the
    format. Windows picks from them by context: 16px in the title bar,
    32px in the taskbar, 48px in Explorer, 256px in the large-icon
    view. Supplying only a big one leaves Windows to downscale it
    itself, which on a thin ring like this mark turns into mush at
    16px.

    The small sizes get a tighter crop for the same reason - at 16px a
    7% margin is one pixel of nothing on each side, and the ring needs
    those pixels more than the padding.
    """
    os.makedirs(WIN_DIR, exist_ok=True)
    path = os.path.join(WIN_DIR, "icon.ico")
    sizes = [256, 128, 64, 48, 32, 24, 16]
    frames = []
    for size in sizes:
        inset = 0.03 if size <= 32 else 0.07
        radius = 0.0 if size <= 32 else 0.18
        frames.append(draw_icon(size, inset_fraction=inset,
                                radius_fraction=radius))
    # Pillow writes every size given in `sizes` into the one file, taking
    # the largest image as the source for each.
    frames[0].save(path, format="ICO",
                   sizes=[(s, s) for s in sorted(sizes)])
    return path


def main():
    if "--win" in sys.argv:
        path = make_windows()
        print("  %-38s %d bytes" % (os.path.relpath(path),
                                    os.path.getsize(path)))
        print("Windows icon written. Rebuild the desktop app to use it.")
        return

    if "--ios" in sys.argv:
        paths = make_ios()
        for path in paths:
            print("  %-38s %d bytes" % (os.path.relpath(path),
                                        os.path.getsize(path)))
        print("%d iOS icons written to %s"
              % (len(paths), os.path.relpath(IOS_DIR)))
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    made = []

    for size in (180, 192, 512):
        # A small inset so the outer ring never touches the tile edge.
        img = draw_icon(size, inset_fraction=0.07, radius_fraction=0.22)
        path = os.path.join(OUT_DIR, "icon-%d.png" % size)
        img.save(path)
        made.append(path)

    for size in (192, 512):
        # Android's safe zone is the middle ~80%, and it crops to the
        # launcher's own shape - so the tile is square here (the launcher
        # supplies the rounding) and the mark sits well inside it.
        img = draw_icon(size, inset_fraction=0.20, radius_fraction=0.0)
        path = os.path.join(OUT_DIR, "maskable-%d.png" % size)
        img.save(path)
        made.append(path)

    for path in made:
        print("  %-34s %d bytes" % (os.path.relpath(path), os.path.getsize(path)))
    print("%d icons written to %s" % (len(made), os.path.relpath(OUT_DIR)))


if __name__ == "__main__":
    main()
