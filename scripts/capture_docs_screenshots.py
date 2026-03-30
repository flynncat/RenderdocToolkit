from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


APP_URL = "http://127.0.0.1:8026"
OUTPUT_DIR = Path(r"g:\RenderdocSKillEvn\docs\images")


async def fill_diagnose(page) -> None:
    await page.fill("#before-path", r"D:\Captures\sample_before.rdc")
    await page.fill("#after-path", r"D:\Captures\sample_after.rdc")
    await page.fill("#pass-name", "MobileBasePass")
    await page.fill("#issue", "同一模型在两次抓帧中的亮度表现不一致。")
    await page.fill("#eid-before", "575")
    await page.fill("#eid-after", "610")


async def fill_cmp(page) -> None:
    await page.click('.tab-btn[data-tab="cmp"]')
    await page.wait_for_timeout(300)
    await page.fill("#cmp-base-path", r"D:\Captures\base.rdc")
    await page.fill("#cmp-new-path", r"D:\Captures\new.rdc")
    await page.fill("#cmp-renderdoc-dir", r"C:\Program Files\RenderDoc")
    await page.fill("#cmp-malioc-path", r"C:\Tools\malioc.exe")


async def fill_asset_export(page) -> None:
    await page.click('.tab-btn[data-tab="asset-export"]')
    await page.wait_for_timeout(300)
    await page.fill("#asset-capture-source-path", r"D:\Captures\character_frame.rdc")
    await page.select_option("#asset-export-scope", "range")
    await page.fill("#asset-pass-manual-eid", "5665")
    await page.fill("#asset-pass-start-manual-eid", "5200")
    await page.fill("#asset-pass-end-manual-eid", "5800")
    await page.fill(
        "#asset-csv-source-path",
        "\n".join(
            [
                r"D:\Exports\character\Part_A.csv",
                r"D:\Exports\character\Part_B.csv",
                r"D:\Exports\character\Part_C.csv",
            ]
        ),
    )


async def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1600, "height": 980}, device_scale_factor=1)
        await page.goto(APP_URL, wait_until="networkidle")
        close_button = page.locator("#setup-close-btn")
        if await close_button.count():
            try:
                await close_button.click(timeout=1000)
                await page.wait_for_timeout(300)
            except Exception:
                pass

        await fill_diagnose(page)
        await page.screenshot(path=str(OUTPUT_DIR / "overview-home.png"))

        await fill_cmp(page)
        await page.screenshot(path=str(OUTPUT_DIR / "cmp-report.png"))

        await fill_asset_export(page)
        await page.screenshot(path=str(OUTPUT_DIR / "asset-export.png"))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
