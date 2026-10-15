#!/usr/bin/env python3
"""In MCP bearer token cho email đã mời (để test MCP client)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from agent_search_job.storage.store import JobStore

db = sys.argv[1] if len(sys.argv) > 1 else "data/jobs.db"
store = JobStore(db)
for row in store.conn.execute("SELECT email, token FROM users").fetchall():
    print(f"{row['email']}: {row['token']}")
