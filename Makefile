# make start            -> chạy web + MCP ở http://localhost:8765 (Ctrl+C để dừng)
#                          SCHEDULED_SCAN_ENABLED=1 (mặc định trong .env.example):
#                          vòng lặp quét định kỳ SCAN_TIMES (08:00, 20:00 giờ VN) chạy cùng server
# make start PORT=8770  -> đổi cổng
# make scan             -> chạy 1 lượt quét định kỳ (quét -> xoá job chết -> mail job mới)
# make scan-now         -> alias của make scan
# make scan-status      -> xem 10 lượt quét định kỳ gần nhất
# make test-mail        -> gửi mail thử kiểm tra cấu hình SMTP
# make purge-dry-run    -> xem trước số job/lịch sử quét cũ sẽ bị xoá (không xoá gì)
# make purge            -> backup data/backups/ rồi xoá job đã ẩn > 90 ngày + crawl_runs cũ
PORT ?= 8765
DB ?= data/jobs.db

.PHONY: start scan scan-now scan-status test-mail purge purge-dry-run
start:
	python3 scripts/serve.py --db $(DB) --port $(PORT)

scan: scan-now

scan-now:
	python3 scripts/scheduled_scan.py --db $(DB)

scan-status:
	python3 scripts/scheduled_scan.py --db $(DB) --status

test-mail:
	python3 scripts/scheduled_scan.py --db $(DB) --test-mail

purge:
	python3 scripts/purge.py --db $(DB)

purge-dry-run:
	python3 scripts/purge.py --db $(DB) --dry-run
