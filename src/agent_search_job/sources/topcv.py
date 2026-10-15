"""TopCV adapter - Playwright headless bypass Cloudflare (đã verify 15/10/2026).

TopCV trả 403 cho HTTP client thường và cả Jina Reader; chỉ browser thật qua được.
URL pattern đã verify: topcv.vn/tim-viec-lam-{slug} ("content-marketing" -> 1.483 jobs).
Job cards: data-job-id + aria-label, salary dạng "10 - 15 triệu".
Chậm (~15s/request) -> pipeline chạy background worker, không chạy trong tool call MCP.
"""
from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator, Optional

from ..domain.models import Company, NormalizedJob, utcnow, SourceName
from .base import DiscoverInput, JobSourceAdapter, SourceHealth

SEARCH_URL = "https://www.topcv.vn/tim-viec-lam-{slug}"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# keyword -> slug tìm kiếm TopCV (đã verify pattern)
DEFAULT_SLUGS = [
    "content-marketing",
    "content-creator",
    "copywriter",
    "digital-marketing",
]


class TopCVAdapter(JobSourceAdapter):
    name = SourceName.TOPCV.value

    def __init__(self, headless: bool = True, slow_mo: int = 0):
        self.headless = headless
        self.slow_mo = slow_mo

    async def discover(self, input: DiscoverInput) -> AsyncIterator[NormalizedJob]:
        slug = self._query_to_slug(input.query)
        url = SEARCH_URL.format(slug=slug)
        jobs = await self._fetch_search_page(url, input.max_results)
        for j in jobs:
            yield j

    def _query_to_slug(self, query: str) -> str:
        s = re.sub(r"[^a-z0-9\s-]", "", query.lower()).strip()
        s = re.sub(r"\s+", "-", s)
        return s or "content-marketing"

    async def _fetch_search_page(self, url: str, max_results: int) -> list[NormalizedJob]:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            ctx = await browser.new_context(
                user_agent=UA, viewport={"width": 1440, "height": 900}, locale="vi-VN",
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # cookie banner bắt buộc phải chấp nhận
                for sel in (
                    "button:has-text('Chấp nhận tất cả')",
                    "#cookie_policy_accept_btn",
                ):
                    try:
                        await page.click(sel, timeout=4000)
                        break
                    except Exception:  # noqa: BLE001
                        pass
                await page.wait_for_timeout(6000)
                html = await page.content()
            finally:
                await browser.close()
        return self._parse_search_html(html)[:max_results]

    def _parse_search_html(self, html: str) -> list[NormalizedJob]:
        """Parse job cards từ search page - pattern đã verify."""
        cards: dict[str, NormalizedJob] = {}
        for m in re.finditer(
            r'data-job-id="(\d+)"[^>]*>(.*?)(?=data-job-id="\d+"|$)', html, re.DOTALL
        ):
            jid, blk = m.group(1), m.group(2)
            if jid in cards:
                continue
            t = re.search(r'aria-label="([^"]{5,120})"', blk)
            if not t or "đăng nhập" in t.group(1).lower():
                continue
            title = t.group(1)
            sal = re.search(
                r"(\d+[\d.,]*\s*-\s*\d+[\d.,]*\s*(?:triệu|TRIỆU)|"
                r"Trên\s*\d+[\d.,]*\s*triệu|Từ\s*\d+[\d.,]*\s*triệu|Thoả thuận)",
                blk, re.I,
            )
            comp = (
                re.search(r'title="((?:CÔNG TY|Công ty|Cong ty|CTY)[^"]{3,60})"', blk)
                or re.search(r'alt="((?:CÔNG TY|Công ty)[^"]{3,60})"', blk)
            )
            url_m = re.search(r'href="(https://www\.topcv\.vn/viec-lam/[^"]+)"', blk)
            loc = re.search(
                r"(Hà Nội|Hồ Chí Minh|Đà Nẵng|TP\.?\s*HCM|Hải Phòng|Bình Dương|Remote)", blk
            )
            cards[jid] = NormalizedJob(
                title=title,
                company_name=comp.group(1) if comp else "",
                salary_text=sal.group(1) if sal else "",
                city=loc.group(1) if loc else "",
                address=(loc.group(1) if loc else "") + ", Việt Nam",
                apply_url=(url_m.group(1).split("?")[0] if url_m else ""),
                source=self.name,
                source_url=(url_m.group(1).split("?")[0] if url_m else ""),
                source_job_id=jid,
                last_verified_at=utcnow(),
            )
        return list(cards.values())

    async def health_check(self) -> SourceHealth:
        try:
            jobs = await self._fetch_search_page(
                SEARCH_URL.format(slug="content-marketing"), 5
            )
            return SourceHealth(
                source=self.name, ok=True, records_last_run=len(jobs),
                checked_at=utcnow(),
            )
        except Exception as e:  # noqa: BLE001
            return SourceHealth(
                source=self.name, ok=False, error=f"{type(e).__name__}: {e}",
                checked_at=utcnow(),
            )
