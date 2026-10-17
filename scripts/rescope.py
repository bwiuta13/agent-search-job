#!/usr/bin/env python3
"""Re-scope jobs.db theo role list mới (content/social/marketing/brand/comms/
copywriter/creative — KHÔNG còn video editor/graphic design thuần).

Việc làm:
1. Backup DB vào <thư mục DB>/backups/ (bắt buộc trước khi đụng dữ liệu).
2. Ẩn (is_active=0) mọi job active mà title+description KHÔNG còn thuộc
   scope mới (in_role_scope) — job đã lưu (saved_jobs) vẫn được ẩn vì đã
   lệch nhu cầu, nhưng được báo cáo rõ.
3. Chạy lại classify_role cho mọi job còn active: cập nhật role_cluster +
   confidence theo taxonomy mới (video_production/multimedia_design không
   còn; brand_marketing mới). Cluster cũ được map sang cluster mới.

Usage:
  python scripts/rescope.py --db data/jobs.db --dry-run   # chỉ xem số sẽ đổi
  python scripts/rescope.py --db data/jobs.db             # backup rồi đổi thật
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_search_job.domain.normalize import classify_role  # noqa: E402
from agent_search_job.pipeline.ingest import in_role_scope  # noqa: E402

# map cluster cũ -> cluster mới cho job giữ được scope nhưng title không đủ
# tín hiệu mới (vd 'Video Content Producer' -> creative_production)
OLD_CLUSTER_MAP = {
    "video_production": "creative_production",
    "multimedia_design": "creative_production",
}


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description="Re-scope jobs.db theo role list mới")
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--dry-run", action="store_true", help="chỉ in, không đổi DB")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT job_id, title, company_name, description, role_cluster, is_active "
        "FROM jobs WHERE is_active=1"
    ).fetchall()

    to_hide, reclassify = [], []
    for r in rows:
        title = r["title"] or ""
        desc = r["description"] or ""
        if not in_role_scope(title, desc):
            to_hide.append((r["job_id"], title, r["role_cluster"]))
            continue
        new_cluster, conf = classify_role(title, desc)
        old = r["role_cluster"]
        if new_cluster != old:
            reclassify.append((new_cluster, round(conf, 2), r["job_id"], title, old))

    by_old = {}
    for _jid, _t, c in to_hide:
        by_old[c] = by_old.get(c, 0) + 1
    by_new = {}
    for nc, _conf, _jid, _t, _old in reclassify:
        by_new[nc] = by_new.get(nc, 0) + 1

    print(f"Tổng job active: {len(rows)}")
    print(f"Sẽ ẨN (ngoài scope mới): {len(to_hide)} — theo cluster cũ: {by_old}")
    for jid, t, c in to_hide[:15]:
        print(f"  [{c}] {t!r}")
    if len(to_hide) > 15:
        print(f"  ... và {len(to_hide) - 15} job khác")
    print(f"Sẽ RECLASSIFY: {len(reclassify)} — theo cluster mới: {by_new}")
    for nc, conf, jid, t, old in reclassify[:15]:
        print(f"  {old} -> {nc} ({conf}): {t!r}")
    if len(reclassify) > 15:
        print(f"  ... và {len(reclassify) - 15} job khác")

    report = {"active": len(rows), "hide": len(to_hide), "reclassify": len(reclassify),
              "hide_by_old_cluster": by_old, "reclassify_to": by_new, "backup": None}
    if args.dry_run or not (to_hide or reclassify):
        conn.close()
        return report

    backup_dir = Path(args.db).resolve().parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"{Path(args.db).stem}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
    dst = sqlite3.connect(backup)
    conn.backup(dst)
    dst.close()
    report["backup"] = str(backup)
    print(f"Đã backup: {backup}")

    with conn:
        if to_hide:
            marks = ",".join("?" * len(to_hide))
            conn.execute(
                f"UPDATE jobs SET is_active=0 WHERE job_id IN ({marks})",
                [j[0] for j in to_hide])
        for new_cluster, conf, jid, _t, _old in reclassify:
            conn.execute(
                "UPDATE jobs SET role_cluster=?, role_cluster_confidence=? WHERE job_id=?",
                (new_cluster, conf, jid))
    conn.close()
    print("Đã cập nhật xong.")
    return report


if __name__ == "__main__":
    main()
