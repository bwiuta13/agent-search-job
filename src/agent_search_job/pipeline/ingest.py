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

# Query seeds cho Content & Marketing TP.HCM
DEFAULT_QUERIES = [
    "content marketing",
    "content creator",
    "social media",
    "digital marketing",
    "copywriter",
    "truyền thông",
]


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

        # 2) normalize
        for job in raw_jobs:
            try:
                normalize_job(job)
                result.normalized += 1
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"normalize: {job.title}: {e}")

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
