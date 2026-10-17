"""
Draw the installer's pictures from the Fortis logo, in the app's colours:
    installer-side.png, installer-side-2x.png                       the panel on the first and last pages
    installer-top.png, installer-top-2x.png, installer-top-3x.png   the icon at the top right of the others
Each comes in sizes for ordinary and high-resolution screens; the installer picks the best fit.
Needs Playwright with Chromium, as for tests/test_ui_browser.py.  Run it again after a change to the
logo or the icon:  python packaging/make_installer_images.py
"""
import io
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "fortis_ledger" / "web" / "static"

SIDE = """<!doctype html><html><head><meta charset="utf-8"><style>
html, body { margin: 0; width: 164px; height: 314px; overflow: hidden; }
body { background: linear-gradient(180deg, #ffffff 0%, #ffffff 46%, #eef4ff 100%);
       font-family: 'Segoe UI', 'Inter', Arial, sans-serif; position: relative; }
.logo { position: absolute; top: 34px; left: 14px; width: 136px; }
.pill { position: absolute; top: 82px; left: 50%; transform: translateX(-50%); font-size: 13px; font-weight: 650;
        color: #0050c8; background: #e8f0ff; border-radius: 6px; padding: 2px 10px; letter-spacing: .02em; }
.icon { position: absolute; bottom: 30px; left: 50%; width: 56px; height: 56px; transform: translateX(-50%);
        filter: drop-shadow(0 6px 12px rgba(0, 51, 179, .25)); }
.bar { position: absolute; left: 0; right: 0; bottom: 0; height: 6px;
       background: linear-gradient(90deg, #0099ff, #0033b3); }
</style></head><body>
<img class="logo" src="LOGO"><span class="pill">Ledger</span><img class="icon" src="ICON"><div class="bar"></div>
</body></html>"""

TOP = """<!doctype html><html><head><meta charset="utf-8"><style>
html, body { margin: 0; width: 58px; height: 58px; overflow: hidden; background: transparent; }
img { position: absolute; left: 3px; top: 3px; width: 52px; height: 52px; }
</style></head><body><img src="ICON"></body></html>"""


def render(page, html: str, width: int, height: int, scale: int, out: Path, transparent: bool = False) -> None:
    html = html.replace("LOGO", (STATIC / "fortis-logo.svg").as_uri()).replace("ICON", (STATIC / "icon.svg").as_uri())
    tmp = HERE / f".render-{out.stem}.html"
    tmp.write_text(html, encoding="utf-8")
    try:
        page.set_viewport_size({"width": width, "height": height})
        page.goto(tmp.as_uri())
        page.wait_for_load_state("networkidle")
        png = page.screenshot(clip={"x": 0, "y": 0, "width": width, "height": height}, omit_background=transparent)
    finally:
        tmp.unlink()
    img = Image.open(io.BytesIO(png)).convert("RGBA" if transparent else "RGB")
    assert img.size == (width * scale, height * scale), img.size
    img.save(out, format="PNG", optimize=True)
    print(out, img.size)


def main() -> None:
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception:
            browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        for scale in (1, 2, 3):
            page = browser.new_page(device_scale_factor=scale)
            suffix = "" if scale == 1 else f"-{scale}x"
            if scale < 3:
                render(page, SIDE, 164, 314, scale, HERE / f"installer-side{suffix}.png")
            render(page, TOP, 58, 58, scale, HERE / f"installer-top{suffix}.png", transparent=True)
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
