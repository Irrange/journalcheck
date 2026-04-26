from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


CANVAS = 256
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "assets"
PNG_PATH = OUTPUT_DIR / "journalcheck_icon.png"
ICO_PATH = OUTPUT_DIR / "journalcheck_icon.ico"


def _draw_shadow(base: Image.Image, box: tuple[int, int, int, int], radius: int, offset_y: int = 8) -> None:
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    x1, y1, x2, y2 = box
    shadow_draw.rounded_rectangle((x1, y1 + offset_y, x2, y2 + offset_y), radius=radius, fill=(0, 0, 0, 72))
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(12)))


def build_icon() -> Image.Image:
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((18, 18, 238, 238), radius=56, fill=(17, 95, 90, 255))
    draw.ellipse((134, 12, 256, 126), fill=(48, 168, 146, 110))
    draw.ellipse((8, 154, 120, 262), fill=(9, 61, 59, 115))

    page_box = (62, 38, 194, 214)
    _draw_shadow(image, page_box, radius=28)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(page_box, radius=28, fill=(250, 251, 246, 255))
    draw.polygon([(157, 38), (194, 38), (194, 75)], fill=(231, 236, 229, 255))
    draw.line([(157, 38), (194, 75)], fill=(200, 210, 200, 255), width=3)

    line_color = (170, 182, 176, 255)
    for y in (82, 104, 126):
        draw.rounded_rectangle((86, y, 170, y + 6), radius=3, fill=line_color)

    draw.line((94, 150, 116, 173), fill=(21, 150, 111, 255), width=16, joint="curve")
    draw.line((116, 173, 167, 116), fill=(21, 150, 111, 255), width=16, joint="curve")

    draw.ellipse((166, 166, 214, 214), fill=(255, 189, 89, 255))
    draw.ellipse((176, 176, 204, 204), fill=(255, 214, 140, 255))
    return image


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    image = build_icon()
    image.save(PNG_PATH)
    image.save(ICO_PATH, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Saved {PNG_PATH}")
    print(f"Saved {ICO_PATH}")


if __name__ == "__main__":
    main()
