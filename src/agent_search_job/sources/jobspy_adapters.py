"""LinkedIn + Indeed adapter qua JobSpy (đã verify 15/10/2026).

JobSpy chạy blocking -> chạy trong threadpool. LinkedIn: ổn định cho VN.
Indeed VN: theo docs/research-job-data-sources.md cần country_indeed="vietnam".
JobSpy không có API async nên wrap bằng asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator

from ..domain.models import NormalizedJob, utcnow, SourceName
from .base import DiscoverInput, JobSourceAdapter, SourceHealth


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


class LinkedInAdapter(JobSourceAdapter):
    name = SourceName.LINKEDIN.value

    def __init__(self, hours_old: int = 720):
        self.hours_old = hours_old

    async def discover(self, input: DiscoverInput) -> AsyncIterator[NormalizedJob]:
        jobs = await asyncio.to_thread(self._scrape, input)
        for j in jobs:
            yield j

    def _scrape(self, input: DiscoverInput) -> list[NormalizedJob]:
        from jobspy import scrape_jobs

        df = scrape_jobs(
            site_name=["linkedin"],
            search_term=input.query,
            location=input.location or "Vietnam",
            results_wanted=min(input.max_results, 25),
            hours_old=self.hours_old,
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
