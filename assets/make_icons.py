"""Draws the ADB File Transfer icon in a dark and a light version.

Idea: the app's header in one glyph. A dot (this PC), an amber link flowing
towards a phone. Small sizes get a bolder, simpler drawing so they stay legible.

Usage: python assets/make_icons.py   (needs Pillow)
Writes icon-dark/light .ico + .png next to this file.
"""
import base64
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
M = 1024  # master size

THEMES = {
    "dark": dict(tile="#151619", edge="#2A2C31", phone="#ECEDEF", screen="#1B1C20", link="#E9A23B"),
    "light": dict(tile="#F4F4F5", edge="#D6D7DB", phone="#17181A", screen="#FFFFFF", link="#D98A1E"),
}


def draw(theme, bold=False, size=M):
    c = THEMES[theme]
    k = 4  # supersample then shrink for clean edges
    S = size * k
    u = S / M
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([32 * u, 32 * u, 992 * u, 992 * u], radius=216 * u, fill=c["tile"],
                        outline=c["edge"], width=max(1, int(8 * u)))

    # glyph on its own layer so it can be scaled to fill the tile
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g = ImageDraw.Draw(layer)
    cy = 512
    stroke = 60 if bold else 40
    px1, px2, py1, py2 = (600, 836, 276, 748) if bold else (612, 820, 300, 724)
    g.rounded_rectangle([px1 * u, py1 * u, px2 * u, py2 * u], radius=(64 if bold else 56) * u,
                        fill=c["screen"], outline=c["phone"], width=int(stroke * u))
    if not bold:
        g.line([(692 * u, 372 * u), (740 * u, 372 * u)], fill=c["phone"], width=int(14 * u))

    def dot(x, r):
        g.ellipse([(x - r) * u, (cy - r) * u, (x + r) * u, (cy + r) * u], fill=c["link"])

    def dash(x1, x2, w):
        g.line([(x1 * u, cy * u), (x2 * u, cy * u)], fill=c["link"], width=int(w * u))
        dot(x1, w / 2)
        dot(x2, w / 2)

    if bold:
        dot(190, 68)
        dash(318, 440, 64)
    else:
        dot(224, 48)
        dash(334, 408, 40)
        dash(470, 540, 40)

    f = 1.12 if bold else 1.2
    n = int(S * f)
    big = layer.resize((n, n), Image.LANCZOS)
    off = (n - S) // 2
    img.alpha_composite(big.crop((off, off, off + S, off + S)))
    return img.resize((size, size), Image.LANCZOS)


def build():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    for theme in THEMES:
        frames = {s: draw(theme, bold=s <= 48, size=s) for s in sizes}
        master = draw(theme, size=256)
        master.save(os.path.join(HERE, f"icon-{theme}.ico"), format="ICO",
                    sizes=[(s, s) for s in sizes],
                    append_images=[frames[s] for s in sizes if s != 256])
        draw(theme, size=512).save(os.path.join(HERE, f"icon-{theme}.png"))
        draw(theme, size=64).save(os.path.join(HERE, f"_{theme}64.png"), optimize=True)
        with open(os.path.join(HERE, f"_{theme}64.png"), "rb") as f:
            print(theme, "64px base64 bytes:", len(base64.b64encode(f.read())))


if __name__ == "__main__":
    build()
