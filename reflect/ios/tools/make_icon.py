"""Draws Reflect's 1024x1024 app icon (a phone mirroring a desktop window). Needs Pillow.

    python3 tools/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
SS = 4  # supersampling for smooth edges
OUT = Path(__file__).resolve().parent.parent / "Reflect/Assets.xcassets/AppIcon.appiconset/AppIcon.png"

BACKGROUND_TOP = (38, 34, 32)
BACKGROUND_BOTTOM = (18, 17, 16)
ACCENT = (217, 119, 87)
PAPER = (244, 238, 230)


def main() -> None:
    s = SIZE * SS
    image = Image.new("RGB", (s, s))
    draw = ImageDraw.Draw(image)
    for y in range(s):
        t = y / (s - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(BACKGROUND_TOP, BACKGROUND_BOTTOM))
        draw.line([(0, y), (s, y)], fill=color)

    def box(x0, y0, x1, y1):
        return [v * SS for v in (x0, y0, x1, y1)]

    # Desktop window behind, offset up-left.
    draw.rounded_rectangle(box(150, 230, 700, 690), radius=40 * SS, fill=(62, 57, 53))
    draw.rounded_rectangle(box(150, 230, 700, 300), radius=40 * SS, fill=(80, 74, 69))
    draw.rectangle(box(150, 270, 700, 300), fill=(80, 74, 69))
    for i, x in enumerate((190, 225, 260)):
        draw.ellipse(box(x, 255, x + 22, 277), fill=(120, 112, 105))

    # Glow + phone in front.
    glow = Image.new("L", (s, s), 0)
    ImageDraw.Draw(glow).rounded_rectangle(box(470, 250, 820, 890), radius=70 * SS, fill=150)
    glow = glow.filter(ImageFilter.GaussianBlur(40 * SS))
    image.paste(Image.new("RGB", (s, s), ACCENT), (0, 0), glow)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(box(480, 240, 810, 880), radius=64 * SS, fill=ACCENT)
    draw.rounded_rectangle(box(504, 264, 786, 856), radius=44 * SS, fill=PAPER)
    draw.rounded_rectangle(box(600, 282, 690, 302), radius=10 * SS, fill=ACCENT)
    # Chat lines on the phone screen.
    for y, x1 in ((360, 740), (410, 700), (460, 750), (560, 720), (610, 690)):
        draw.rounded_rectangle(box(536, y, x1, y + 22), radius=11 * SS, fill=(205, 196, 186))
    draw.rounded_rectangle(box(536, 770, 754, 822), radius=26 * SS, fill=(225, 216, 206))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.resize((SIZE, SIZE), Image.LANCZOS).save(OUT, optimize=True)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
