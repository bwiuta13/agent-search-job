"""Pipeline: chạy các adapter song song, normalize, dedupe, upsert vào storage.

Theo docs/implementation-plan.md M2: một crawl run có thể resume, idempotent,
không sinh duplicate khi ingest cùng fixture hai lần.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from ..domain.models import NormalizedJob, RoleCluster, SourceName, utcnow
from ..domain.normalize import normalize_job, dedupe_key
from ..sources.base import DiscoverInput, JobSourceAdapter, SourceHealth
from ..storage.store import JobStore

log = logging.getLogger("pipeline")

# Query seeds - CHỈ Truyền thông đa phương tiện (content, social, video, design, PR, creative)
DEFAULT_QUERIES = [
    "content",
    "content creator",
    "social media",
    "copywriter",
    "video editor",
    "graphic design",
    "truyền thông",
    "creative",
]

# Job ngoài phạm vi Truyền thông đa phương tiện -> bỏ, không lưu (kể cả khi nguồn trả về)
# marketing tổng quát: performance/telesales/event/field marketing, sales, hành chính...
EXCLUDE_PATTERNS = [
    r"(?i)\btelesales?\b", r"(?i)\bbán hàng\b", r"(?i)\bsales?\b(?!\s*content)",
    r"(?i)\btư vấn", r"(?i)\bnhân viên kinh doanh\b",
    r"(?i)\bperformance marketing\b", r"(?i)\bfield marketing\b",
    r"(?i)\bevent executive\b", r"(?i)\btổ chức sự kiện\b",
    r"(?i)\bhr\b", r"(?i)\btuyển dụng\b", r"(?i)\bkế toán\b", r"(?i)\bthu ngân\b",
    r"(?i)\badmin\b",
]

# Bắt buộc: title/description phải có ít nhất 1 dấu hiệu Truyền thông đa phương tiện
INCLUDE_HINTS = [
    "content", "nội dung", "social", "truyền thông", "media", "pr ", "comms",
    "copywriter", "seo", "video", "dựng", "editor", "design", "thiết kế",
    "đồ họa", "graphic", "creative", "sáng tạo", "multimedia", "brand",
    "campaign", "chiến dịch", "kịch", "storyboard", "tiktok", "fanpage",
    "photographer", "chụp ảnh", "marcom", "editorial", "writer", "viết",
    "livestream", "live stream", "host", "mc ", "blogger", "podcast",
]


def in_multimedia_scope(title: str, description: str = "") -> bool:
    """True nếu job thuộc Truyền thông đa phương tiện; False với job marketing thuần khác.

    Ưu tiên: nếu TITLE có dấu hiệu truyền thông rõ (content/social/video/design...)
    -> thuộc phạm vi dù có từ 'bán hàng' trong ngữ cảnh (vd 'Trưởng nhóm BH mảng video').
    """
    import re
    t = (title or "").lower()
    text = f"{t} {(description or '')[:400]}".lower()
    # dấu hiệu truyền thông xuất hiện trong TITLE -> giữ (word boundary để
    # 'pr' trong 'Representative' không tính)
    import re as _re2
    title_hints = [
        "content", "nội dung", "social", "truyền thông", "media", "copywriter",
        "seo", "video", "dựng", "editor", "design", "thiết kế", "đồ họa",
        "graphic", "creative", "sáng tạo", "multimedia", "pr", "comms",
        "communication", "livestream", "host", "kịch", "photographer",
        "chụp ảnh", "marcom", "brand", "writer", "viết", "creator",
    ]
    for h in title_hints:
        if _re2.search(r"(?<![a-zà-ỹ])" + _re2.escape(h) + r"(?![a-zà-ỹ])", t):
            return True
    for pat in EXCLUDE_PATTERNS:
        if re.search(pat, text[:300]):
            return False
    # hint cần ranh giới từ để tránh false positive ('designed' chứa 'design')
    import re as _re
    boundary_hints = [
        "content", "nội dung", "social", "truyền thông", "media", "comms",
        "copywriter", "seo", "video", "dựng", "editor", "design", "thiết kế",
        "đồ họa", "graphic", "creative", "sáng tạo", "multimedia", "brand",
        "campaign", "chiến dịch", "kịch", "storyboard", "tiktok", "fanpage",
        "photographer", "chụp ảnh", "marcom", "editorial", "writer", "viết",
        "livestream", "live stream", "blogger", "podcast", "host", "mc",
    ]
    for h in boundary_hints:
        if _re.search(r"(?<![a-zà-ỹ])" + _re.escape(h) + r"(?![a-zà-ỹ])", text):
            return True
    # hint dạng cụm có sẵn (trailing space giữ ngữ cảnh): 'pr ', 'mc '
    return any(h in text for h in ("pr ", "mc "))


@dataclass
class CrawlRunResult:
    run_id: int = 0
    started_at: object = None
    finished_at: object = None
    discovered: int = 0
    normalized: int = 0
    inserted: int = 0
    updated: int = 0
    duplicates_skipped: int = 0
    out_of_scope: int = 0
    errors: list[str] = field(default_factory=list)
    health: list[SourceHealth] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "discovered": self.discovered,
            "normalized": self.normalized,
            "inserted": self.inserted,
            "updated": self.updated,
            "duplicates_skipped": self.duplicates_skipped,
            "out_of_scope": self.out_of_scope,
            "errors": self.errors,
            "health": [
                {
                    "source": h.source, "ok": h.ok,
                    "latency_ms": h.latency_ms, "error": h.error,
                } for h in self.health
            ],
        }


class IngestPipeline:
    def __init__(self, store: JobStore, adapters: list[JobSourceAdapter]):
        self.store = store
        self.adapters = adapters

    async def run(
        self,
        queries: Optional[list[str]] = None,
        max_results_per_query: int = 50,
    ) -> CrawlRunResult:
        queries = queries or DEFAULT_QUERIES
        result = CrawlRunResult(started_at=utcnow())
        result.run_id = self.store.begin_crawl_run(queries)

        # 1) discover song song từng adapter
        raw_jobs: list[NormalizedJob] = []
        health_tasks = []
        for adapter in self.adapters:
            health_tasks.append(adapter.health_check())
        result.health = await asyncio.gather(*health_tasks)

        for adapter in self.adapters:
            for q in queries:
                try:
                    got = 0
                    async for job in adapter.discover(DiscoverInput(
                        query=q, location="Ho Chi Minh",
                        max_results=max_results_per_query,
                    )):
                        raw_jobs.append(job)
                        got += 1
                        if got >= max_results_per_query:
                            break
                except Exception as e:  # noqa: BLE001 - adapter lỗi thì log, bỏ qua
                    msg = f"{adapter.name}: {type(e).__name__}: {e}"
                    log.warning("Adapter error: %s", msg)
                    result.errors.append(msg)
        result.discovered = len(raw_jobs)

        # 2) normalize + lọc phạm vi: chỉ giữ job Truyền thông đa phương tiện
        for job in raw_jobs:
            try:
                normalize_job(job)
                result.normalized += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"normalize: {job.title}: {e}")
        before_scope = len(raw_jobs)
        raw_jobs = [j for j in raw_jobs
                    if in_multimedia_scope(j.title, j.description or "")]
        result.out_of_scope = before_scope - len(raw_jobs)

        # 3) dedupe + upsert (idempotent: cùng dedupe_key -> update, không insert mới)
        seen_keys: dict[str, int] = {}
        for job in raw_jobs:
            if not job.title or not job.company_name:
                # không có title/company thì bỏ (validator theo docs M2)
                continue
            key = dedupe_key(job.title, job.company_name)
            if key in seen_keys:
                result.duplicates_skipped += 1
                continue
            existing = self.store.find_by_dedupe_key(key)
            if existing:
                self.store.update_job(existing, job)
                result.updated += 1
                seen_keys[key] = existing
            else:
                jid = self.store.insert_job(job)
                seen_keys[key] = jid
                result.inserted += 1
        # mark inactive: job quá hạn deadline
        self.store.mark_expired_inactive()
        result.finished_at = utcnow()
        self.store.finish_crawl_run(result.run_id, result)
        return result
