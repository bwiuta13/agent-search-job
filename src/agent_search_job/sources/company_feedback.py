"""Nguồn feedback/review công ty - mỗi nguồn kèm provenance + confidence.

Theo docs/research-job-data-sources.md mục 4:
- ITviec /companies/<slug>/review: aggregate public (điểm tổng, 5 sub-score,
  % recommend) - nguồn review chính cho công ty IT, không cần login.
- Indeed rating: có sẵn khi search job qua JobSpy.
- Không suy diễn rating từ quảng bá của công ty; thiếu dữ liệu -> unavailable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import httpx

from ..domain.models import CompanyFeedback

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class FeedbackResult:
    feedback: Optional[CompanyFeedback] = None
    status: str = "unavailable"  # unavailable | ok
    note: str = ""


class ITviecReviewSource:
    """Aggregate review từ trang /companies/<slug>/review của ITviec."""

    name = "itviec"
    base = "https://itviec.com"

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    async def get_company_review(self, slug: str) -> FeedbackResult:
        url = f"{self.base}/companies/{slug}/review"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers={"User-Agent": UA},
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return FeedbackResult(
                    status="unavailable",
                    note=f"ITviec trả HTTP {resp.status_code} cho {url}",
                )
            fb = self._parse_review_page(resp.text, url)
            if fb is None:
                return FeedbackResult(
                    status="unavailable",
                    note=f"Chưa parse được rating từ {url} (công ty có thể chưa có review)",
                )
            return FeedbackResult(feedback=fb, status="ok")
        except Exception as e:  # noqa: BLE001 - nguồn lỗi thì unavailable, không sập
            return FeedbackResult(
                status="unavailable", note=f"{type(e).__name__}: {e}"
            )

    def _parse_review_page(self, html: str, url: str) -> Optional[CompanyFeedback]:
        """Parse trang review ITviec. Format thật (verify 15/10/2026):
        'Overall rating 3.7 2206 reviews' + sub-scores kèm 'x reviews'.
        Trang 404/redirect không có khối này -> trả None (không đoán)."""
        from datetime import datetime, timezone
        # Bắt buộc có khối "Overall rating" rồi số X và "N reviews" sau đó
        # (HTML thật: rating cách Overall ~330 ký tự, count cách ~2600 - tag giữa)
        from datetime import datetime, timezone
        m = re.search(r"Overall\s+rating", html)
        if not m:
            return None
        tail = html[m.end(): m.end() + 4000]
        text = re.sub(r"<[^>]+>", " ", tail)
        text = re.sub(r"\s+", " ", text)
        mm = re.search(r"([0-5](?:\.[0-9])?)\s*(?:out of 5)?\s*([\d,]+)\s+reviews", text)
        if not mm:
            return None
        rating = float(mm.group(1))
        try:
            count = int(mm.group(2).replace(",", ""))
        except ValueError:
            count = None
        # sub-scores nằm trong cùng đoạn text đã strip tag
        subs: dict[str, float] = {}
        sub_tail = text[mm.end(): mm.end() + 2000]
        for label, pat in [
            ("van_phong", r"(?:Văn phòng|Office)\s+([0-5](?:\.[0-9])?)"),
            ("van_hoa", r"(?:Văn hóa|Culture)\s+([0-5](?:\.[0-9])?)"),
            ("luong_thuong", r"(?:Lương thưởng|Salary)\s+([0-5](?:\.[0-9])?)"),
            ("quan_ly", r"(?:Quản lý|Management)\s+([0-5](?:\.[0-9])?)"),
            ("phat_trien", r"(?:Phát triển|Career)\s+([0-5](?:\.[0-9])?)"),
        ]:
            sm = re.search(pat, sub_tail)
            if sm:
                subs[label] = float(sm.group(1))
        summary = ""
        if subs:
            summary = "Điểm thành phần: " + ", ".join(f"{k}={v}" for k, v in subs.items())
        confidence = "high" if (count or 0) >= 100 else "medium" if (count or 0) >= 20 else "low"
        return CompanyFeedback(
            company_id=0,  # caller gán sau
            source=self.name,
            source_url=url,
            rating=rating,
            review_count=count,
            summary=summary,
            fetched_at=datetime.now(timezone.utc),
            confidence=confidence,
        )


async def get_company_feedback(company_name: str, itviec_slug: str = "") -> FeedbackResult:
    """Entry: lấy feedback công ty. MVP: ITviec; nguồn khác bổ sung sau."""
    slug = itviec_slug or re.sub(r"[^a-z0-9-]", "", company_name.lower().strip().replace(" ", "-"))
    if not slug:
        return FeedbackResult(status="unavailable", note="Thiếu tên công ty")
    src = ITviecReviewSource()
    return await src.get_company_review(slug)
