#!/usr/bin/env python3
"""Generate Tauri app icons from the bookshelf logo source image."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS_SOURCE = ROOT / "frontend" / "src" / "assets" / "app-icon-source.png"
DOWNLOADS_SOURCE = (
    Path.home() / "Downloads" / "Gemini_Generated_Image_n16zjrn16zjrn16z.png"
)
CURSOR_SOURCE = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Cursor"
    / "User"
    / "workspaceStorage"
    / "empty-window"
    / "images"
    / "Gemini_Generated_Image_n16zjrn16zjrn16z-e58a03a8-4b24-4cbb-b3dc-43fd4ad84ae1.png"
)
OUTPUT_DIR = ROOT / "frontend" / "src-tauri" / "icons"
CORNER_RADIUS_RATIO = 0.22

PNG_OUTPUTS: dict[str, int] = {
    "icon.png": 512,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "32x32.png": 32,
    "Square30x30Logo.png": 30,
    "Square44x44Logo.png": 44,
    "Square71x71Logo.png": 71,
    "Square89x89Logo.png": 89,
    "Square107x107Logo.png": 107,
    "Square142x142Logo.png": 142,
    "Square150x150Logo.png": 150,
    "Square284x284Logo.png": 284,
    "Square310x310Logo.png": 310,
    "StoreLogo.png": 50,
}

ICNS_ICONSET: dict[str, int] = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def resolve_source() -> tuple[Path, str]:
    if ASSETS_SOURCE.exists():
        return ASSETS_SOURCE, "frontend/src/assets/app-icon-source.png"
    if DOWNLOADS_SOURCE.exists():
        ASSETS_SOURCE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DOWNLOADS_SOURCE, ASSETS_SOURCE)
        return ASSETS_SOURCE, "Downloads/Gemini_Generated_Image_n16zjrn16zjrn16z.png"
    if CURSOR_SOURCE.exists():
        return CURSOR_SOURCE, "Cursor workspaceStorage image"
    raise FileNotFoundError(
        "No source image found. Expected one of:\n"
        f"  - {ASSETS_SOURCE}\n"
        f"  - {DOWNLOADS_SOURCE}\n"
        f"  - {CURSOR_SOURCE}"
    )


def center_crop_square(image: Image.Image) -> Image.Image:
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side))


def apply_rounded_corners(image: Image.Image, radius_ratio: float) -> Image.Image:
    rgba = image.convert("RGBA")
    side = rgba.size[0]
    radius = max(1, round(side * radius_ratio))

    mask = Image.new("L", (side, side), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, side - 1, side - 1), radius=radius, fill=255)

    rounded = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    rounded.paste(rgba, (0, 0), mask)
    return rounded


def resize_icon(base: Image.Image, size: int) -> Image.Image:
    return base.resize((size, size), Image.Resampling.LANCZOS)


def write_png(path: Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)


def generate_icns(base: Image.Image, output_path: Path) -> None:
    if shutil.which("iconutil") is None:
        raise RuntimeError("iconutil not found; required to build icon.icns on macOS")

    with tempfile.TemporaryDirectory(prefix="app-iconset-") as tmp_dir:
        iconset_dir = Path(tmp_dir) / "AppIcon.iconset"
        iconset_dir.mkdir()

        for filename, size in ICNS_ICONSET.items():
            write_png(iconset_dir / filename, resize_icon(base, size))

        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(output_path)],
            check=True,
        )


def generate_ico(base: Image.Image, output_path: Path) -> None:
    largest = max(ICO_SIZES)
    resize_icon(base, largest).save(
        output_path,
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
    )


def main() -> int:
    source_path, source_label = resolve_source()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with Image.open(source_path) as opened:
        square = center_crop_square(opened)
        base = apply_rounded_corners(square, CORNER_RADIUS_RATIO)

    written: list[Path] = []

    for filename, size in PNG_OUTPUTS.items():
        out = OUTPUT_DIR / filename
        write_png(out, resize_icon(base, size))
        written.append(out)

    icns_path = OUTPUT_DIR / "icon.icns"
    generate_icns(base, icns_path)
    written.append(icns_path)

    ico_path = OUTPUT_DIR / "icon.ico"
    generate_ico(base, ico_path)
    written.append(ico_path)

    print(f"Source: {source_label}")
    print(f"Source path: {source_path}")
    print(f"Output directory: {OUTPUT_DIR}")
    for path in written:
        print(f"  - {path.relative_to(ROOT)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
