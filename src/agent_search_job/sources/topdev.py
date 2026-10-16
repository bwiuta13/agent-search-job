"""TopDev adapter - public API + trang detail (verify cấu trúc thật 16/10/2026).

Đã verify:
- GET api.topdev.vn/td/v2/jobs?fields[job]=csv: status 200; salary là DICT với
  min/max bị che dấu (*), dùng min_filter/max_filter; level='free' (không dùng);
  addresses.full_addresses có quận.
- JD đầy đủ: trang detail topdev.vn/viec-lam/{slug} server render HTML từng mục
  (trách nhiệm / kỹ năng / quyền lợi) trong <div class="prose-ul"> - fetch bằng httpx
  được, không cần Playwright. Inline JSON "description" có nhưng đã mất <li>.
- HCM = region_ids=79. Keyword search "marketing" trả job thật.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from typing import AsyncIterator

import httpx

from ..domain.models import NormalizedJob, SourceName, utcnow
from .base import DiscoverInput, JobSourceAdapter, SourceHealth
from .detail_fetchers import _strip

BASE = "https://api.topdev.vn/td/v2/jobs"
FIELDS_CSV = ("id,title,salary,company,detail_url,level,addresses,deadlines,"
              "benefits,skills_str,experience")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _salary_text(sal: dict) -> str:
    """TopDev che min/max bằng '*' - chỉ dùng min_filter/max_filter khi có."""
    if not isinstance(sal, dict):
        return str(sal or "")
    lo = sal.get("min_filter")
    hi = sal.get("max_filter")
    if isinstance(lo, (int, str)) and isinstance(hi, (int, str)):
        try:
            lo_i, hi_i = int(lo), int(hi)
            if hi_i > 0 and lo_i > 0 and hi_i >= lo_i:
                return f"{lo_i // 1000000} - {hi_i // 1000000} triệu"
            if hi_i > 0:
                return f"Trên {hi_i // 1000000} triệu"
        except (TypeError, ValueError):
            pass
    if str(sal.get("is_negotiable") or "0") == "1":
        return "Thỏa thuận"
    return ""


def _first_district(addr: dict) -> str:
    for c in (addr.get("collection_addresses") or []):
        d = ((c.get("district") or {}).get("value")) if isinstance(c.get("district"), dict) else None
        if d:
            return d
    return ""


class TopDevAdapter(JobSourceAdapter):
    name = SourceName.TOPDEV.value

    def __init__(self, timeout: float = 20.0, region_hcm: str = "79"):
        self.timeout = timeout
        self.region_hcm = region_hcm

    async def discover(self, input: DiscoverInput) -> AsyncIterator[NormalizedJob]:
        kw = input.query.strip().lower() or "marketing"
        params = {
            "keyword": kw,
            "region_ids": self.region_hcm,
            "page": 1,
            "page_size": min(input.max_results, 30),
            "locale": "vi_VN",
            "fields[job]": FIELDS_CSV,
        }
        async with httpx.AsyncClient(
            timeout=self.timeout, headers={"User-Agent": UA},
        ) as client:
            resp = await client.get(BASE, params=params)
            if resp.status_code != 200:
                return
            data = resp.json()
        for raw in (data.get("data") or []):
            yield self._to_job(raw)

    def _to_job(self, raw: dict) -> NormalizedJob:
        comp = raw.get("company") or {}
        addr = raw.get("addresses") or {}
        full_addr = ""
        if isinstance(addr, dict):
            fa = addr.get("full_addresses")
            if isinstance(fa, list) and fa:
                full_addr = str(fa[0])
            elif isinstance(fa, str):
                full_addr = fa
        skills = []
        s = raw.get("skills_str")
        if s:
            skills = [x.strip() for x in re.split(r"[;,]+", str(s)) if x.strip()][:12]
        return NormalizedJob(
            title=raw.get("title", ""),
            company_name=comp.get("display_name", "") or "",
            salary_text=_salary_text(raw.get("salary")),
            district=_first_district(addr) if isinstance(addr, dict) else "",
            address=full_addr[:300],
            benefits=[],
            languages=skills,
            apply_url=raw.get("detail_url", "") or "",
            source=self.name,
            source_url=raw.get("detail_url", "") or "",
            source_job_id=str(raw.get("id", "")),
            last_verified_at=utcnow(),
        )

    async def health_check(self) -> SourceHealth:
        import time
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers={"User-Agent": UA},
            ) as client:
                resp = await client.get(BASE, params={
                    "keyword": "marketing", "region_ids": self.region_hcm,
                    "page": 1, "page_size": 1, "locale": "vi_VN",
                    "fields[job]": "id,title",
                })
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


async def fetch_topdev_detail(url: str, timeout: float = 20.0):
    """JD đầy đủ từ trang detail TopDev: các khối prose-ul server render (giữ xuống dòng)."""
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": UA}, follow_redirects=True,
    ) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        return None
    h = resp.text
    # JD render sẵn trong các khối <div class="prose-ul"> (mỗi mục 1 khối: trách nhiệm,
    # kỹ năng, quyền lợi) - còn nguyên <p>/<li>. JSON "description" của trang đã bị
    # TopDev làm phẳng (các <li> dính liền nhau, verify 16/10/2026) -> chỉ là fallback.
    desc_parts: list[str] = []
    requirements = ""
    benefits: list[str] = []
    for m in re.finditer(
        r'([^<>]+)</span><div class="[^"]*\bmt-2"><div class="prose-ul[^"]*">(.*?)'
        r'</div></div>(?=<span|</div>)', h, re.S,
    ):
        heading = html_lib.unescape(m.group(1)).strip()
        text = _strip(m.group(2))
        if not text:
            continue
        low = heading.lower()
        if any(k in low for k in ("kỹ năng", "yêu cầu", "skill", "requirement",
                                  "qualification")):
            requirements = text[:2000]
        elif any(k in low for k in ("quyền lợi", "phúc lợi", "benefit")):
            benefits = text.split("\n")[:20]
        else:
            desc_parts.append(text if not desc_parts else f"{heading}:\n{text}")
    description = "\n".join(desc_parts)[:4000]
    if not description:
        for m in re.finditer(r'"description":"((?:[^"\\]|\\.)*)"', h):
            try:
                val = json.loads('"' + m.group(1) + '"')
            except json.JSONDecodeError:
                continue
            text = _strip(val)
            if len(text) > 100:
                description = text[:4000]
                break
    if not description:
        return None
    if not requirements:
        for m in re.finditer(r'"(?:requirements|skills_str)":"((?:[^"\\]|\\.)*)"', h):
            try:
                val = json.loads('"' + m.group(1) + '"')
            except json.JSONDecodeError:
                continue
            text = _strip(val)
            if len(text) > 40:
                requirements = text[:2000]
                break
    return {"description": description, "requirements": requirements,
            "benefits": benefits}
