"""Trợ lý AI (/assistant): lịch sử, hồ sơ/CV, agent SSE + tool, dọn dữ liệu — chạy offline.

LLM + Tavily giả bằng FakeHTTP (thay httpx.AsyncClient của module agent), giống TestNLQuery.
"""
from __future__ import annotations

import copy
import io
import json
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agent_search_job.domain.models import NormalizedJob, utcnow
from agent_search_job.domain.normalize import normalize_job


@pytest.fixture
def app(monkeypatch):
    """App với DB :memory: mới, 2 user được mời, LLM có key (giả), chưa có Tavily."""
    monkeypatch.setenv("JOBS_DB", ":memory:")
    monkeypatch.setenv("ALLOWLIST_EMAILS", "a@example.com,b@example.com")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    for k in ("ASSISTANT_CHAT_TTL_HOURS", "ASSISTANT_DAILY_MESSAGES", "ASSISTANT_DAILY_WEB_SEARCHES"):
        monkeypatch.delenv(k, raising=False)
    import agent_search_job.web.app as appmod
    appmod._store = None
    appmod._mcp = None
    return appmod


@pytest.fixture
def public_app(app, monkeypatch):
    monkeypatch.setenv("ALLOWLIST_EMAILS", "")
    return app


def login(appmod, email="a@example.com") -> TestClient:
    client = TestClient(appmod.app)
    code = appmod.get_store().conn.execute(
        "SELECT token FROM users WHERE email=?", (email,)).fetchone()["token"]
    r = client.post("/login", data={"email": email, "code": code}, follow_redirects=False)
    assert r.status_code == 302
    return client


def uid(appmod, email="a@example.com") -> int:
    return appmod.get_store().conn.execute(
        "SELECT user_id FROM users WHERE email=?", (email,)).fetchone()["user_id"]


def add_conv(appmod, owner, title="Hỏi về content", hours_ago=0) -> int:
    store = appmod.get_store()
    ts = (utcnow() - timedelta(hours=hours_ago)).isoformat()
    with store.conn:
        cid = store.conn.execute(
            "INSERT INTO chat_conversations(owner, title, created_at, updated_at) VALUES (?,?,?,?)",
            (owner, title, ts, ts)).lastrowid
        store.conn.execute(
            "INSERT INTO chat_messages(conversation_id, role, content, created_at) VALUES (?,?,?,?)",
            (cid, "user", title, ts))
    return cid


class TestHistoryAndProfile:
    def test_conversations_are_private_per_user(self, app):
        a, b = login(app, "a@example.com"), login(app, "b@example.com")
        cid = add_conv(app, f"user:{uid(app, 'a@example.com')}")
        assert [c["id"] for c in a.get("/api/assistant/conversations").json()["items"]] == [cid]
        assert b.get("/api/assistant/conversations").json()["items"] == []
        assert b.get(f"/api/assistant/conversations/{cid}").status_code == 404
        assert b.delete(f"/api/assistant/conversations/{cid}").status_code == 404
        conv = a.get(f"/api/assistant/conversations/{cid}").json()
        assert conv["messages"][0]["content"] == "Hỏi về content"
        assert a.delete(f"/api/assistant/conversations/{cid}").json() == {"deleted": True}
        assert a.get("/api/assistant/conversations").json()["items"] == []

    def test_guest_needs_cookie_and_sees_only_own_chats(self, public_app):
        assert TestClient(public_app.app).get("/api/assistant/conversations").status_code == 401
        guest = TestClient(public_app.app, cookies={"vnjobs_guest": "g" * 32})
        other = add_conv(public_app, "guest:" + "h" * 32)
        mine = add_conv(public_app, "guest:" + "g" * 32)
        ids = [c["id"] for c in guest.get("/api/assistant/conversations").json()["items"]]
        assert ids == [mine] and other not in ids

    def test_chats_and_guest_profiles_expire_after_ttl(self, app):
        client = login(app)
        owner = f"user:{uid(app)}"
        old, fresh = add_conv(app, owner, "cũ", hours_ago=25), add_conv(app, owner, "mới", hours_ago=1)
        store = app.get_store()
        old_ts = (utcnow() - timedelta(hours=30)).isoformat()
        with store.conn:
            store.conn.execute("INSERT INTO user_profiles(owner, about, updated_at) VALUES (?,?,?)",
                               (owner, "3 năm content", old_ts))
            store.conn.execute("INSERT INTO user_profiles(owner, about, updated_at) VALUES (?,?,?)",
                               ("guest:" + "x" * 32, "khách", old_ts))
        ids = [c["id"] for c in client.get("/api/assistant/conversations").json()["items"]]
        assert ids == [fresh]
        assert store.conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE conversation_id=?", (old,)).fetchone()[0] == 0
        owners = [r[0] for r in store.conn.execute("SELECT owner FROM user_profiles")]
        assert owners == [owner]  # hồ sơ user đăng nhập giữ lại, hồ sơ khách cũ bị xoá

    def test_ttl_env_is_respected(self, app, monkeypatch):
        monkeypatch.setenv("ASSISTANT_CHAT_TTL_HOURS", "2")
        client = login(app)
        add_conv(app, f"user:{uid(app)}", hours_ago=3)
        assert client.get("/api/assistant/conversations").json()["items"] == []

    def test_startup_sweep_purges_without_requests(self, app):
        add_conv(app, "user:1", hours_ago=48)
        with TestClient(app.app):  # chạy lifespan: dọn ngay khi khởi động
            pass
        assert app.get_store().conn.execute("SELECT COUNT(*) FROM chat_conversations").fetchone()[0] == 0

    def test_profile_put_get_delete(self, app):
        client = login(app)
        empty = client.get("/api/assistant/profile").json()
        assert empty == {"about": "", "cv_filename": None, "cv_chars": 0, "updated_at": None}
        r = client.put("/api/assistant/profile", json={"about": "  3 năm content TikTok  "})
        assert r.status_code == 200 and r.json()["about"] == "3 năm content TikTok"
        assert client.get("/api/assistant/profile").json()["about"] == "3 năm content TikTok"
        assert client.put("/api/assistant/profile", json={"about": "x" * 4001}).status_code == 400
        assert client.delete("/api/assistant/profile").json() == {"deleted": True}
        assert client.get("/api/assistant/profile").json()["about"] == ""

    def test_cross_origin_writes_are_rejected(self, app):
        client = login(app)
        r = client.put("/api/assistant/profile", json={"about": "x"},
                       headers={"origin": "https://evil.example"})
        assert r.status_code == 403
