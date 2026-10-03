"""Generate synthetic demo media and DOM fixtures without MongoDB or API access."""
import argparse
from io import BytesIO
import importlib.util
import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lookup", ROOT / "emoticon_lookup.py")
lookup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lookup)


def build(fixtures=False):
    directory = ROOT / "tests/fixtures" if fixtures else ROOT / "generated/demo"
    directory.mkdir(parents=True, exist_ok=True)
    frames = []
    for step in range(20):
        image = Image.new("RGB", (240, 120), "#f1f5f1")
        draw = ImageDraw.Draw(image)
        for index, color in enumerate(("#167365", "#de9b55", "#607db6")):
            x = 60 + index * 60
            y = 60 + round(math.sin(2 * math.pi * step / 20 + index) * 25)
            draw.ellipse((x - 15, y - 15, x + 15, y + 15), fill=color)
        frames.append(image)
    data = BytesIO()
    frames[0].save(data, format="GIF", save_all=True, append_images=frames[1:], duration=65, loop=0)
    media, preview = directory / "animation.gif", directory / "preview.png"
    media.write_bytes(data.getvalue())
    preview.write_bytes(lookup.thumbnail(data.getvalue())[0])
    row = dict(slug="demo_animation", count=1, status="ok", animated=True, demo=True,
               url=None, preview=str(preview), media=str(media))
    if fixtures:
        rows = [dict(slug="etextrary", count=1, status="missing", detail="No exact API match"),
                row | {"slug": "etextraryan", "count": 2}, row]
        target = directory / "gallery.html"
    else:
        rows = [row]
        target = ROOT / "examples/animation_demo.html"
    lookup.write_html(target, rows, "Animation demo", "rndmzd", sample=True)
    print(f"Created {target.relative_to(ROOT)} using synthetic media only.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", action="store_true", help="Build ignored test fixtures instead of the example gallery")
    build(parser.parse_args().fixtures)
