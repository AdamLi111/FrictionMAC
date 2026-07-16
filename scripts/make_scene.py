"""
Generate scripted test scenes for stub-mode perception tests.

Draws simple but clearly-recognizable mugs (body + handle + rim) with a caption, so a real
VLM (the World-Understanding expert) genuinely perceives the scene when the stub serves
these frames via ROBOT_STUB_SCENE.

Scene `two_mugs`: front = a red mug, right = a blue mug, left/back = empty (no mug).
=> two distinct candidates for "the mug" => genuine ambiguity.
"""
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 640, 480
OUT = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "scene_two_mugs")


def _font(size):
    for path in [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _room(bg=(232, 228, 220)):
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    # simple table surface
    d.rectangle([0, 330, W, H], fill=(170, 140, 100))
    d.line([0, 330, W, 330], fill=(120, 95, 60), width=4)
    return img, d


def _draw_mug(d, cx, color, name):
    # body
    body = [cx - 70, 200, cx + 50, 330]
    d.rounded_rectangle(body, radius=16, fill=color, outline=(30, 30, 30), width=4)
    # rim (top ellipse)
    d.ellipse([cx - 70, 188, cx + 50, 216], fill=tuple(min(255, c + 30) for c in color),
              outline=(30, 30, 30), width=4)
    # handle
    d.arc([cx + 40, 225, cx + 95, 300], start=300, end=60, fill=(30, 30, 30), width=10)
    # label
    d.text((cx - 70, 340), name, fill=(20, 20, 20), font=_font(22))


def make():
    os.makedirs(OUT, exist_ok=True)

    # front: red mug
    img, d = _room()
    _draw_mug(d, 300, (200, 55, 45), "red ceramic coffee mug")
    d.text((16, 12), "Front view", fill=(60, 60, 60), font=_font(20))
    img.save(os.path.join(OUT, "front.jpg"), "JPEG", quality=90)

    # right: blue mug (clearly a different mug)
    img, d = _room()
    _draw_mug(d, 320, (45, 85, 200), "blue ceramic coffee mug")
    d.text((16, 12), "Right view", fill=(60, 60, 60), font=_font(20))
    img.save(os.path.join(OUT, "right.jpg"), "JPEG", quality=90)

    # left/back: empty (no mug)
    for name, view in [("left.jpg", "Left view"), ("back.jpg", "Back view")]:
        img, d = _room()
        d.text((16, 12), view, fill=(60, 60, 60), font=_font(20))
        d.text((180, 250), "(empty wall, no mug)", fill=(120, 120, 120), font=_font(22))
        img.save(os.path.join(OUT, name), "JPEG", quality=90)

    print("wrote scene to", os.path.abspath(OUT))
    for f in sorted(os.listdir(OUT)):
        print("  ", f, os.path.getsize(os.path.join(OUT, f)), "bytes")


if __name__ == "__main__":
    make()
