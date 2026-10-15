"""CareerViet adapter - parse HTML (đã verify 15/10/2026).

Cần --compressed (httpx tự negotiate gzip). URL pattern:
careerviet.vn/viec-lam/<kw>-tai-ho-chi-minh-kl8-vi.html
"""
from __future__ import annotations

import re
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
        for job in self._parse_listing(html):
            yield job

    def _parse_listing(self, html: str) -> list[NormalizedJob]:
        jobs = []
        seen = set()
        # listing links: /viec-lam/<slug>-<id>-vi.html
        for m in re.finditer(r'href="(/viec-lam/([a-z0-9-]+?)-(\d{6,8})-vi\.html)"', html):
            href, slug, jid = m.group(1), m.group(2), m.group(3)
            if jid in seen:
                continue
            seen.add(jid)
            title = slug.replace("-", " ").title()
            jobs.append(NormalizedJob(
                title=title,
                company_name="",
                city="Hồ Chí Minh",
                address="Hồ Chí Minh",
                is_hcm=True,
                apply_url=f"{self.base}{href}",
                source=self.name,
                source_url=f"{self.base}{href}",
                source_job_id=jid,
                last_verified_at=utcnow(),
            ))
        return jobs[:50]

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
