"""TopCV adapter - Playwright headless bypass Cloudflare (đã verify 15/10/2026).

TopCV trả 403 cho HTTP client thường và cả Jina Reader; chỉ browser thật qua được.
URL pattern đã verify: topcv.vn/tim-viec-lam-{slug}-tai-ho-chi-minh-kl2?page=N (lọc TP.HCM, phân trang).
Job cards: data-job-id + aria-label, salary dạng "10 - 15 triệu".
Chậm (~15s/request) -> pipeline chạy background worker, không chạy trong tool call MCP.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from typing import AsyncIterator, Optional

from ..domain.models import Company, NormalizedJob, utcnow, SourceName
from .base import DiscoverInput, JobSourceAdapter, SourceHealth

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.topcv.vn/tim-viec-lam-{slug}-tai-ho-chi-minh-kl2"  # đã lọc TP.HCM (kl2)
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class TopCVAdapter(JobSourceAdapter):
    """Mỗi từ khoá lật trang ?page=2,3... (25–50 job/trang) tới khi hết job mới hoặc đủ
    TOPCV_MAX_PAGES trang. Đã đo 22/11/2026: "content-marketing" TP.HCM > 7 trang, "marketing"
    > 7 trang x 50 — chỉ lấy trang 1 như trước thì sót ~280/330 job đúng nhóm nghề.

    Quét lịch sự: TopCV (Cloudflare) trả 429 + trang "Chờ một chút..." khi tải trang dồn dập
    (đo 22/11/2026). Nghỉ TOPCV_PAGE_DELAY giây giữa các trang, tối đa TOPCV_MAX_PAGES_PER_RUN
    trang mỗi lượt; gặp 429/trang xác minh thì DỪNG TopCV cả lượt (không cố vượt), ghi lỗi."""
    name = SourceName.TOPCV.value

    def __init__(self, headless: bool = True, slow_mo: int = 0):
        self.headless = headless
        self.slow_mo = slow_mo
        self.max_pages = max(1, int(os.environ.get("TOPCV_MAX_PAGES") or 10))
        # pipeline đọc thuộc tính này thay cho giới hạn chung (30/từ khoá) vì TopCV phân trang sâu
        self.max_results_per_query = max(1, int(os.environ.get("TOPCV_MAX_RESULTS") or 250))
        self._seen: set[str] = set()  # job_id đã trả trong lượt này: từ khoá sau trùng thì không lật tiếp
        self.page_delay = max(0.0, float(os.environ.get("TOPCV_PAGE_DELAY") or 8))
        self.pages_left = max(1, int(os.environ.get("TOPCV_MAX_PAGES_PER_RUN") or 60))
        self.blocked = False

    async def discover(self, input: DiscoverInput) -> AsyncIterator[NormalizedJob]:
        if self.blocked:
            raise RuntimeError("TopCV đang giới hạn truy cập (429/xác minh bot) — bỏ qua TopCV lượt này")
        if self.pages_left <= 0:
            log.info("TopCV: đã dùng hết %s trang của lượt này, bỏ qua %r",
                     os.environ.get("TOPCV_MAX_PAGES_PER_RUN") or 60, input.query)
            return
        # bỏ dấu tiếng Việt: "sáng tạo nội dung" -> "sang-tao-noi-dung" (slug TopCV)
        plain = unicodedata.normalize("NFD", input.query.lower().replace("đ", "d"))
        plain = "".join(ch for ch in plain if unicodedata.category(ch) != "Mn")
        slug = re.sub(r"\s+", "-", re.sub(r"[^a-z0-9\s-]", "", plain).strip()) or "content-marketing"
        jobs = await self._fetch_search_pages(SEARCH_URL.format(slug=slug), self.max_pages,
                                              input.max_results, self._seen)
        for j in jobs:
            yield j

    async def _fetch_search_pages(self, url: str, max_pages: int, max_results: int,
                                  seen: set[str]) -> list[NormalizedJob]:
        """Lật trang tới khi một trang không còn job chưa thấy (hết kết quả hoặc toàn job trùng)."""
        from playwright.async_api import async_playwright

        out: list[NormalizedJob] = []
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
                for n in range(1, max_pages + 1):
                    if self.pages_left <= 0:
                        break
                    if n > 1 or self._seen:  # mọi lần tải trang trừ trang đầu tiên của lượt
                        await asyncio.sleep(self.page_delay)
                    self.pages_left -= 1
                    resp = await page.goto(url + (f"?page={n}" if n > 1 else ""),
                                           wait_until="domcontentloaded", timeout=60000)
                    if (resp is not None and resp.status in (403, 429)) or \
                            re.search(r"Chờ một chút|Just a moment", await page.title()):
                        self.blocked = True
                        log.warning("TopCV trả %s (Cloudflare giới hạn truy cập) ở %s — dừng TopCV lượt này",
                                    resp.status if resp is not None else "?", page.url)
                        break
                    if n == 1:  # cookie banner bắt buộc phải chấp nhận (1 lần mỗi browser)
                        for sel in (
                            "button:has-text('Chấp nhận tất cả')",
                            "#cookie_policy_accept_btn",
                        ):
                            try:
                                await page.click(sel, timeout=4000)
                                break
                            except Exception:  # noqa: BLE001
                                pass
                    await page.wait_for_timeout(6000 if n == 1 else 3000)
                    fresh = [j for j in self._parse_search_html(await page.content())
                             if j.source_job_id not in seen]
                    if not fresh:
                        break
                    for j in fresh:
                        seen.add(j.source_job_id)
                        # trang đã lọc TP.HCM: thẻ ghi "Hà Nội & 2 nơi khác" vẫn có chỗ làm ở HCM
                        j.city, j.address = "Hồ Chí Minh", "Hồ Chí Minh, Việt Nam"
                    out += fresh
                    if len(out) >= max_results:
                        break
            finally:
                await browser.close()
        return out[:max_results]

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
            jobs = await self._fetch_search_pages(
                SEARCH_URL.format(slug="content-marketing"), 1, 5, set()
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
