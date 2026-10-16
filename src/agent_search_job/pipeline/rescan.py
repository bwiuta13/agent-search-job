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
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..domain.models import utcnow
from ..storage.store import JobStore


@dataclass
class RescanStatus:
    running: bool
    started_at: Optional[str] = None
    source: str = ""
    message: str = ""


class RescanManager:
    """Quản lý rescan chạy nền - 1 job 1 lúc, tránh chồng chéo crawl."""

    _current: Optional[RescanStatus] = None
    _task: Optional[asyncio.Task] = None

    @classmethod
    def status(cls) -> RescanStatus:
        st = cls._current
        if cls._task is not None and cls._task.done():
            cls._task = None
            if st is not None:
                return RescanStatus(False, st.started_at, st.source,
                                    st.message or "Hoàn tất")
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
    from ..pipeline.ingest import IngestPipeline
    from ..sources.vietnamworks import VietnamWorksAdapter
    from ..sources.itviec import ITviecAdapter
    from ..sources.careerviet import CareerVietAdapter
    from ..sources.jobspy_adapters import LinkedInAdapter
    try:
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
        pipeline = IngestPipeline(store, adapters)
        await pipeline.run(queries=[query, "content", "marketing"],
                           max_results_per_query=max_results)
        if with_details:
            await enrich_job_details(store, source)
    except Exception as e:  # noqa: BLE001 - background task phải nuốt lỗi
        RescanManager._current = RescanStatus(
            False, RescanManager._current.started_at if RescanManager._current else None,
            source, f"Lỗi quét: {type(e).__name__}: {e}"[:300],
        )
    else:
        if RescanManager._current:
            RescanManager._current.running = False
            RescanManager._current.message = "Hoàn tất"


async def enrich_job_details(store: JobStore, source: str, limit: int = 50):
    """Fetch JD đầy đủ cho job chưa có description (hard-code selector từng nguồn)."""
    from ..domain.normalize import normalize_job
    from ..sources.detail_fetchers import fetch_detail

    where = "description IS NULL OR LENGTH(description) < 200"
    if source != "all":
        where += f" AND source = '{source}'"
    rows = store.conn.execute(
        f"SELECT job_id, source, source_url FROM jobs WHERE ({where}) AND is_active=1 "
        "ORDER BY job_id DESC LIMIT ?", (limit,),
    ).fetchall()
    updated = 0
    for r in rows:
        try:
            detail = await fetch_detail(r["source"], r["source_url"])
        except Exception:  # noqa: BLE001
            detail = None
        if detail is None:
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
