"""LinkedIn + Indeed adapter qua JobSpy (đã verify 15/10/2026).

JobSpy chạy blocking -> chạy trong threadpool. LinkedIn: ổn định cho VN.
Indeed VN: theo docs/research-job-data-sources.md cần country_indeed="vietnam".
JobSpy không có API async nên wrap bằng asyncio.to_thread.
LinkedIn: JobSpy fetch_description dính 429 là trả JD rỗng không báo lỗi -> tắt,
tự lấy JD từ trang /jobs/view/{id} tuần tự, giãn cách, retry khi 429.
"""
from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

from ..domain.models import NormalizedJob, utcnow, SourceName
from .base import DiscoverInput, JobSourceAdapter, SourceHealth
from .detail_fetchers import UA, LinkedInDetailFetcher


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


class LinkedInAdapter(JobSourceAdapter):
    name = SourceName.LINKEDIN.value

    def __init__(self, hours_old: int = 720, detail_delay: float = 2.0):
        self.hours_old = hours_old
        self.detail_delay = detail_delay

    async def discover(self, input: DiscoverInput) -> AsyncIterator[NormalizedJob]:
        import httpx
        jobs = await asyncio.to_thread(self._scrape, input)
        fetcher = LinkedInDetailFetcher()
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True,
            headers={"User-Agent": UA, "Accept-Language": "vi,en;q=0.8"},
        ) as client:
            for i, j in enumerate(jobs):
                if not j.description and j.source_url:
                    if i:
                        await asyncio.sleep(self.detail_delay)  # tránh 429
                    try:
                        d = await fetcher.fetch(j.source_url, client)
                    except Exception:  # noqa: BLE001 - lỗi mạng -> job không có JD
                        d = None
                    if d:
                        j.description = d.description[:4000]
                yield j

    def _scrape(self, input: DiscoverInput) -> list[NormalizedJob]:
        from jobspy import scrape_jobs

        df = scrape_jobs(
            site_name=["linkedin"],
            search_term=input.query,
            location=input.location or "Vietnam",
            results_wanted=min(input.max_results, 25),
            hours_old=self.hours_old,
            fetch_description=False,  # JD lấy trong discover (xem docstring module)
        )
        out = []
        for _, r in df.iterrows():
            desc = _strip_html(str(r.description or ""))[:4000]
            out.append(NormalizedJob(
                title=str(r.title or ""),
                company_name=str(r.company or ""),
                city=str(r.location or ""),
                address=str(r.location or ""),
                description=desc,
                is_hcm="hcm" in str(r.location or "").lower()
                       or "chi minh" in str(r.location or "").lower(),
                apply_url=str(r.job_url or ""),
                source=self.name,
                source_url=str(r.job_url or ""),
                source_job_id=str(r.id or r.job_url or ""),
                last_verified_at=utcnow(),
            ))
        return out

    async def health_check(self) -> SourceHealth:
        try:
            jobs = await asyncio.to_thread(
                self._scrape, DiscoverInput(query="marketing", max_results=5)
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


class IndeedAdapter(JobSourceAdapter):
    """Indeed VN qua JobSpy - nguồn có rating/review công ty kèm theo."""
    name = SourceName.INDEED.value

    async def discover(self, input: DiscoverInput) -> AsyncIterator[NormalizedJob]:
        jobs = await asyncio.to_thread(self._scrape, input)
        for j in jobs:
            yield j

    def _scrape(self, input: DiscoverInput) -> list[NormalizedJob]:
        from jobspy import scrape_jobs

        df = scrape_jobs(
            site_name=["indeed"],
            search_term=input.query,
            location=input.location or "Ho Chi Minh City, Vietnam",
            country_indeed="vietnam",
            results_wanted=min(input.max_results, 25),
        )
        out = []
        for _, r in df.iterrows():
            desc = _strip_html(str(r.description or ""))[:4000]
            out.append(NormalizedJob(
                title=str(r.title or ""),
                company_name=str(r.company or ""),
                city=str(r.location or ""),
                address=str(r.location or ""),
                description=desc,
                is_hcm="hcm" in str(r.location or "").lower()
                       or "chi minh" in str(r.location or "").lower(),
                apply_url=str(r.job_url or ""),
                source=self.name,
                source_url=str(r.job_url or ""),
                source_job_id=str(r.id or r.job_url or ""),
                last_verified_at=utcnow(),
            ))
        return out
