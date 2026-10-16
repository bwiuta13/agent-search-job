import asyncio
import sys
sys.path.insert(0, "src")
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8765"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 860}, locale="vi-VN",
        )
        page = await ctx.new_page()

        # 1) trang login
        await page.goto(f"{BASE}/", wait_until="networkidle", timeout=30000)
        await page.screenshot(path="/tmp/ui_1_login.png")

        # 2) login -> search
        await page.goto(f"{BASE}/", wait_until="networkidle")
        await page.fill('input[name="email"]', "friend@example.com")
        await page.click('button[type="submit"]')
        await page.wait_for_load_state("networkidle")
        # search "content"
        await page.goto(f"{BASE}/search?q=content", wait_until="networkidle")
        await page.wait_for_timeout(500)
        await page.screenshot(path="/tmp/ui_2_search.png", full_page=False)

        # scroll xuống để thấy badge "phù hợp X%"
        await page.evaluate("window.scrollBy(0, 400)")
        await page.wait_for_timeout(300)
        await page.screenshot(path="/tmp/ui_3_search_scroll.png")

        # 3) trang chi tiết job (job đầu tiên)
        link = await page.query_selector('a[href^="/jobs/"]')
        if link:
            await link.click()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(300)
            await page.screenshot(path="/tmp/ui_4_job_detail.png")

        # 4) trang công ty
        comp = await page.query_selector('a[href^="/companies/"]')
        if comp:
            await comp.click()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(300)
            await page.screenshot(path="/tmp/ui_5_company.png")

        # 5) search với filter cấp bậc + companies
        await page.goto(f"{BASE}/search?job_level=senior", wait_until="networkidle")
        await page.wait_for_timeout(400)
        await page.screenshot(path="/tmp/ui_6_level_filter.png")
        await page.goto(f"{BASE}/companies", wait_until="networkidle")
        await page.wait_for_timeout(400)
        await page.screenshot(path="/tmp/ui_7_companies.png")

        await browser.close()
        print("done")

asyncio.run(main())
