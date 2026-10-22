"""Pipeline: chạy các adapter song song, normalize, dedupe, upsert vào storage.

Theo docs/implementation-plan.md M2: một crawl run có thể resume, idempotent,
không sinh duplicate khi ingest cùng fixture hai lần.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from ..domain.models import NormalizedJob, RoleCluster, SourceName, utcnow
from ..domain.normalize import normalize_job, dedupe_key
from ..sources.base import DiscoverInput, JobSourceAdapter, SourceHealth
from ..storage.store import JobStore

log = logging.getLogger("pipeline")

# Query seeds - role list người dùng cần tìm (content/social/marketing/comms/
# copywriter/creative). KHÔNG còn "video editor"/"graphic design" thuần.
DEFAULT_QUERIES = [
    "content",
    "content creator",
    "social media",
    "copywriter",
    "content marketing",
    "marketing executive",
    "brand marketing",
    "truyền thông",
    "creative",
]

# Job ngoài phạm vi role list -> bỏ, không lưu (kể cả khi nguồn trả về):
# sales/telesales/tư vấn/performance/field/event marketing, HR, kế toán...
# 'video editor'/'graphic design' thuần cũng KHÔNG còn trong scope.
EXCLUDE_PATTERNS = [
    r"(?i)\btelesales?\b", r"(?i)\bbán hàng\b", r"(?i)\bsales?\b(?!\s*content)",
    r"(?i)\btư vấn", r"(?i)\bnhân viên kinh doanh\b",
    r"(?i)\bperformance marketing\b", r"(?i)\bfield marketing\b",
    r"(?i)\bevent executive\b", r"(?i)\btổ chức sự kiện\b",
    r"(?i)\bhr\b", r"(?i)\btuyển dụng\b", r"(?i)\bkế toán\b", r"(?i)\bthu ngân\b",
    r"(?i)\badmin\b",
]

# Bắt buộc: title/description phải có ít nhất 1 dấu hiệu role list người dùng
# (content, social, marketing executive/brand, comms, copywriter, creative)
INCLUDE_HINTS = [
    "content", "nội dung", "social", "truyền thông", "media", "pr ", "comms",
    "copywriter", "seo", "creative", "sáng tạo", "brand", "thương hiệu",
    "campaign", "chiến dịch", "kịch", "storyboard", "tiktok", "fanpage",
    "marcom", "editorial", "writer", "viết", "livestream", "blogger",
    "podcast", "marketing executive", "marketing specialist",
    "marketing online", "social content", "content creator",
]


def in_role_scope(title: str, description: str = "") -> bool:
    """True nếu job thuộc role list người dùng cần tìm (social media, content
    creator, content marketing, communication, marketing executive, brand
    marketing, content writer, copywriter, creative).

    KHÔNG còn video editor / graphic design / multimedia thuần trong scope.

    Ưu tiên: nếu TITLE có dấu hiệu role rõ (content/social/brand/comms...)
    -> thuộc phạm vi dù JD có từ ngữ bán hàng trong ngữ cảnh
    (vd 'Trưởng nhóm BH mảng video truyền thông' -> 'truyền thông' trong title).
    """
    import re
    t = (title or "").lower()
    text = f"{t} {(description or '')[:400]}".lower()
    # Title là video editor / thiết kế đồ họa thuần -> NGOÀI scope (role list
    # không có), KIỂM TRA TRƯỚC title hints vì vd 'Video Editor – Commercial
    # & Content' có chữ 'content' nhưng vẫn là job dựng video thuần.
    # Chỉ giữ nếu title còn nhắc role thật (marketing/creator/social/brand/
    # creative/truyền thông...) ở phần khác của title.
    if re.search(
        r"video\s*editor|editor\s*video|biên\s*tập\s*video|dựng\s*video|"
        r"videographer|compositor|motion\s*graph|graphic\s*design|"
        r"thiết\s*kế\s*đồ\s*họa|đồ\s*họa|multimedia|\bdesigner\b", t,
    ) and not re.search(
        r"creator|sáng\s*tạo|creative|marketing|social|brand|thương\s*hiệu|"
        r"truyền\s*thông|communication|copywriter|biên\s*kịch|content\s*marketing", t,
    ):
        return False
    # dấu hiệu role xuất hiện trong TITLE -> giữ (word boundary để
    # 'pr' trong 'Representative' không tính)
    title_hints = [
        "content", "nội dung", "social", "truyền thông", "media", "copywriter",
        "seo", "creative", "sáng tạo", "pr", "comms", "communication",
        "marcom", "brand", "thương hiệu", "writer", "viết", "creator",
        "tiktok", "fanpage",
    ]
    for h in title_hints:
        if re.search(r"(?<![a-zà-ỹ])" + re.escape(h) + r"(?![a-zà-ỹ])", t):
            return True
    for pat in EXCLUDE_PATTERNS:
        if re.search(pat, text[:300]):
            return False
    # hint cần ranh giới từ để tránh false positive ('designed' chứa 'design')
    boundary_hints = [
        "content", "nội dung", "social", "truyền thông", "media", "comms",
        "copywriter", "seo", "creative", "sáng tạo", "brand", "thương hiệu",
        "campaign", "chiến dịch", "kịch", "storyboard", "marcom",
        "editorial", "viết", "livestream", "live stream",
        "blogger", "podcast", "host",
    ]
    for h in boundary_hints:
        if re.search(r"(?<![a-zà-ỹ])" + re.escape(h) + r"(?![a-zà-ỹ])", text):
            return True
    # 'marketing'/'brand marketing' là role khi là CHỨC DANH nghề
    # (executive/specialist/manager/lead/...), không phải mảng bán hàng.
    # Performance/field marketing đã bị exclude chặn ở trên.
    if re.search(
        r"(?:marketing|brand marketing)[^,.]{0,25}"
        r"(?:executive|specialist|manager|lead|officer|associate|"
        r"strategist|coordinator|chuyên viên|nhân viên)",
        text,
    ):
        return True
    # 'dựng'/'video'/'design' thuần KHÔNG còn là hint riêng — video editor
    # thuần giờ ngoài scope; job dựng video chỉ còn vào scope khi là
    # content creator/social (đã match ở title/boundary hints trên).
    # 'xây dựng' không bao giờ đếm.
    # hint dạng cụm có sẵn (trailing space giữ ngữ cảnh): 'pr ', 'mc '
    return any(h in text for h in ("pr ", "mc "))


# Backward-compat: tên cũ (tests/CLI cũ vẫn import được)
in_multimedia_scope = in_role_scope


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
    out_of_hcm: int = 0
    # số job bị ẩn (is_active=0) cuối lượt quét: hết hạn nộp + N ngày không thấy lại
    hidden_expired: int = 0
    hidden_stale: int = 0
    # job_id của các job MỚI insert trong lượt này (mail job mới sau lượt quét định kỳ)
    inserted_job_ids: list[int] = field(default_factory=list)
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
            "out_of_hcm": self.out_of_hcm,
            "hidden_expired": self.hidden_expired,
            "hidden_stale": self.hidden_stale,
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

    @staticmethod
    def _is_hcm(job: NormalizedJob) -> bool:
        """Bắt buộc job phải ở TP.HCM - hard-code theo đặc thù từng nguồn.

        - vietnamworks/itviec: đã filter cityId/region HCM lúc discover (tin cậy)
        - linkedin: location tự do -> kiểm tra text có dấu hiệu HCM
        - topcv: search theo link HCM nhưng vẫn lọt job HN -> chặn theo text
        - topdev: region_ids=79 -> tin cậy, nhưng vẫn kiểm tra text phòng leak
        - vng: location "Thành phố Hồ Chí Minh" hoặc title ghi rõ (Bangkok/Jakarta
          là vị trí làm việc thực tế của role -> loại)
        """
        if job.source in ("vietnamworks", "itviec", "topdev"):
            # nguồn đã lọc HCM ở query; chỉ cần không có dấu hiệu nơi khác RÕ RỆNG
            text = f"{job.title} {job.address}".lower()
            other = ["hà nội", "hanoi", "đà nẵng", "da nang", "hải phòng",
                     "bangkok", "jakarta", "sydney", "singapore", "gò công",
                     "can tho", "cần thơ", "binh duong", "bình dương"]
            # trừ khi HCM cũng xuất hiện cùng lúc (ví dụ "Hà Nội; HCM")
            hcm = any(k in text for k in ("hồ chí minh", "ho chi minh", "hcm"))
            if hcm:
                return True
            return not any(o in text for o in other)
        # linkedin/topcv/vng/careerviet: phải có dấu hiệu HCM rõ
        text = f"{job.city} {job.address} {job.title} {job.description[:200]}".lower()
        hcm_signs = [
            "hồ chí minh", "ho chi minh", "hcm", "tp.hcm", "tp hcm",
            "sài gòn", "sai gon", "quận 1", "quận 3", "quận 7", "bình thạnh",
            "thủ đức", "gò vấp", "phú nhuận", "tân bình", "tân phú",
        ]
        if any(k in text for k in hcm_signs):
            return True
        # fallback: flag is_hcm do normalize_job tính từ địa chỉ gốc
        return bool(job.is_hcm)

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
        for h in result.health:
            if h.ok:
                log.info("%s: kết nối OK%s", h.source,
                         f" ({h.latency_ms} ms)" if h.latency_ms else "")
            else:
                log.warning("%s: health check lỗi — %s", h.source, h.error)

        crawled_sources: set[str] = set()  # nguồn quét được ít nhất 1 job lượt này
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
                    log.info("%s · %r: %d job", adapter.name, q, got)
                    if got:
                        crawled_sources.add(adapter.name)
                except Exception as e:  # noqa: BLE001 - adapter lỗi thì log, bỏ qua
                    msg = f"{adapter.name}: {type(e).__name__}: {e}"
                    log.warning("%s · %r lỗi — %s: %s", adapter.name, q,
                                type(e).__name__, str(e)[:200])
                    result.errors.append(msg)
        result.discovered = len(raw_jobs)

        # 2) normalize + lọc phạm vi: chỉ giữ job thuộc role list người dùng
        for job in raw_jobs:
            try:
                normalize_job(job)
                result.normalized += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"normalize: {job.title}: {e}")
        before_scope = len(raw_jobs)
        raw_jobs = [j for j in raw_jobs
                    if in_role_scope(j.title, j.description or "")]
        result.out_of_scope = before_scope - len(raw_jobs)

        # 3) HARD REQUIREMENT: TP.HCM - loại job không xác định được ở HCM
        # (hard-code theo nguồn: mỗi nguồn có cách ghi location khác nhau)
        before_hcm = len(raw_jobs)
        raw_jobs = [j for j in raw_jobs if self._is_hcm(j)]
        result.out_of_hcm = before_hcm - len(raw_jobs)

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
                self.store.update_job(existing, job, keep_inactive=True)
                result.updated += 1
                seen_keys[key] = existing
            else:
                jid = self.store.insert_job(job)
                seen_keys[key] = jid
                result.inserted += 1
                result.inserted_job_ids.append(jid)
        log.info("Lọc: %d job tìm thấy, %d ngoài phạm vi nghề, %d ngoài TP.HCM, "
                 "%d trùng → +%d mới, %d cập nhật", result.discovered, result.out_of_scope,
                 result.out_of_hcm, result.duplicates_skipped, result.inserted, result.updated)
        # mark inactive: job quá hạn deadline + job JOB_STALE_DAYS ngày không thấy lại
        # trên nguồn vừa quét (mặc định 30, 0 = tắt)
        stale_days = int(os.environ.get("JOB_STALE_DAYS", "30") or 0)
        hidden = self.store.mark_expired_inactive(
            stale_days=stale_days, stale_sources=tuple(sorted(crawled_sources)))
        result.hidden_expired = hidden["expired"]
        result.hidden_stale = hidden["stale"]
        if hidden["expired"] or hidden["stale"]:
            log.info("Ẩn %d job quá hạn nộp, %d job %d ngày không thấy lại trên nguồn",
                     hidden["expired"], hidden["stale"], stale_days)
        result.finished_at = utcnow()
        self.store.finish_crawl_run(result.run_id, result)
        return result
