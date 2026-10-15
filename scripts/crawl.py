#!/usr/bin/env python3
"""Chạy crawl pipeline: các adapter -> normalize -> dedupe -> SQLite.

Usage:
    python scripts/crawl.py [--db data/jobs.db] [--queries "content marketing,content creator"]
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_search_job.storage.store import JobStore
from agent_search_job.pipeline.ingest import IngestPipeline, DEFAULT_QUERIES
from agent_search_job.sources.vietnamworks import VietnamWorksAdapter
from agent_search_job.sources.jobspy_adapters import LinkedInAdapter
from agent_search_job.sources.itviec import ITviecAdapter
from agent_search_job.sources.careerviet import CareerVietAdapter
from agent_search_job.sources.topcv import TopCVAdapter


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--queries", default=",".join(DEFAULT_QUERIES))
    ap.add_argument("--max-results", type=int, default=30)
    ap.add_argument("--with-topcv", action="store_true",
                    help="Bật TopCV Playwright (chậm, ~15s/trang)")
    ap.add_argument("--with-linkedin", action="store_true", default=True)
    ap.add_argument("--no-linkedin", dest="with_linkedin", action="store_false")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    adapters = [VietnamWorksAdapter()]
    if args.with_linkedin:
        adapters.append(LinkedInAdapter())
    adapters.append(ITviecAdapter())
    adapters.append(CareerVietAdapter())
    if args.with_topcv:
        adapters.append(TopCVAdapter())

    store = JobStore(args.db)
    pipeline = IngestPipeline(store, adapters)
    queries = [q.strip() for q in args.queries.split(",") if q.strip()]
    result = await pipeline.run(queries=queries, max_results_per_query=args.max_results)
    print("\n=== CRAWL RESULT ===")
    import json
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    print("\nDB stats:", store.stats())


if __name__ == "__main__":
    asyncio.run(main())
