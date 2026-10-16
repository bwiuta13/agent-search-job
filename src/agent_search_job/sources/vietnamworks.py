"""VietnamWorks adapter - internal API đã verify 15/10/2026.

POST ms.vietnamworks.com/job-search/v1.0/search
- Không cần auth; header Origin/Referer bắt buộc.
- HCM = cityId 29 (docs/research-job-data-sources.md mục 3).
- Dữ liệu giàu: salary min/max, benefits, skills, companyProfile, workingLocations.
- retrieveFields phải có "jobId": thiếu thì API trả jobId=0 và jobUrl rỗng.
- jobDescription/jobRequirement trong search bị cắt ~300 ký tự -> discover lấy
  JD đầy đủ từ trang detail (VietnamWorksDetailFetcher), lỗi thì giữ bản cắt.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator, Optional

import httpx

from ..domain.models import Company, NormalizedJob, utcnow, SourceName
from .base import DiscoverInput, JobSourceAdapter, SourceHealth
from .detail_fetchers import UA, VietnamWorksDetailFetcher

API_URL = "https://ms.vietnamworks.com/job-search/v1.0/search"
HCM_CITY_ID = "29"

HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.vietnamworks.com",
    "Referer": "https://www.vietnamworks.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
}

RETRIEVE_FIELDS = [
    "jobId", "jobTitle", "jobUrl", "companyName", "companyProfile", "companySize",
    "salary", "salaryMin", "salaryMax", "prettySalary", "isSalaryVisible",
    "jobDescription", "jobRequirement", "benefits", "skills",
    "workingLocations", "address", "industries", "industriesV3",
    "approvedOn", "expiredOn", "alias", "jobLevelVI", "numOfApplications",
    "companyId", "companyUrl",
]


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _parse_iso(s: str):
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class VietnamWorksAdapter(JobSourceAdapter):
    name = SourceName.VIETNAMWORKS.value

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    async def discover(self, input: DiscoverInput) -> AsyncIterator[NormalizedJob]:
        payload = {
            "userId": 0,
            "query": input.query,
            "filter": [{"field": "workingLocations.cityId", "value": HCM_CITY_ID}],
            "ranges": [],
            "order": [{"field": "relevant", "value": "asc"}],
            "hitsPerPage": min(input.max_results, 50),
            "page": 0,
            "retrieveFields": RETRIEVE_FIELDS,
        }
        async with httpx.AsyncClient(timeout=self.timeout, headers=HEADERS) as client:
            resp = await client.post(API_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
        jobs = [self._to_job(raw) for raw in data.get("data", [])]
        # JD trong search bị cắt -> lấy bản đầy đủ từ trang detail (5 request song song)
        sem = asyncio.Semaphore(5)
        fetcher = VietnamWorksDetailFetcher()
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers={"User-Agent": UA},
        ) as client:
            async def fill(job: NormalizedJob):
                if not job.source_url:
                    return
                async with sem:
                    try:
                        d = await fetcher.fetch(job.source_url, client)
                    except Exception:  # noqa: BLE001 - lỗi trang detail -> giữ JD cắt
                        return
                if d:
                    job.description = d.description[:4000]
                    job.requirements = (d.requirements or job.requirements)[:2000]
            await asyncio.gather(*(fill(j) for j in jobs))
        for job in jobs:
            yield job

    def _to_job(self, raw: dict) -> NormalizedJob:
        locs = raw.get("workingLocations") or []
        addr = "; ".join(
            f"{l.get('address', '')} {l.get('cityNameVI', '')}".strip() for l in locs
        ) or raw.get("address", "")
        salary_text = raw.get("prettySalary") or ""
        if not salary_text and raw.get("isSalaryVisible") and raw.get("salary"):
            salary_text = str(raw.get("salary"))
        benefits = [b for b in (raw.get("benefits") or []) if isinstance(b, str)]
        skills = [s for s in (raw.get("skills") or []) if isinstance(s, str)]
        return NormalizedJob(
            title=raw.get("jobTitle", ""),
            company_name=raw.get("companyName", ""),
            salary_text=salary_text,
            address=addr,
            description=_strip_html(raw.get("jobDescription", ""))[:4000],
            requirements=_strip_html(raw.get("jobRequirement", ""))[:2000],
            benefits=benefits,
            languages=skills[:10],
            deadline=_parse_iso(raw.get("expiredOn", "")),
            apply_url=raw.get("jobUrl", ""),
            source=self.name,
            source_url=raw.get("jobUrl", ""),
            source_job_id=str(raw.get("jobId", "")),
            last_verified_at=utcnow(),
        )

    async def health_check(self) -> SourceHealth:
        import time
        start = time.monotonic()
        try:
            payload = {
                "userId": 0, "query": "marketing", "filter": [],
                "ranges": [], "order": [], "hitsPerPage": 1, "page": 0,
                "retrieveFields": ["jobTitle"],
            }
            async with httpx.AsyncClient(timeout=self.timeout, headers=HEADERS) as client:
                resp = await client.post(API_URL, json=payload)
                resp.raise_for_status()
                data = resp.json()
            latency = int((time.monotonic() - start) * 1000)
            return SourceHealth(
                source=self.name, ok=True, latency_ms=latency,
                records_last_run=data.get("meta", {}).get("nbHits", 0),
                checked_at=utcnow(),
            )
        except Exception as e:  # noqa: BLE001 - health check nuốt mọi lỗi
            return SourceHealth(
                source=self.name, ok=False, error=f"{type(e).__name__}: {e}",
                checked_at=utcnow(),
            )
