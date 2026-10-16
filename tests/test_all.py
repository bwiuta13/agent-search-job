"""Tests: normalize (salary, location, role cluster), dedupe key, storage,
MCP contracts, web API và MCP protocol - chạy không cần mạng (fixtures).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_search_job.domain.models import NormalizedJob, RoleCluster, WorkMode, utcnow
from agent_search_job.domain.normalize import (
    classify_role, dedupe_key, is_hcm, normalize_job, parse_district,
    parse_experience, parse_salary,
)
from agent_search_job.mcp.contracts import SearchFilters, ValidationError
from agent_search_job.storage.store import JobStore


# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------

def make_job(**kw) -> NormalizedJob:
    defaults = dict(
        title="Content Marketing Specialist",
        company_name="Công Ty TNHH ABC",
        salary_text="15 - 25 triệu",
        address="123 Nguyễn Huệ, Quận 1, TP.HCM",
        city="Hồ Chí Minh",
        description="Làm content TikTok, Facebook. Dùng Canva, CapCut.",
        requirements="2 năm kinh nghiệm content marketing",
        source="vietnamworks",
        source_url="https://example.com/job/1",
        source_job_id="1",
    )
    defaults.update(kw)
    return NormalizedJob(**defaults)


# ---------------------------------------------------------------------------
# Normalize tests
# ---------------------------------------------------------------------------

class TestSalary:
    def test_range_trieu(self):
        lo, hi, txt = parse_salary("15 - 25 triệu")
        assert lo == 15_000_000 and hi == 25_000_000

    def test_range_with_dot(self):
        lo, hi, _ = parse_salary("2.5 - 3.5 triệu")
        assert lo == 2_500_000 and hi == 3_500_000

    def test_from(self):
        lo, hi, _ = parse_salary("Từ 8 triệu")
        assert lo == 8_000_000 and hi is None

    def test_above(self):
        lo, hi, _ = parse_salary("Trên 20 triệu")
        assert lo == 20_000_000

    def test_negotiable(self):
        lo, hi, _ = parse_salary("Thoả thuận")
        assert lo is None and hi is None

    def test_vnd_raw(self):
        lo, hi, _ = parse_salary("20000000 VND")
        assert lo == 20_000_000

    def test_garbage(self):
        lo, hi, txt = parse_salary("lương hấp dẫn")
        assert lo is None and hi is None and txt == "lương hấp dẫn"


class TestExperience:
    def test_range(self):
        assert parse_experience("2-3 năm kinh nghiệm") == (2.0, 3.0)

    def test_plus(self):
        assert parse_experience("trên 2 năm") == (2.0, None)

    def test_fresher(self):
        assert parse_experience("Fresher được đào tạo") == (0.0, None)

    def test_none(self):
        assert parse_experience("không ghi") == (None, None)


class TestLocation:
    def test_is_hcm_variants(self):
        assert is_hcm("TP.HCM")
        assert is_hcm("Hồ Chí Minh")
        assert is_hcm("Ho Chi Minh City")
        assert is_hcm("Sài Gòn")

    def test_not_hcm(self):
        assert not is_hcm("Hà Nội")
        assert not is_hcm("Đà Nẵng")

    def test_district(self):
        assert parse_district("123 Lê Lợi, Quận 1") == "Quận 1"
        assert parse_district("Gò Vấp, TP.HCM") == "Gò Vấp"
        assert parse_district("Bình Thạnh") == "Bình Thạnh"


class TestRoleCluster:
    def test_content_marketing(self):
        c, conf = classify_role("Content Marketing Executive")
        assert c == RoleCluster.CONTENT_MARKETING.value and conf >= 0.5

    def test_video(self):
        c, _ = classify_role("Video Editor - Dựng video TikTok")
        assert c == RoleCluster.VIDEO_PRODUCTION.value

    def test_social(self):
        c, _ = classify_role("Social Media Specialist")
        assert c == RoleCluster.SOCIAL_MEDIA.value

    def test_pr(self):
        c, _ = classify_role("Chuyên viên Truyền thông - PR")
        assert c == RoleCluster.COMMUNICATIONS_PR.value

    def test_generic_fallback(self):
        c, conf = classify_role("Nhân viên Marketing")
        assert c == RoleCluster.CONTENT_MARKETING.value


class TestNormalizeJob:
    def test_full_pipeline(self):
        job = normalize_job(make_job())
        assert job.salary_min_vnd == 15_000_000
        assert job.salary_max_vnd == 25_000_000
        assert job.is_hcm
        assert job.district == "Quận 1"
        assert job.role_cluster == RoleCluster.CONTENT_MARKETING.value
        assert "tiktok" in job.platforms and "facebook" in job.platforms
        assert "canva" in job.creative_tools and "capcut" in job.creative_tools
        assert job.experience_years_min == 2.0

    def test_html_entities_unescaped(self):
        # TopCV/VietnamWorks trả "&amp;" trong title/company/description
        store = JobStore(":memory:")
        job = normalize_job(make_job(
            title="Social Media Leader | AI &amp; AUTOMATION",
            company_name="CÔNG TY TNHH TM&amp;DV JAMU JMC",
            description="Focus: Beauty &amp; Personal Care &#64;brand",
            benefits=["Lương &amp; thưởng"],
        ))
        assert job.title == "Social Media Leader | AI & AUTOMATION"
        assert job.company_name == "CÔNG TY TNHH TM&DV JAMU JMC"
        assert job.description == "Focus: Beauty & Personal Care @brand"
        assert job.benefits == ["Lương & thưởng"]
        jid = store.insert_job(job)
        # fingerprint lấy từ text đã unescape: crawl lại bản "&" sạch vẫn trùng
        key = dedupe_key("Social Media Leader | AI & AUTOMATION", "CÔNG TY TNHH TM&DV JAMU JMC")
        assert "amp" not in key.split()
        assert store.find_by_dedupe_key(key) == jid
        assert store.get_job(jid)["title"] == "Social Media Leader | AI & AUTOMATION"


# ---------------------------------------------------------------------------
# Storage tests
# ---------------------------------------------------------------------------

class TestStore:
    def test_insert_and_search(self):
        store = JobStore(":memory:")
        jid = store.insert_job(normalize_job(make_job()))
        assert jid > 0
        res = store.search_jobs(query="content")
        assert res["total_estimate"] >= 1
        assert any(i["job_id"] == jid for i in res["items"])

    def test_dedupe_idempotent(self):
        store = JobStore(":memory:")
        j1 = normalize_job(make_job())
        j2 = normalize_job(make_job())  # trùng title+company
        id1 = store.insert_job(j1)
        existing = store.find_by_dedupe_key(dedupe_key(j1.title, j1.company_name))
        assert existing == id1
        store.update_job(existing, j2)  # không insert mới
        assert store.stats()["total_jobs"] == 1

    def test_expired_marked_inactive(self):
        store = JobStore(":memory:")
        j = make_job(deadline=datetime.now(timezone.utc) - timedelta(days=1))
        store.insert_job(normalize_job(j))
        store.mark_expired_inactive()
        res = store.search_jobs()  # mặc định chỉ active
        assert res["total_estimate"] == 0

    def test_filters(self):
        store = JobStore(":memory:")
        store.insert_job(normalize_job(make_job(
            title="Video Editor", salary_text="10 - 20 triệu",
            description="dựng video premiere",
        )))
        store.insert_job(normalize_job(make_job(
            title="Senior Content Marketing", salary_text="30 - 40 triệu",
        )))
        res = store.search_jobs(role_clusters=["video_production"])
        assert res["total_estimate"] == 1
        res = store.search_jobs(salary_min_vnd=30_000_000)
        assert res["total_estimate"] == 1
        res = store.search_jobs(query="không-tồn-tại-xyz")
        assert res["total_estimate"] == 0

    def test_saved_jobs(self):
        store = JobStore(":memory:")
        jid = store.insert_job(normalize_job(make_job()))
        uid = store.conn.execute("SELECT user_id FROM users LIMIT 1").fetchone()["user_id"]
        assert store.save_job_for_user(uid, jid)
        assert not store.save_job_for_user(uid, jid)  # đã lưu
        items = store.saved_jobs_for_user(uid)
        assert len(items) == 1
        assert store.unsave_job_for_user(uid, jid)
        assert not store.saved_jobs_for_user(uid)

    def test_company_feedback(self):
        from agent_search_job.domain.models import Company, CompanyFeedback
        store = JobStore(":memory:")
        cid = store.upsert_company(Company(name="FPT Software"))
        fb = CompanyFeedback(
            company_id=cid, source="itviec",
            source_url="https://itviec.com/companies/fpt-software/review",
            rating=4.2, review_count=2206, confidence="high",
        )
        store.save_feedback(fb)
        got = store.get_feedback(cid)
        assert len(got) == 1 and got[0]["rating"] == 4.2
        assert got[0]["review_count"] == 2206


# ---------------------------------------------------------------------------
# MCP contracts
# ---------------------------------------------------------------------------

class TestContracts:
    def test_valid(self):
        f = SearchFilters(query="content tiktok", page_size=25,
                          posted_within_days=30).validate()
        assert f.query == "content tiktok"

    def test_page_size_too_big(self):
        with pytest.raises(ValidationError):
            SearchFilters(page_size=26).validate()

    def test_bad_role(self):
        with pytest.raises(ValidationError):
            SearchFilters(role_clusters=["ceo"]).validate()

    def test_bad_source(self):
        with pytest.raises(ValidationError):
            SearchFilters(source="nasa").validate()

    def test_posted_days_max(self):
        with pytest.raises(ValidationError):
            SearchFilters(posted_within_days=61).validate()


# ---------------------------------------------------------------------------
# MCP server protocol (in-process, không cần HTTP)
# ---------------------------------------------------------------------------

class TestMCPServer:
    def _srv(self):
        from agent_search_job.mcp.server import MCPServer
        store = JobStore(":memory:")
        store.insert_job(normalize_job(make_job()))
        return MCPServer(store)

    async def test_initialize(self):
        srv = self._srv()
        r = await srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {}})
        assert r["result"]["serverInfo"]["name"] == "vn-jobs-mcp"

    async def test_tools_list(self):
        srv = self._srv()
        r = await srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in r["result"]["tools"]]
        assert names == [
            "vn_jobs_search_jobs", "vn_jobs_get_job", "vn_jobs_get_company",
            "vn_jobs_get_company_feedback", "vn_jobs_get_market_facets",
        ]
        # mọi tool có inputSchema + outputSchema
        for t in r["result"]["tools"]:
            assert t["inputSchema"]["type"] == "object"
            assert t["outputSchema"]["required"]

    async def test_search_tool(self):
        srv = self._srv()
        r = await srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "vn_jobs_search_jobs",
                                         "arguments": {"query": "content"}}})
        sc = r["result"]["structuredContent"]
        assert sc["total_estimate"] >= 1
        assert "items" in sc and "next_cursor" in sc
        assert sc["applied_filters"]["query"] == "content"
        # TextContent fallback cũng là JSON
        import json
        json.loads(r["result"]["content"][0]["text"])

    async def test_search_validation_error(self):
        srv = self._srv()
        r = await srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"name": "vn_jobs_search_jobs",
                                         "arguments": {"page_size": 99}}})
        assert r["result"]["isError"] is True

    async def test_get_job(self):
        srv = self._srv()
        r = await srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                              "params": {"name": "vn_jobs_get_job",
                                         "arguments": {"job_id": 1}}})
        assert r["result"]["structuredContent"]["job"]["title"]

    async def test_get_company_feedback_unavailable(self):
        from agent_search_job.domain.models import Company
        srv = self._srv()
        srv.store.upsert_company(Company(name="XYZ Media"))
        r = await srv.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                              "params": {"name": "vn_jobs_get_company_feedback",
                                         "arguments": {"company_id": 1}}})
        sc = r["result"]["structuredContent"]
        assert sc["status"] == "unavailable"
        assert sc["feedback"] is None
        assert "note" in sc  # không để model tự đoán

    async def test_get_facets(self):
        srv = self._srv()
        r = await srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                              "params": {"name": "vn_jobs_get_market_facets",
                                         "arguments": {}}})
        sc = r["result"]["structuredContent"]
        assert any(f["value"] == "content_marketing" for f in sc["role_clusters"])

    async def test_unknown_method(self):
        srv = self._srv()
        r = await srv.handle({"jsonrpc": "2.0", "id": 8, "method": "nope"})
        assert r["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Web API tests (TestClient, không cần mạng thật)
# ---------------------------------------------------------------------------

class TestWebAPI:
    def _client(self):
        from fastapi.testclient import TestClient
        import os
        os.environ["JOBS_DB"] = ":memory:"
        import agent_search_job.web.app as appmod
        appmod._store = None  # reset singleton
        client = TestClient(appmod.app)
        # login bằng email allowlist mặc định
        r = client.post("/login", data={"email": "friend@example.com"},
                        follow_redirects=False)
        assert r.status_code == 302, r.text
        return client

    def test_login_and_search(self):
        client = self._client()
        r = client.get("/search?q=content")
        assert r.status_code == 200
        assert "Việc làm" in r.text or "Tìm việc" in r.text

    def test_unauthenticated_blocked(self):
        from fastapi.testclient import TestClient
        import os
        os.environ["JOBS_DB"] = ":memory:"
        import agent_search_job.web.app as appmod
        appmod._store = None
        client = TestClient(appmod.app)
        r = client.get("/search", follow_redirects=False)
        assert r.status_code == 401
        r = client.get("/api/jobs")
        assert r.status_code == 401

    def test_noindex_header(self):
        client = self._client()
        r = client.get("/search?q=content")
        assert r.headers["x-robots-tag"] == "noindex, nofollow, noarchive"

    def test_mcp_endpoint_auth(self):
        from fastapi.testclient import TestClient
        import os
        os.environ["JOBS_DB"] = ":memory:"
        import agent_search_job.web.app as appmod
        appmod._store = None
        client = TestClient(appmod.app)
        # thiếu token -> 401
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.status_code == 401
        # token sai -> 403
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                        headers={"Authorization": "Bearer sai"})
        assert r.status_code == 403

    def test_mcp_endpoint_with_token(self):
        client = self._client()
        # lấy token từ DB
        from agent_search_job.web.app import get_store
        token = get_store().conn.execute(
            "SELECT token FROM users LIMIT 1"
        ).fetchone()["token"]
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                      "method": "tools/list"},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        tools = r.json()["result"]["tools"]
        assert len(tools) == 5

    def test_mcp_origin_check(self):
        client = self._client()
        from agent_search_job.web.app import get_store
        token = get_store().conn.execute(
            "SELECT token FROM users LIMIT 1"
        ).fetchone()["token"]
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={"Authorization": f"Bearer {token}",
                                 "Origin": "https://evil.example"})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# LLM enrichment (mock httpx, không gọi mạng thật)
# ---------------------------------------------------------------------------

class TestLLMEnrich:
    def _cfg(self):
        from agent_search_job.pipeline.llm_enrich import LLMConfig
        return LLMConfig(api_key="test-key", model="z-ai/glm-5.3",
                         base_url="http://llm.test/v1")

    async def test_score_relevance_parses_json(self):
        import agent_search_job.pipeline.llm_enrich as le

        class FakeResp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {
                    "content": '{"relevance": 0.9, "reason": "đúng nhóm content"}'}}]}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return FakeResp()

        orig = le.httpx.AsyncClient
        le.httpx.AsyncClient = FakeClient
        try:
            en = le.LLMEnricher(self._cfg())
            rel = await en.score_relevance({"title": "Social Media Specialist",
                                            "company_name": "X", "district": "Quận 1",
                                            "city": "Hồ Chí Minh", "description": "TikTok"})
            assert rel is not None and rel.relevance == 0.9
            assert "content" in rel.reason
        finally:
            le.httpx.AsyncClient = orig

    async def test_extract_sanitizes_enums(self):
        import agent_search_job.pipeline.llm_enrich as le

        class FakeResp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": json.dumps({
                    "platforms": ["tiktok", "napster"],
                    "content_types": ["short_video", "wrong"],
                    "creative_tools": ["capcut", "excel"],
                    "on_camera_required": True,
                    "client_facing": None,
                    "agency_or_inhouse": "agency",
                    "kpi_types": ["views"],
                    "languages": ["vi", "jp"],
                })}}]}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return FakeResp()

        orig = le.httpx.AsyncClient
        le.httpx.AsyncClient = FakeClient
        try:
            en = le.LLMEnricher(self._cfg())
            ext = await en.extract("TikTok Content", "làm video ngắn")
            assert ext.platforms == ["tiktok"]
            assert ext.content_types == ["short_video"]
            assert ext.creative_tools == ["capcut"]
            assert ext.on_camera_required is True
            assert ext.agency_or_inhouse == "agency"
            assert ext.languages == ["vi"]
        finally:
            le.httpx.AsyncClient = orig

    async def test_no_api_key_returns_none(self):
        from agent_search_job.pipeline.llm_enrich import LLMEnricher, LLMConfig
        en = LLMEnricher(LLMConfig(api_key=""))
        assert await en.score_relevance({"title": "x"}) is None
        assert await en.extract("t", "d") is None

    async def test_llm_error_returns_none(self):
        import agent_search_job.pipeline.llm_enrich as le

        class FakeClient:
            def __init__(self, *a, **k): raise RuntimeError("network down")

        orig = le.httpx.AsyncClient
        le.httpx.AsyncClient = FakeClient
        try:
            en = le.LLMEnricher(self._cfg())
            assert await en.score_relevance({"title": "x"}) is None
        finally:
            le.httpx.AsyncClient = orig

    def test_cache_roundtrip_and_search_join(self):
        from agent_search_job.pipeline.llm_enrich import PROMPT_VERSION
        store = JobStore(":memory:")
        jid = store.insert_job(normalize_job(make_job()))
        assert store.get_llm_cache(jid, "m", "v1", "relevance") is None
        store.set_llm_cache(jid, "m", "v1", "relevance",
                            {"relevance": 0.8, "reason": "ok"})
        got = store.get_llm_cache(jid, "m", "v1", "relevance")
        assert got["relevance"] == 0.8
        # jobs_needing_llm loại job đã cache
        store.set_llm_cache(jid, "m", PROMPT_VERSION, "relevance",
                            {"relevance": 0.8, "reason": "ok"})
        assert store.jobs_needing_llm("m", PROMPT_VERSION) == []
        # search join llm_relevance
        res = store.search_jobs(query="content")
        item = next(i for i in res["items"] if i["job_id"] == jid)
        assert item.get("llm_relevance") == 0.8
