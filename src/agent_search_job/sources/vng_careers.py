"""VNG Careers adapter - career.vng.com.vn (verify cấu trúc thật 16/10/2026).

Trang Next.js server render: __NEXT_DATA__ -> props.pageProps.jobs (10 jobs/trang,
total 78). Mỗi job có JD đầy đủ (1700+ chars) ngay trong list - không cần fetch detail.
- Job HCM: location == "Thành phố Hồ Chí Minh"
- job_family: Marketing/Creative... -> dùng làm hint phân loại
- Lọc phía ingest: chỉ giữ job thuộc Truyền thông đa phương tiện (scope filter chung).

Công ty lớn khác (FPT): fpt.com.vn + fpt-software.com đều chặn Cloudflare (403)
hoặc 404 - không có endpoint công khai; bỏ qua, ghi nhận trong docs.
"""
from __future__ import annotations

import json
import re
from typing import AsyncIterator

import httpx

from ..domain.models import NormalizedJob, SourceName, utcnow
from .base import DiscoverInput, JobSourceAdapter, SourceHealth

BASE = "https://career.vng.com.vn/vi/tim-kiem-viec-lam"
LIST_URL = "https://career.vng.com.vn/co-hoi-nghe-nghiep"
DETAIL_BASE = "https://career.vng.com.vn/vi/job"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"</(p|li|div|h\d|ul|ol)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text.replace("&nbsp;", " ")
                  .replace("&amp;", "&")).strip()


async def _fetch_page(client: httpx.AsyncClient, page: int) -> tuple[list[dict], int]:
    resp = await client.get(LIST_URL, params={"page": page})
    if resp.status_code != 200:
        return [], 0
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        resp.text, re.S,
    )
    if not m:
        return [], 0
    try:
        d = json.loads(m.group(1))
    except json.JSONDecodeError:
        return [], 0
    pp = d.get("props", {}).get("pageProps", {})
    return (pp.get("jobs") or []), int(pp.get("total") or 0)


class VNGCareersAdapter(JobSourceAdapter):
    name = SourceName.VNG.value

    def __init__(self, timeout: float = 20.0, max_pages: int = 8):
        self.timeout = timeout
        self.max_pages = max_pages

    async def discover(self, input: DiscoverInput) -> AsyncIterator[NormalizedJob]:
        async with httpx.AsyncClient(
            timeout=self.timeout, headers={"User-Agent": UA},
            follow_redirects=True,
        ) as client:
            total = 0
            emitted = 0
            for page in range(1, self.max_pages + 1):
                jobs, t = await _fetch_page(client, page)
                if not jobs:
                    break
                total = t or total
                for raw in jobs:
                    if emitted >= input.max_results:
                        return
                    j = self._to_job(raw)
                    if j:
                        emitted += 1
                        yield j
                if page * 10 >= total:
                    break

    def _to_job(self, raw: dict) -> NormalizedJob | None:
        title = str(raw.get("title") or "").strip()
        if not title:
            return None
        loc = str(raw.get("location") or "")
        desc = _strip_html(str(raw.get("description") or ""))[:4000]
        slug = str(raw.get("slug") or "")
        detail_url = f"{DETAIL_BASE}/{slug}" if slug else LIST_URL
        return NormalizedJob(
            title=title,
            company_name="VNG Corporation",
            city="Hồ Chí Minh" if "Hồ Chí Minh" in loc else loc,
            address=loc,
            description=desc,
            is_hcm="Hồ Chí Minh" in loc,
            apply_url=detail_url,
            source=self.name,
            source_url=detail_url,
            source_job_id=str(raw.get("job_id") or slug or title),
            last_verified_at=utcnow(),
        )

    async def health_check(self) -> SourceHealth:
        import time
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers={"User-Agent": UA},
                follow_redirects=True,
            ) as client:
                jobs, total = await _fetch_page(client, 1)
            latency = int((time.monotonic() - start) * 1000)
            return SourceHealth(
                source=self.name, ok=bool(jobs), latency_ms=latency,
                records_last_run=len(jobs), checked_at=utcnow(),
            )
        except Exception as e:  # noqa: BLE001
            return SourceHealth(
                source=self.name, ok=False, error=f"{type(e).__name__}: {e}",
                checked_at=utcnow(),
            )
