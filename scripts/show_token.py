#!/usr/bin/env python3
"""In mã truy cập cho từng email đã mời: dùng để đăng nhập web và làm MCP bearer token.

Mã ngẫu nhiên (secrets.token_urlsafe) - gửi riêng cho từng người, không đăng công khai.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from agent_search_job.storage.store import JobStore

db = sys.argv[1] if len(sys.argv) > 1 else "data/jobs.db"
store = JobStore(db)
for row in store.conn.execute("SELECT email, token FROM users").fetchall():
    print(f"{row['email']}: {row['token']}")
