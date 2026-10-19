#!/usr/bin/env python3
"""Dọn dữ liệu cũ trong jobs.db.

Chiến lược dọn dữ liệu:
1. (tự động, cuối mỗi lần quét) ẩn job quá hạn nộp + job JOB_STALE_DAYS ngày (30)
   không thấy lại trên nguồn vừa quét - xem IngestPipeline.run.
2. (script này) xoá hẳn job đã ẩn quá JOB_PURGE_AFTER_DAYS ngày (90), tính từ mốc muộn
   nhất giữa lần cuối thấy trên nguồn (last_verified_at) và hạn nộp. Bỏ qua job có
   người đã lưu (saved_jobs). Xoá kèm cache AI (llm_enrichment) của job đó.
3. (script này) xoá crawl_runs cũ hơn CRAWL_RUNS_KEEP_DAYS ngày (90), rồi VACUUM.
   Bản backup bỏ hội thoại trợ lý AI (chat_conversations/chat_messages) và hồ sơ khách (user_profiles owner 'guest:%').
Công ty + feedback giữ nguyên. Trước khi xoá thật luôn backup DB vào <thư mục DB>/backups/.

Usage:
  python scripts/purge.py --db data/jobs.db --dry-run   # chỉ xem số dòng sẽ xoá
  python scripts/purge.py --db data/jobs.db             # backup rồi xoá thật
"""
import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main(argv=None) -> dict:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

    ap = argparse.ArgumentParser(description="Dọn job đã ẩn lâu + lịch sử quét cũ")
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--dry-run", action="store_true", help="chỉ in số dòng sẽ xoá")
    ap.add_argument("--hidden-days", type=int,
                    default=int(os.environ.get("JOB_PURGE_AFTER_DAYS") or 90))
    ap.add_argument("--runs-days", type=int,
                    default=int(os.environ.get("CRAWL_RUNS_KEEP_DAYS") or 90))
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    job_cut = (now - timedelta(days=args.hidden_days)).isoformat()
    runs_cut = (now - timedelta(days=args.runs_days)).isoformat()
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys=ON")

    old_hidden = """FROM jobs WHERE is_active=0 AND last_verified_at < ?
                    AND (deadline IS NULL OR deadline < ?)"""
    job_ids = [r[0] for r in conn.execute(
        f"SELECT job_id {old_hidden} AND job_id NOT IN (SELECT job_id FROM saved_jobs)",
        (job_cut, job_cut))]
    saved_kept = conn.execute(
        f"SELECT COUNT(*) {old_hidden} AND job_id IN (SELECT job_id FROM saved_jobs)",
        (job_cut, job_cut)).fetchone()[0]
    marks = ",".join("?" * len(job_ids)) or "NULL"
    # cache AI của job sắp xoá + cache mồ côi (job đã mất từ trước)
    cache_where = f"job_id IN ({marks}) OR job_id NOT IN (SELECT job_id FROM jobs)"
    report = {
        "jobs": len(job_ids),
        "saved_kept": saved_kept,
        "llm_cache": conn.execute(
            f"SELECT COUNT(*) FROM llm_enrichment WHERE {cache_where}", job_ids).fetchone()[0],
        "crawl_runs": conn.execute(
            "SELECT COUNT(*) FROM crawl_runs WHERE started_at < ?", (runs_cut,)).fetchone()[0],
        "backup": None,
    }
    print(f"{'[dry-run] ' if args.dry_run else ''}Job đã ẩn quá {args.hidden_days} ngày: "
          f"{report['jobs']} sẽ xoá ({saved_kept} job có người lưu được giữ lại); "
          f"cache AI: {report['llm_cache']}; crawl_runs quá {args.runs_days} ngày: "
          f"{report['crawl_runs']}")
    if args.dry_run or not (report["jobs"] or report["llm_cache"] or report["crawl_runs"]):
        conn.close()
        return report

    backup_dir = Path(args.db).resolve().parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"{Path(args.db).stem}-{now:%Y%m%d-%H%M%S}.db"
    dst = sqlite3.connect(backup)
    conn.backup(dst)  # backup API: gồm cả dữ liệu còn trong WAL
    # backup không giữ hội thoại trợ lý AI và hồ sơ khách (chỉ sống ASSISTANT_CHAT_TTL_HOURS giờ)
    if dst.execute("SELECT 1 FROM sqlite_master WHERE name='chat_messages'").fetchone():
        with dst:
            dst.execute("DELETE FROM chat_messages")
            dst.execute("DELETE FROM chat_conversations")
            dst.execute("DELETE FROM user_profiles WHERE owner LIKE 'guest:%'")
        dst.execute("VACUUM")
    dst.close()
    report["backup"] = str(backup)
    print(f"Đã backup: {backup}")

    with conn:
        conn.execute(f"DELETE FROM llm_enrichment WHERE {cache_where}", job_ids)
        conn.execute(f"DELETE FROM jobs WHERE job_id IN ({marks})", job_ids)
        conn.execute("DELETE FROM crawl_runs WHERE started_at < ?", (runs_cut,))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.close()
    print("Đã xoá xong và VACUUM.")
    return report


if __name__ == "__main__":
    main()
