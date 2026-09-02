# -*- coding: utf-8 -*-
"""Render the app icons for the installable app, from the logo geometry.

    python tools/make_icons.py

Drawn rather than exported so the icons stay in step with static/logo.svg
without a design tool in the loop - the three circles and their tangency
rule are the same ones written down in that file.

Two families are produced:

  icon-<n>.png       the mark on its tile, edge to edge
  maskable-<n>.png   the same mark inside a safe zone

Both are needed, and they are not interchangeable. Android crops a
maskable icon to whatever shape the launcher uses - a circle, a squircle,
a rounded square - so anything within about 10% of the edge can be cut
off. A single icon used for both either floats too small on iOS or loses
its outer ring on Android.
"""
import os

from PIL import Image, ImageDraw

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "static", "icons")

# From static/favicon.svg. Cream ground, bronze mark.
GROUND = (242, 236, 228, 255)
MARK = (110, 100, 32, 255)

# The logo, expressed as fractions of the mark's own box, so it scales to
# any size. Internal tangency: the distance between two centres is the
# DIFFERENCE of their radii, which is what makes the gap sweep round into
# a coil instead of stacking into a crescent.
#   outer  r 88  at cy 100
#   middle r 57, touching the outer at the TOP    -> cy 69
#   inner  r 33, touching the middle at the BOTTOM -> cy 93
CIRCLES = [(100 / 200.0, 88 / 200.0),
           (69 / 200.0, 57 / 200.0),
           (93 / 200.0, 33 / 200.0)]
STROKE_FRACTION = 6.5 / 200.0


def draw_icon(size, inset_fraction, radius_fraction):
    """One icon. `inset_fraction` is the empty margin around the mark."""
    # 4x supersampling: PIL has no antialiased stroke, and a thin ring
    # drawn at 192px directly comes out visibly stepped.
    scale = 4
    px = size * scale
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    corner = int(px * radius_fraction)
    draw.rounded_rectangle([0, 0, px - 1, px - 1], radius=corner, fill=GROUND)

    box = px * (1 - 2 * inset_fraction)
    origin = px * inset_fraction
    width = max(1, int(box * STROKE_FRACTION))

    for cy_frac, r_frac in CIRCLES:
        cx = origin + box / 2.0
        cy = origin + box * cy_frac
        r = box * r_frac
        draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                     outline=MARK, width=width)

    return img.resize((size, size), Image.LANCZOS)


def main():
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
