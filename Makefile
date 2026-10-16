# make start            -> chạy web + MCP ở http://localhost:8765 (Ctrl+C để dừng)
# make start PORT=8770  -> đổi cổng
# make purge-dry-run    -> xem trước số job/lịch sử quét cũ sẽ bị xoá (không xoá gì)
# make purge            -> backup data/backups/ rồi xoá job đã ẩn > 90 ngày + crawl_runs cũ
PORT ?= 8765
DB ?= data/jobs.db

.PHONY: start purge purge-dry-run
start:
	python3 scripts/serve.py --db $(DB) --port $(PORT)

purge:
	python3 scripts/purge.py --db $(DB)

purge-dry-run:
	python3 scripts/purge.py --db $(DB) --dry-run
