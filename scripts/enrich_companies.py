#!/usr/bin/env python3
"""Enrich công ty: upsert companies từ jobs + lấy feedback ITviec.

Chạy sau crawl. Với mỗi company_name xuất hiện trong jobs:
1. Upsert vào bảng companies (MVP: từ tên, không gọi thêm API ngoài).
2. Thử lấy feedback ITviec review theo slug tên công ty (fail-safe).
"""
import asyncio
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_search_job.storage.store import JobStore
from agent_search_job.domain.models import Company
from agent_search_job.sources.company_feedback import get_company_feedback


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", name.lower()).strip()
    return re.sub(r"\s+", "-", s)


async def main(db: str = "data/jobs.db"):
    logging.basicConfig(level=logging.WARNING)
    store = JobStore(db)
    names = [r["company_name"] for r in store.conn.execute(
        "SELECT DISTINCT company_name FROM jobs WHERE company_name != ''"
    ).fetchall()]
    print(f"Found {len(names)} distinct companies")
    feedback_ok = 0
    for name in names:
        # 1) upsert company
        comp = Company(name=name)
        existing = store.find_company_by_name(name)
        cid = store.upsert_company(comp) if not existing else existing["company_id"]
        # link jobs -> company
        with store.conn:
            store.conn.execute(
                "UPDATE jobs SET company_id=? WHERE company_name LIKE ?",
                (cid, f"%{name}%"),
            )
        # 2) feedback ITviec (bỏ qua tên pháp lý dài/brands rõ ràng không phải IT)
        if any(k in name.lower() for k in ("công ty", "cty", "tnhh", "jsc")):
            slug = _slugify(name.split(".", 2)[-1].strip()) if "." in name else _slugify(name)
        else:
            slug = _slugify(name)
        try:
            res = await get_company_feedback(name, itviec_slug=slug)
            if res.status == "ok" and res.feedback:
                res.feedback.company_id = cid
                store.save_feedback(res.feedback)
                feedback_ok += 1
                print(f"  ✓ feedback: {name} -> rating={res.feedback.rating} count={res.feedback.review_count}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ feedback error {name}: {e}")
    print(f"\nCompanies upserted: {len(names)}, feedback sources saved: {feedback_ok}")
    print("DB stats:", store.stats())


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "data/jobs.db"))
