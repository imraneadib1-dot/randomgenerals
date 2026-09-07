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
import sys

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
