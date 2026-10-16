#!/usr/bin/env python3
"""One-off: sửa các row đã crawl trước khi normalize_job unescape HTML entity.

TopCV/VietnamWorks trả text còn "&amp;" nên web hiển thị "&amp;" thay vì "&".
Script unescape các cột text của jobs + companies.name và tính lại dedupe_key
(để crawl lần sau vẫn match row cũ thay vì insert trùng).

Mặc định chỉ in thay đổi (dry-run). Thêm --apply để ghi vào DB.
    python scripts/unescape_html_entities.py [--db data/jobs.db] [--apply]
"""
import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_search_job.domain.normalize import dedupe_key
from agent_search_job.storage.store import JobStore

TEXT_COLS = ("title", "company_name", "salary_text", "address", "city", "district",
             "description", "requirements")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--apply", action="store_true", help="ghi thay đổi (mặc định dry-run)")
    args = ap.parse_args()
    store = JobStore(args.db)
    conn = store.conn

    job_updates = []
    for row in conn.execute(f"SELECT job_id, dedupe_key, benefits, {', '.join(TEXT_COLS)} FROM jobs"):
        new = {c: html.unescape(row[c] or "") for c in TEXT_COLS}
        new["benefits"] = json.dumps([html.unescape(b) for b in json.loads(row["benefits"] or "[]")])
        new["dedupe_key"] = dedupe_key(new["title"], new["company_name"])
        changed = {c: v for c, v in new.items() if v != (row[c] or "")}
        if not changed:
            continue
        if "dedupe_key" in changed:
            clash = conn.execute("SELECT job_id FROM jobs WHERE dedupe_key=? AND job_id!=?",
                                 (new["dedupe_key"], row["job_id"])).fetchone()
            if clash:
                print(f"  ! job {row['job_id']}: dedupe_key mới trùng job {clash['job_id']}, giữ key cũ")
                del changed["dedupe_key"]
        job_updates.append((row["job_id"], changed))
        if "title" in changed or "company_name" in changed:
            print(f"job {row['job_id']}: {new['title']} | {new['company_name']}")
        else:
            print(f"job {row['job_id']}: {', '.join(changed)}")

    company_updates = []
    for row in conn.execute("SELECT company_id, name FROM companies"):
        name = html.unescape(row["name"])
        if name == row["name"]:
            continue
        if conn.execute("SELECT 1 FROM companies WHERE name=?", (name,)).fetchone():
            print(f"  ! company {row['company_id']}: '{name}' đã tồn tại, bỏ qua")
            continue
        company_updates.append((name, row["company_id"]))
        print(f"company {row['company_id']}: {name}")

    print(f"\n{len(job_updates)} jobs, {len(company_updates)} companies cần sửa.")
    if not args.apply:
        print("Dry-run: chưa ghi gì. Chạy lại với --apply để ghi.")
        return
    with conn:
        for job_id, changed in job_updates:
            sets = ", ".join(f"{c}=?" for c in changed)
            conn.execute(f"UPDATE jobs SET {sets} WHERE job_id=?", (*changed.values(), job_id))
        conn.executemany("UPDATE companies SET name=? WHERE company_id=?", company_updates)
    print("Đã ghi.")


if __name__ == "__main__":
    main()
