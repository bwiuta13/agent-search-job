# make start            -> chạy web + MCP ở http://localhost:8765 (Ctrl+C để dừng)
# make start PORT=8770  -> đổi cổng
PORT ?= 8765
DB ?= data/jobs.db

.PHONY: start
start:
	python3 scripts/serve.py --db $(DB) --port $(PORT)
