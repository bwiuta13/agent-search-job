#!/usr/bin/env python3
"""Chạy MỘT lượt quét định kỳ rồi thoát: quét nguồn -> ẩn/xoá job hết hạn
hoặc không còn tuyển -> gửi mail job mới.

Dùng cho cron / Compose / systemd-timer khi không bật vòng lặp trong server
(SCHEDULED_SCAN_ENABLED). Lịch quét định kỳ 12 tiếng một trong hai cách:

1) Trong server web (khuyên dùng, một tiến trình duy nhất):
   .env:  SCHEDULED_SCAN_ENABLED=1  SCAN_INTERVAL_HOURS=12
   -> vòng lặp chạy trong lifespan FastAPI (kèm web + MCP).

2) Cron ngoài (server không chạy web):
   crontab -e
   0 */12 * * * cd /path/to/agent-search-job && python3 scripts/scheduled_scan.py >> data/scheduled-scan.log 2>&1

Usage:
    python scripts/scheduled_scan.py                    # chạy 1 lượt (mặc định)
    python scripts/scheduled_scan.py --db data/jobs.db
    python scripts/scheduled_scan.py --no-details       # không fetch JD chi tiết
    python scripts/scheduled_scan.py --no-notify        # không gửi mail
    python scripts/scheduled_scan.py --status           # xem 10 lượt gần nhất
    python scripts/scheduled_scan.py --test-mail        # gửi mail thử (kiểm tra cấu hình SMTP)
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_search_job.storage.store import JobStore


def main():
    ap = argparse.ArgumentParser(description="Quét định kỳ: crawl -> xoá job chết -> mail job mới")
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--no-details", action="store_true",
                    help="Không fetch JD chi tiết từ trang nguồn (nhanh hơn)")
    ap.add_argument("--no-notify", action="store_true", help="Không gửi mail job mới")
    ap.add_argument("--with-topcv", action="store_true",
                    help="Bật TopCV (Playwright, chậm ~15s/trang)")
    ap.add_argument("--status", action="store_true", help="Xem N lượt quét gần nhất rồi thoát")
    ap.add_argument("--test-mail", action="store_true",
                    help="Gửi mail thử tới MAIL_TO để kiểm tra cấu hình SMTP rồi thoát")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

    from agent_search_job.pipeline.scheduled_scan import run_scheduled_scan, last_scans
    from agent_search_job.pipeline.mail_notify import send_mail

    if args.test_mail:
        ok, msg = send_mail(
            "[Agent Search Job] Mail thử — cấu hình SMTP hoạt động",
            "<p>Mail thử từ <code>scripts/scheduled_scan.py --test-mail</code>: "
            "nếu bạn nhận được mail này thì cấu hình SMTP trong .env đã đúng.</p>",
            "Mail thử từ scripts/scheduled_scan.py --test-mail: "
            "nếu bạn nhận được mail này thì cấu hình SMTP trong .env đã đúng.",
        )
        print(("OK: " if ok else "LỖI: ") + msg)
        return 0 if ok else 1

    store = JobStore(args.db)

    if args.status:
        scans = last_scans(store, limit=10)
        if not scans:
            print("Chưa có lượt quét định kỳ nào.")
            return 0
        for s in scans:
            r = s.get("result") or {}
            print(f"#{s['scan_id']} {s['started_at']} "
                  f"{'OK' if s.get('ok') else 'LỖI'} "
                  f"+{r.get('inserted', 0)} mới, {r.get('updated', 0)} cập nhật, "
                  f"{r.get('hidden', 0)} bị ẩn, xoá {(r.get('deleted') or {}).get('jobs', 0)}, "
                  f"mail: {(r.get('mail') or {}).get('message', '—')}")
        return 0

    res = asyncio.run(run_scheduled_scan(
        store, with_details=not args.no_details,
        with_topcv=args.with_topcv, notify=not args.no_notify))
    print("\n=== SCHEDULED SCAN RESULT ===")
    print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    print("\nDB stats:", store.stats())
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
