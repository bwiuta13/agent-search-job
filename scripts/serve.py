#!/usr/bin/env python3
"""Chạy website + MCP server.

Usage: python scripts/serve.py [--db data/jobs.db] [--port 8765]
MCP endpoint: http://localhost:8765/mcp (Bearer token = users.token trong DB)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    import os
    os.environ["JOBS_DB"] = args.db

    import uvicorn
    from agent_search_job.web.app import app
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
