"""Draw the app icon (fortis-ledger.ico, and icon-512.png for the Claude Desktop extension) - the same design as
fortis_ledger/web/static/icon.svg."""
from pathlib import Path

from PIL import Image, ImageDraw


def draw(size: int) -> Image.Image:
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Fortis blue, top to bottom as in the logo's bar (#0099ff - #0033b3)
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        gd.line([(0, y), (size, y)], fill=(0, int(0x99 + (0x33 - 0x99) * t), int(0xFF + (0xB3 - 0xFF) * t), 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=12 * s, fill=255)
    img.paste(grad, (0, 0), mask)
    f = [(17, 15), (47, 15), (47, 22), (25, 22), (25, 29), (43, 29), (43, 36), (25, 36), (25, 49), (17, 49)]
    d.polygon([(x * s, y * s) for x, y in f], fill=(255, 255, 255, 255))
    mark = [(39, 40), (47, 40), (47, 49), (33, 49), (33, 43), (39, 43)]
    d.polygon([(x * s, y * s) for x, y in mark], fill=(18, 32, 46, 220))
    return img


if __name__ == "__main__":
    out = Path(__file__).with_name("fortis-ledger.ico")
    big = draw(256)
    big.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(out)
    png = Path(__file__).resolve().parent.parent / "fortis_ledger" / "web" / "static" / "icon-512.png"
    draw(512).save(png, optimize=True)
    print(png)
