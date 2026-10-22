"""Xoá dữ liệu job chết cho scheduled scan — cùng logic scripts/purge.py
nhưng chạy trong tiến trình có sẵn (server web), không cần subprocess.

Khác scripts/purge.py:
- backup DB bằng SQLite backup API (gồm cả dữ liệu trong WAL) — không dùng
  VACUUM để không khoá DB lâu trên server đang phục vụ web.
- không xoá crawl_runs (dọn riêng bằng make purge định kỳ; scheduled scan
  giữ nhẹ và nhanh).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..storage.store import JobStore

log = logging.getLogger("pipeline.purge")


def purge_stale_data(store: JobStore, hidden_days: int = 3,
                     backup: bool = True) -> dict:
    """Xoá hẳn job đã ẩn quá hidden_days ngày (trừ job có người lưu) + cache AI
    của job đó + cache AI mồ côi. Backup DB trước khi xoá nếu backup=True.

    Trả dict báo cáo {"jobs": N, "llm_cache": N, "saved_kept": N, "backup": path}.
    """
    conn = store.conn
    now = datetime.now(timezone.utc)
    job_cut = (now - timedelta(days=hidden_days)).isoformat()

    old_hidden = """FROM jobs WHERE is_active=0 AND last_verified_at < ?
                    AND (deadline IS NULL OR deadline < ?)"""
    job_ids = [r[0] for r in conn.execute(
        f"SELECT job_id {old_hidden} AND job_id NOT IN (SELECT job_id FROM saved_jobs)",
        (job_cut, job_cut))]
    saved_kept = conn.execute(
        f"SELECT COUNT(*) {old_hidden} AND job_id IN (SELECT job_id FROM saved_jobs)",
        (job_cut, job_cut)).fetchone()[0]
    marks = ",".join("?" * len(job_ids)) or "NULL"
    cache_where = f"job_id IN ({marks}) OR job_id NOT IN (SELECT job_id FROM jobs)"
    llm_cache = conn.execute(
        f"SELECT COUNT(*) FROM llm_enrichment WHERE {cache_where}", job_ids).fetchone()[0]

    report = {"jobs": len(job_ids), "llm_cache": llm_cache,
              "saved_kept": saved_kept, "backup": None}
    if not (report["jobs"] or llm_cache):
        return report

    if backup:
        try:
            backup_path = _backup(conn, store, now)
            report["backup"] = str(backup_path)
            log.info("Backup trước khi xoá: %s", backup_path)
        except sqlite3.Error as e:
            # backup lỗi -> KHÔNG xoá (an toàn dữ liệu hơn là xoá không backup)
            log.error("Backup DB lỗi nên bỏ qua lượt xoá: %s", e)
            report["jobs"] = report["llm_cache"] = 0
            report["error"] = f"backup: {e}"
            return report

    with conn:
        conn.execute(f"DELETE FROM llm_enrichment WHERE {cache_where}", job_ids)
        conn.execute(f"DELETE FROM jobs WHERE job_id IN ({marks})", job_ids)
    log.info("Đã xoá %d job đã ẩn quá %d ngày (+%d cache AI, giữ %d job có người lưu)",
             report["jobs"], hidden_days, llm_cache, saved_kept)
    return report


def _backup(conn: sqlite3.Connection, store: JobStore, now: datetime) -> Path:
    """Backup DB hiện tại vào <thư mục DB>/backups/ (SQLite backup API: gồm cả WAL)."""
    db_path = getattr(store, "db_path", "data/jobs.db")
    # db_path có thể là URI memory (test) -> không backup file
    if db_path.startswith("file:"):
        return Path("memory.db")
    backup_dir = Path(db_path).resolve().parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    path = backup_dir / f"{Path(db_path).stem}-{now:%Y%m%d-%H%M%S}.db"
    dst = sqlite3.connect(path)
    try:
        conn.backup(dst)
    finally:
        dst.close()
    return path
