#!/usr/bin/env python3
"""LLM enrichment chạy SAU ingest (theo docs/research-website.md mục 6).

Với mỗi job active HCM chưa có cache:
1. Extract field khó (platforms, content_types, tools, on_camera, KPI...)
2. Chấm relevance so với nhóm mục tiêu Content & Multimedia TP.HCM
Kết quả cache theo (job_id, model, prompt_version, kind) - chạy lại không tốn token.
LLM không cấu hình / lỗi -> job vẫn giữ data rule-based, không sập.

Usage:
    python scripts/llm_enrich.py [--db data/jobs.db] [--limit 50]
Cần env: OPENAI_API_KEY, AI_TEXT_MODEL, OPENAI_BASE_URL (xem .env.example)
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_search_job.pipeline.llm_enrich import LLMConfig, LLMEnricher, PROMPT_VERSION
from agent_search_job.storage.store import JobStore


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    cfg = LLMConfig.from_env()
    if not cfg.ok:
        print("OPENAI_API_KEY chưa cấu hình - bỏ qua LLM enrichment "
              "(dữ liệu rule-based vẫn đầy đủ).")
        return 0

    store = JobStore(args.db)
    enricher = LLMEnricher(cfg)
    jobs = store.jobs_needing_llm(cfg.model, PROMPT_VERSION, args.limit)
    print(f"Jobs cần enrich: {len(jobs)} (model={cfg.model}, prompt={PROMPT_VERSION})")

    ok = fail = 0
    for job in jobs:
        # 1) relevance
        rel = await enricher.score_relevance(job)
        if rel is not None:
            store.set_llm_cache(
                job["job_id"], cfg.model, PROMPT_VERSION, "relevance",
                {"relevance": rel.relevance, "reason": rel.reason},
            )
            ok += 1
        else:
            fail += 1
            continue
        # 2) extract field khó (chỉ job relevance >= 0.5 để tiết kiệm)
        if rel.relevance >= 0.5:
            ext = await enricher.extract(
                job["title"], job.get("description") or "",
                job.get("requirements") or "",
            )
            if ext is not None:
                store.set_llm_cache(
                    job["job_id"], cfg.model, PROMPT_VERSION, "extract",
                    {
                        "platforms": ext.platforms,
                        "content_types": ext.content_types,
                        "creative_tools": ext.creative_tools,
                        "on_camera_required": ext.on_camera_required,
                        "client_facing": ext.client_facing,
                        "agency_or_inhouse": ext.agency_or_inhouse,
                        "kpi_types": ext.kpi_types,
                        "languages": ext.languages,
                    },
                )
        status = "✓" if rel.relevance >= 0.5 else "–"
        print(f"  {status} [{job['job_id']}] {job['title'][:50]} "
              f"rel={rel.relevance:.1f} | {rel.reason[:60]}")

    print(f"\nEnriched: {ok} | LLM fail (bỏ qua): {fail}")
    print("DB stats:", store.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
