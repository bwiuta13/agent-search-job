"""CareerViet adapter - parse HTML (giao diện Next.js mới, verify 20/10/2026).

Cần --compressed (httpx tự negotiate gzip). URL tìm kiếm:
careerviet.vn/viec-lam/<kw>-tai-ho-chi-minh-kl8-vi.html (trang 1: 50 việc).
Link việc: /vi/tim-viec-lam/<slug>.<mã hex>.html (bản cũ /viec-lam/<slug>-<id>-vi.html không còn
-> từ 15/10 adapter khớp 0 việc ở mọi từ khoá).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from ..domain.models import NormalizedJob, utcnow, SourceName
from .base import DiscoverInput, JobSourceAdapter, SourceHealth

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class CareerVietAdapter(JobSourceAdapter):
    name = SourceName.CAREERVIET.value
    base = "https://careerviet.vn"

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    async def discover(self, input: DiscoverInput) -> AsyncIterator[NormalizedJob]:
        kw = re.sub(r"[^a-z0-9\s]", "", input.query.lower()).strip().replace(" ", "-")
        url = f"{self.base}/viec-lam/{kw or 'content'}-tai-ho-chi-minh-kl8-vi.html"
        async with httpx.AsyncClient(
            timeout=self.timeout, headers={"User-Agent": UA, "Accept-Encoding": "gzip"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return
            html = resp.text
        for job in self._parse_listing(html)[: input.max_results]:
            yield job

    def _parse_listing(self, html: str) -> list[NormalizedJob]:
        # Mỗi việc là 1 khối <div class="job-item" id="job-item-<mã>">: a.job_link (data-id, title, href),
        # a.company-name, div.salary ("Lương: 15 - 20 Triệu VND" | "Cạnh tranh"), div.location <li>, <time> hạn nộp.
        # Tách theo khối rồi cắt 8000 ký tự (thật ~4-5KB, phần lớn là svg): regex không bao giờ chạy trên cả trang 1.6MB.
        jobs = []
        seen = set()
        for blk in html.split('<div class="job-item')[1:]:
            blk = blk[:8000]
            link = re.search(r'<a [^>]*class="job_link"[^>]*>', blk)
            if not link:
                continue
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', link.group(0)))
            jid, href, title = attrs.get("data-id", ""), attrs.get("href", ""), attrs.get("title", "").strip()
            if not jid or not href.startswith("/") or not title or jid in seen:
                continue
            seen.add(jid)
            company = re.search(r'<a [^>]*class="company-name"[^>]*>([^<]*)</a>', blk)
            salary = re.search(r'<div class="salary"><p>(?:<em[^>]*></em>)?(?:Lương:\s*)?([^<]*)</p>', blk)
            location = re.search(r'<div class="location">.*?<ul>(.*?)</ul>', blk, re.S)
            deadline = re.search(r"Hạn nộp.*?<time>(\d{2})-(\d{2})-(\d{4})</time>", blk, re.S)
            address = ", ".join(x.strip() for x in re.findall(r"<li>([^<]*)</li>", location.group(1))) if location else ""
            jobs.append(NormalizedJob(
                title=title,
                company_name=company.group(1).strip() if company else "",
                salary_text=salary.group(1).strip() if salary else "",
                city="Hồ Chí Minh",
                address=address or "Hồ Chí Minh",
                is_hcm=True,
                deadline=(datetime(int(deadline.group(3)), int(deadline.group(2)), int(deadline.group(1)),
                                   tzinfo=timezone.utc) if deadline else None),
                apply_url=f"{self.base}{href}",
                source=self.name,
                source_url=f"{self.base}{href}",
                source_job_id=jid,
                last_verified_at=utcnow(),
            ))
        return jobs

    async def health_check(self) -> SourceHealth:
        import time
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers={"User-Agent": UA}, follow_redirects=True
            ) as client:
                resp = await client.get(f"{self.base}/")
            latency = int((time.monotonic() - start) * 1000)
            return SourceHealth(
                source=self.name, ok=resp.status_code == 200,
                latency_ms=latency, checked_at=utcnow(),
            )
        except Exception as e:  # noqa: BLE001
            return SourceHealth(
                source=self.name, ok=False, error=f"{type(e).__name__}: {e}",
                checked_at=utcnow(),
            )
