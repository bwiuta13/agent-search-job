"""Scheduled scan: chạy định kỳ mỗi N giờ — quét nguồn, xoá job hết hạn/ngừng
tuyển, gửi mail job mới.

Trách nhiệm một lượt scan (run_scheduled_scan):
1. Quét mọi nguồn (IngestPipeline — nút bấm "Quét ngay" trên web chạy cùng code):
   - job quá hạn nộp + job JOB_STALE_DAYS ngày không thấy lại trên nguồn vừa
     quét bị ẩn (is_active=0) ngay cuối lượt quét (logic cũ của pipeline).
2. Xoá hẳn job đã ẩn quá SCAN_DELETE_AFTER_DAYS ngày (mặc định 3) — kế thừa
   logic scripts/purge.py: backup DB vào backups/ trước khi xoá, giữ job có
   người lưu, xoá kèm cache AI của job đó.
3. Gửi mail "N việc làm mới" tới MAIL_TO nếu cấu hình SMTP (pipeline/mail_notify).

Vòng lặp định kỳ (ScheduledScanManager):
- SCHEDULED_SCAN_ENABLED=1 bật vòng lặp chạy trong tiến trình server web
  (lifespan FastAPI, mỗi SCAN_INTERVAL_HOURS=12 giờ, mặc định chạy ngay lúc
  khởi động).
- Không chồng chéo: đang quét thì lượt định kỳ tới bị bỏ qua (RescanManager
  cũng chặn "Quét ngay" đang chạy).
- scripts/scheduled_scan.py chạy một lượt rồi thoát (cron/Compose không cần
  server sống; web chạy thì loop trong server giữ lịch luôn đúng).

Lịch sử từng lượt lưu trong bảng scheduled_scans (scan_id, started_at,
finished_at, result_json) — xem bằng `python scripts/scheduled_scan.py --status`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from ..domain.models import utcnow
from ..storage.store import JobStore

log = logging.getLogger("pipeline.scheduled_scan")

DEFAULT_INTERVAL_HOURS = 12


def interval_hours() -> int:
    try:
        v = int(os.environ.get("SCAN_INTERVAL_HOURS") or DEFAULT_INTERVAL_HOURS)
    except ValueError:
        v = DEFAULT_INTERVAL_HOURS
    return max(1, v)


def scheduled_enabled() -> bool:
    """True khi vòng lặp trong server được bật (SCHEDULED_SCAN_ENABLED)."""
    v = os.environ.get("SCHEDULED_SCAN_ENABLED", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return False


# ---------------------------------------------------------------------------
# Lịch sử scan trong DB (bảng scheduled_scans)
# ---------------------------------------------------------------------------

_SCAN_TABLE = """
CREATE TABLE IF NOT EXISTS scheduled_scans (
    scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER NOT NULL DEFAULT 0,
    result_json TEXT DEFAULT '{}'
)
"""


def _ensure_scan_table(store: JobStore) -> None:
    """Tạo bảng scheduled_scans nếu chưa có (chỉ đọc sqlite_master trước để
    tránh DDL gây tranh ghi mỗi lần gọi từ thread khác)."""
    have = store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_scans'"
    ).fetchone()
    if have:
        return
    with store.conn:
        store.conn.executescript(_SCAN_TABLE)


def record_scan_start(store: JobStore) -> int:
    _ensure_scan_table(store)
    with store.conn:
        cur = store.conn.execute(
            "INSERT INTO scheduled_scans (started_at, ok) VALUES (?, 0)",
            (utcnow().isoformat(),),
        )
        return cur.lastrowid


def record_scan_end(store: JobStore, scan_id: int, result: dict) -> None:
    _ensure_scan_table(store)
    with store.conn:
        store.conn.execute(
            "UPDATE scheduled_scans SET finished_at=?, ok=?, result_json=? WHERE scan_id=?",
            (utcnow().isoformat(), int(bool(result.get("ok"))),
             json.dumps(result, ensure_ascii=False), scan_id),
        )


def last_scans(store: JobStore, limit: int = 10) -> list[dict]:
    _ensure_scan_table(store)
    rows = store.conn.execute(
        "SELECT * FROM scheduled_scans ORDER BY scan_id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["result"] = json.loads(d.get("result_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            d["result"] = {}
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Quét + xoá + mail — một lượt scan
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    ok: bool = True
    at: str = ""
    run_id: int = 0
    discovered: int = 0
    inserted: int = 0
    updated: int = 0
    out_of_scope: int = 0
    out_of_hcm: int = 0
    duplicates_skipped: int = 0
    details_fetched: int = 0
    errors: list[str] = field(default_factory=list)
    hidden: int = 0            # bị ẩn cuối lượt quét (hết hạn/ngừng tuyển)
    deleted: dict = field(default_factory=dict)  # purge báo xoá bao nhiêu dòng
    new_jobs: list[dict] = field(default_factory=list)  # job mới cho mail
    mail: dict = field(default_factory=dict)     # trạng thái gửi mail

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "at": self.at,
            "run_id": self.run_id,
            "discovered": self.discovered,
            "inserted": self.inserted,
            "updated": self.updated,
            "out_of_scope": self.out_of_scope,
            "out_of_hcm": self.out_of_hcm,
            "duplicates_skipped": self.duplicates_skipped,
            "details_fetched": self.details_fetched,
            "errors": self.errors,
            "hidden": self.hidden,
            "deleted": self.deleted,
            "new_jobs": self.new_jobs,
            "mail": self.mail,
        }


def _adapters(with_topcv: bool = False):
    """Danh sách adapter cho lượt quét định kỳ — dùng DEFAULT_QUERIES đầy đủ."""
    from ..sources.vietnamworks import VietnamWorksAdapter
    from ..sources.itviec import ITviecAdapter
    from ..sources.careerviet import CareerVietAdapter
    from ..sources.topdev import TopDevAdapter
    from ..sources.vng_careers import VNGCareersAdapter
    from ..sources.jobspy_adapters import LinkedInAdapter

    adapters = [VietnamWorksAdapter()]
    if os.environ.get("SCAN_LINKEDIN", "1").strip().lower() not in ("0", "false", "no", "off"):
        adapters.append(LinkedInAdapter())
    adapters += [ITviecAdapter(), CareerVietAdapter(), TopDevAdapter(), VNGCareersAdapter()]
    if with_topcv:
        from ..sources.topcv import TopCVAdapter
        adapters.append(TopCVAdapter())
    return adapters


async def run_scheduled_scan(store: JobStore, with_details: bool = True,
                             with_topcv: bool = False,
                             notify: bool = True) -> ScanResult:
    """Một lượt quét định kỳ: crawl -> ẩn hết hạn -> xoá dữ liệu chết -> mail job mới.

    Không raise: mọi lỗi được nuốt vào result (lượt cron kế tiếp vẫn chạy).
    """
    from .ingest import IngestPipeline, DEFAULT_QUERIES
    from .rescan import enrich_job_details
    from .mail_notify import notify_new_jobs

    res = ScanResult(at=utcnow().isoformat())
    scan_id = record_scan_start(store)
    try:
        pipeline = IngestPipeline(store, _adapters(with_topcv))
        result = await pipeline.run(queries=list(DEFAULT_QUERIES),
                                    max_results_per_query=30)
        res.run_id = result.run_id
        res.discovered, res.inserted = result.discovered, result.inserted
        res.updated, res.out_of_scope = result.updated, result.out_of_scope
        res.out_of_hcm = result.out_of_hcm
        res.duplicates_skipped = result.duplicates_skipped
        res.hidden = result.hidden_expired + result.hidden_stale
        res.errors = result.errors[:20]

        if with_details:
            n = 0
            for src in ("itviec", "careerviet", "topdev"):
                try:
                    n += await enrich_job_details(store, src, limit=40)
                except Exception as e:  # noqa: BLE001
                    log.warning("JD details %s lỗi: %s", src, e)
            res.details_fetched = n

        # job mới cho mail: job MỚI insert trong lượt này (pipeline ghi job_id),
        # active, mới nhất trước — job bị ẩn ngay (hết hạn) không vào mail
        res.new_jobs = _jobs_by_ids(store, result.inserted_job_ids, limit=50)

        # xoá hẳn job đã ẩn lâu (data chết) — dùng logic purge của scripts/purge.py
        res.deleted = _purge_hidden(store)

        if notify and res.new_jobs:
            site_url = os.environ.get("SITE_URL", "").strip().rstrip("/")
            summary = {
                "at": res.at, "inserted": res.inserted, "updated": res.updated,
                "hidden": res.hidden, "deleted": _total_deleted(res.deleted),
                "errors": len(res.errors),
            }
            ok, msg = notify_new_jobs(res.new_jobs, summary, site_url)
            res.mail = {"sent": ok, "message": msg, "count": len(res.new_jobs)}
        else:
            res.mail = {"sent": False, "message": "Không có job mới" if not res.new_jobs
                        else "Tắt gửi mail (—no-notify)", "count": len(res.new_jobs)}
        res.ok = True
    except Exception as e:  # noqa: BLE001 - lượt scan lỗi không được làm sập vòng lặp
        res.ok = False
        res.errors = res.errors + [f"scan: {type(e).__name__}: {e}"]
        log.error("Lượt quét định kỳ lỗi: %s: %s", type(e).__name__, e)
    record_scan_end(store, scan_id, res.to_dict())
    log.info("Lượt quét định kỳ xong: +%d mới, %d cập nhật, %d bị ẩn, xoá %s, mail: %s",
             res.inserted, res.updated, res.hidden,
             _total_deleted(res.deleted) or 0,
             res.mail.get("message", "—"))
    return res


def _jobs_by_ids(store: JobStore, job_ids: list[int], limit: int = 50) -> list[dict]:
    """Job theo danh sách job_id (mới insert trong lượt quét), chỉ job còn active."""
    if not job_ids:
        return []
    ids = list(dict.fromkeys(job_ids))[-limit:]  # mới nhất trước, bỏ trùng
    qs = ",".join("?" * len(ids))
    rows = store.conn.execute(
        f"""SELECT job_id, title, company_name, salary_text, salary_min_vnd,
                   salary_max_vnd, district, role_cluster, job_level,
                   apply_url, source_url, first_seen_at
            FROM jobs WHERE job_id IN ({qs}) AND is_active=1
            ORDER BY job_id DESC""",
        ids,
    ).fetchall()
    return [dict(r) for r in rows]


def _total_deleted(deleted: dict) -> int:
    return int(deleted.get("jobs") or 0)


def _purge_hidden(store: JobStore) -> dict:
    """Xoá hẳn job đã ẩn quá SCAN_DELETE_AFTER_DAYS ngày — giống scripts/purge.py
    (backup trước, giữ job có người lưu, xoá cache AI mồ côi) nhưng chạy trong
    tiến trình hiện có: backup bằng SQLite backup API, KHÔNG VACUUM (tránh khoá
    DB lâu trên server đang chạy).
    """
    from .purge_store import purge_stale_data

    try:
        days = int(os.environ.get("SCAN_DELETE_AFTER_DAYS") or 3)
    except ValueError:
        days = 3
    if days <= 0:
        return {"jobs": 0, "note": "SCAN_DELETE_AFTER_DAYS<=0: tắt xoá"}
    return purge_stale_data(store, hidden_days=days)


# ---------------------------------------------------------------------------
# Vòng lặp định kỳ chạy trong server web (SCHEDULED_SCAN_ENABLED=1)
# ---------------------------------------------------------------------------

class ScheduledScanManager:
    """Giữ vòng lặp quét định kỳ trong server — 1 lượt 1 lúc, không chồng chéo."""

    _task: Optional[asyncio.Task] = None
    _running: bool = False
    last_result: Optional[ScanResult] = None

    @classmethod
    def running(cls) -> bool:
        return cls._running

    @classmethod
    def status(cls) -> dict:
        return {
            "enabled": scheduled_enabled(),
            "interval_hours": interval_hours(),
            "running": cls._running,
            "last": cls.last_result.to_dict() if cls.last_result else None,
        }

    @classmethod
    async def start_loop(cls, store: JobStore) -> Optional[asyncio.Task]:
        """Khởi động vòng lặp nếu bật. Trả về task hoặc None nếu tắt."""
        if not scheduled_enabled():
            log.info("Quét định kỳ tắt (SCHEDULED_SCAN_ENABLED!=1) — dùng cron/scripts nếu cần")
            return None
        if cls._task is not None and not cls._task.done():
            return cls._task
        cls._task = asyncio.create_task(cls._loop(store))
        return cls._task

    @classmethod
    async def _loop(cls, store: JobStore):
        hours = interval_hours()
        log.info("Quét định kỳ bật: mỗi %d giờ (+ chạy ngay 1 lượt lúc khởi động)", hours)
        # chạy ngay 1 lượt lúc khởi động? SCAN_ON_STARTUP=0 để chỉ chạy theo lịch
        if os.environ.get("SCAN_ON_STARTUP", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Bỏ lượt quét lúc khởi động (SCAN_ON_STARTUP=0) — lượt đầu sau %d giờ", hours)
            await asyncio.sleep(hours * 3600)
        while True:
            await cls._run_once(store)
            await asyncio.sleep(hours * 3600)

    @classmethod
    async def _run_once(cls, store: JobStore):
        if cls._running:
            log.warning("Lượt quét trước chưa xong — bỏ lượt định kỳ này")
            return
        cls._running = True
        try:
            # with_details theo env (SCAN_JD_DETAILS, mặc định 1); test đặt 0
            details = os.environ.get("SCAN_JD_DETAILS", "1").strip().lower() \
                not in ("0", "false", "no", "off")
            cls.last_result = await run_scheduled_scan(store, with_details=details)
        finally:
            cls._running = False
