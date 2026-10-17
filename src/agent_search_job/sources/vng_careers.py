"""VNG Careers adapter - career.vng.com.vn (verify cấu trúc thật 16/10/2026).

Trang Next.js server render: __NEXT_DATA__ -> props.pageProps.jobs (10 jobs/trang,
total 78). "description" trong list chỉ là excerpt text phẳng (mất <li>/<br>) -> job HCM
fetch thêm trang /tim-kiem-viec-lam/chi-tiet/{slug}: job_data.description + requirement
là HTML đầy đủ (verify 16/10/2026).
- Job HCM: location == "Thành phố Hồ Chí Minh"
- job_family: Marketing/Creative... -> dùng làm hint phân loại
- Lọc phía ingest: chỉ giữ job thuộc role list người dùng (scope filter chung).

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
from .detail_fetchers import _strip

BASE = "https://career.vng.com.vn/vi/tim-kiem-viec-lam"
LIST_URL = "https://career.vng.com.vn/co-hoi-nghe-nghiep"  # fallback (không phân trang)
DETAIL_LIST = "https://career.vng.com.vn/tim-kiem-viec-lam/chi-tiet"
DETAIL_BASE = "https://career.vng.com.vn/vi/job"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


async def _fetch_page(client: httpx.AsyncClient, page: int) -> tuple[list[dict], int]:
    # PHẢI dùng /vi/tim-kiem-viec-lam?page=N - URL /co-hoi-nghe-nghiep BỎ QUA page
    # param (trả y hệt 10 job đầu mọi trang) -> mất 68/78 job (verify 16/10/2026)
    resp = await client.get(BASE, params={"page": page})
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
        # slug -> job_data trang chi tiết; ingest gọi discover 1 lần/query mà VNG bỏ qua
        # query -> cache để mỗi job chỉ fetch detail 1 lần/lượt quét
        self._details: dict[str, dict] = {}

    async def discover(self, input: DiscoverInput) -> AsyncIterator[NormalizedJob]:
        async with httpx.AsyncClient(
            timeout=self.timeout, headers={"User-Agent": UA},
            follow_redirects=True,
        ) as client:
            total = 0
            emitted = 0
            seen_ids: set[str] = set()  # dedupe trong discover (phòng server lặp trang)
            for page in range(1, self.max_pages + 1):
                jobs, t = await _fetch_page(client, page)
                if not jobs:
                    break
                total = t or total
                new_in_page = 0
                for raw in jobs:
                    key = str(raw.get("job_id") or raw.get("slug") or "")
                    if key and key in seen_ids:
                        continue
                    if key:
                        seen_ids.add(key)
                    if emitted >= input.max_results:
                        return
                    j = self._to_job(raw)
                    slug = str(raw.get("slug") or "")
                    if j and j.is_hcm and slug:
                        # list chỉ có excerpt text phẳng (mất <li>/<br>) -> lấy HTML
                        # description + requirement ở trang chi tiết (job_data)
                        if slug not in self._details:
                            jd: dict = {}
                            try:
                                resp = await client.get(f"{DETAIL_LIST}/{slug}")
                                m = re.search(
                                    r'<script id="__NEXT_DATA__" type="application/json">'
                                    r'(.*?)</script>', resp.text, re.S,
                                ) if resp.status_code == 200 else None
                                if m:
                                    jd = json.loads(m.group(1))["props"]["pageProps"].get(
                                        "job_data") or {}
                            except (httpx.HTTPError, ValueError, KeyError,
                                    TypeError, AttributeError):
                                jd = {}
                            self._details[slug] = jd if isinstance(jd, dict) else {}
                        jd = self._details[slug]
                        full = _strip(str(jd.get("description") or ""))
                        if full:
                            j.description = full[:4000]
                            j.requirements = _strip(str(jd.get("requirement") or ""))[:2000]
                    if j:
                        emitted += 1
                        new_in_page += 1
                        yield j
                if new_in_page == 0 and page > 1:
                    break  # trang lặp -> hết dữ liệu mới
                if page * 10 >= total:
                    break

    def _to_job(self, raw: dict) -> NormalizedJob | None:
        title = str(raw.get("title") or "").strip()
        if not title:
            return None
        loc = str(raw.get("location") or "")
        desc = _strip(str(raw.get("description") or ""))[:4000]
        slug = str(raw.get("slug") or "")
        detail_url = f"{DETAIL_LIST}/{slug}" if slug else LIST_URL
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
