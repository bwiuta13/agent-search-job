"""Rescan: người dùng chủ động quét lại nguồn tùy thích từ web/MCP.

Theo thiết kế docs/research-website.md: crawl chạy NGOÀI tool call (không chờ
trong request). Ở đây local single-user nên chạy background task FastAPI
(BackgroundTasks/asyncio.create_task) + trạng thái lưu trong bảng crawl_runs
kết quả trả ngay {status: "started"}.

MCP tool là read-only theo docs - rescan chỉ expose qua REST/web (nút bấm),
không đưa vào MCP để đúng ràng buộc "no crawling inside tool calls".
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..domain.models import utcnow
from ..storage.store import JobStore

log = logging.getLogger("pipeline.rescan")
# logger được gom vào log rescan hiển thị trên web: pipeline + fetcher của mình, JobSpy
# (JobSpy tự đặt propagate=False nên phải gắn handler trực tiếp)
_LOG_SOURCES = ("pipeline", "agent_search_job", "JobSpy:LinkedIn", "JobSpy:Indeed")
_MAX_LOG = 500


@dataclass
class RescanStatus:
    running: bool
    started_at: Optional[str] = None
    source: str = ""
    message: str = ""
    finished_at: Optional[str] = None
    ok: bool = True
    log: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


class _RescanLogHandler(logging.Handler):
    """Ghi log của lượt quét vào RescanStatus.log để web poll hiển thị trực tiếp."""

    def __init__(self, status: RescanStatus):
        super().__init__(logging.INFO)
        self.status = status

    def emit(self, record: logging.LogRecord):
        if len(self.status.log) >= _MAX_LOG:
            return
        self.status.log.append({
            "t": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname.lower(),
            "msg": record.getMessage()[:400],
        })


class RescanManager:
    """Quản lý rescan chạy nền - 1 job 1 lúc, tránh chồng chéo crawl."""

    _current: Optional[RescanStatus] = None
    _task: Optional[asyncio.Task] = None

    @classmethod
    def status(cls) -> RescanStatus:
        st = cls._current
        if cls._task is not None and cls._task.done():
            cls._task = None
            if st is not None and st.running:
                st.running = False
                st.message = st.message or "Hoàn tất"
        return st or RescanStatus(False)

    @classmethod
    async def start(cls, store: JobStore, source: str = "all",
                    query: str = "marketing", max_results: int = 15,
                    with_details: bool = True) -> RescanStatus:
        if cls._task is not None and not cls._task.done():
            return RescanStatus(True, cls._current.started_at if cls._current else None,
                                cls._current.source if cls._current else "",
                                "Một lượt quét đang chạy - thử lại sau khi xong")
        st = RescanStatus(True, utcnow().isoformat(), source, "Đang quét...")
        cls._current = st
        cls._task = asyncio.create_task(
            _run_rescan(store, source, query, max_results, with_details)
        )
        return st


async def _run_rescan(store: JobStore, source: str, query: str,
                      max_results: int, with_details: bool):
    """Chạy IngestPipeline cho các nguồn được chọn trong background."""
    from ..pipeline.ingest import DEFAULT_QUERIES, IngestPipeline
    from ..sources.vietnamworks import VietnamWorksAdapter
    from ..sources.itviec import ITviecAdapter
    from ..sources.careerviet import CareerVietAdapter
    from ..sources.jobspy_adapters import LinkedInAdapter
    st = RescanManager._current
    try:
        import jobspy  # noqa: F401 - import trước để JobSpy tự tạo logger + handler console
    except ImportError:
        pass
    handler = _RescanLogHandler(st) if st else None
    loggers = [logging.getLogger(n) for n in _LOG_SOURCES]
    old_levels = [lg.level for lg in loggers]
    if handler:
        for lg in loggers:
            lg.addHandler(handler)
            if lg.getEffectiveLevel() > logging.INFO:
                lg.setLevel(logging.INFO)
    try:
        log.info("Bắt đầu quét nguồn %s · từ khóa %r · tối đa %d job/từ khóa",
                 "mọi nguồn" if source == "all" else source, query, max_results)
        adapters = []
        if source in ("all", "vietnamworks"):
            adapters.append(VietnamWorksAdapter())
        if source in ("all", "itviec"):
            adapters.append(ITviecAdapter())
        if source in ("all", "careerviet"):
            adapters.append(CareerVietAdapter())
        if source in ("all", "linkedin"):
            adapters.append(LinkedInAdapter())
        if source in ("all", "topdev"):
            from ..sources.topdev import TopDevAdapter
            adapters.append(TopDevAdapter())
        if source in ("all", "vng"):
            from ..sources.vng_careers import VNGCareersAdapter
            adapters.append(VNGCareersAdapter())
        if source in ("all", "topcv"):
            from ..sources.topcv import TopCVAdapter
            adapters.append(TopCVAdapter())
        pipeline = IngestPipeline(store, adapters)
        # từ khoá người dùng gõ quét trước, rồi đủ role list (DEFAULT_QUERIES) để không sót nhóm nghề nào
        result = await pipeline.run(queries=list(dict.fromkeys([query, *DEFAULT_QUERIES])),
                                    max_results_per_query=max_results)
        n = 0
        if with_details:
            n = await enrich_job_details(store, source)
            log.info("Bổ sung JD chi tiết: cập nhật %d job", n)
        message = (f"Hoàn tất: +{result.inserted} việc mới, {result.updated} cập nhật"
                   + (f", bổ sung JD {n} job" if n else "")
                   + (f", {len(result.errors)} lỗi" if result.errors else ""))
        log.info(message)
        if st:
            st.message, st.ok = message, not result.errors
            st.summary = {"discovered": result.discovered, "inserted": result.inserted,
                          "updated": result.updated, "details": n,
                          "errors": len(result.errors)}
    except Exception as e:  # noqa: BLE001 - background task phải nuốt lỗi
        message = f"Lỗi quét: {type(e).__name__}: {e}"[:300]
        log.error(message)
        if st:
            st.message, st.ok = message, False
    finally:
        if handler:
            for lg, lv in zip(loggers, old_levels):
                lg.removeHandler(handler)
                lg.setLevel(lv)
        if st:
            st.running = False
            st.finished_at = utcnow().isoformat()


async def enrich_job_details(store: JobStore, source: str, limit: int = 50):
    """Fetch JD đầy đủ cho job chưa có description (hard-code selector từng nguồn)."""
    from ..domain.normalize import normalize_job
    from ..sources.detail_fetchers import fetch_detail

    where = "description IS NULL OR LENGTH(description) < 200"
    if source != "all":
        where += f" AND source = '{source}'"
    rows = store.conn.execute(
        f"SELECT job_id, source, source_url FROM jobs WHERE ({where}) AND is_active=1 "
        "AND source_url != '' ORDER BY job_id DESC LIMIT ?", (limit,),
    ).fetchall()
    if rows:
        log.info("Lấy JD chi tiết cho %d job còn thiếu JD", len(rows))
    updated = 0
    for r in rows:
        try:
            detail = await fetch_detail(r["source"], r["source_url"])
        except Exception as e:  # noqa: BLE001
            log.warning("%s · job #%d: lỗi lấy JD (%s: %s)", r["source"], r["job_id"],
                        type(e).__name__, str(e)[:120])
            continue
        if detail is None:
            log.warning("%s · job #%d: không lấy được JD từ trang chi tiết",
                        r["source"], r["job_id"])
            continue
        with store.conn:
            store.conn.execute(
                """UPDATE jobs SET description=?, requirements=?, benefits=?
                   WHERE job_id=?""",
                (detail.description or "", detail.requirements or "",
                 __import__("json").dumps(detail.benefits), r["job_id"]),
            )
        updated += 1
    return updated
