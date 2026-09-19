"""Generate placeholder MSIX tile assets.

The Microsoft Store requires specific PNG sizes for the app tile/logo
at each of the paths referenced from AppxManifest.xml. This script
draws a simple placeholder so the packaging pipeline has something
valid to pack and the CI build goes green from day one -- swap these
for real designed icons before submitting to the Store. Real icon
guidance: https://learn.microsoft.com/windows/apps/design/style/app-icons-and-logos
"""

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = {
    "StoreLogo.png": (50, 50),
    "Square44x44Logo.png": (44, 44),
    "Square150x150Logo.png": (150, 150),
    "Wide310x150Logo.png": (310, 150),
}

OUTPUT_DIR = Path(__file__).parent / "Assets"


def draw_icon(size: tuple[int, int]) -> Image.Image:
    img = Image.new("RGBA", size, (37, 99, 235, 255))  # solid blue background
    draw = ImageDraw.Draw(img)
    w, h = size
    margin = min(w, h) * 0.2
    draw.polygon(
        [
            (margin, h * 0.5),
            (w * 0.5, h - margin),
            (w - margin, margin),
        ],
        outline=(255, 255, 255, 255),
        width=max(2, int(min(w, h) * 0.06)),
    )
    return img


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, size in ASSETS.items():
        draw_icon(size).save(OUTPUT_DIR / filename)
        print(f"wrote {OUTPUT_DIR / filename} ({size[0]}x{size[1]})")


if __name__ == "__main__":
    main()
