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

    def test_cursor_pages_cover_all_jobs_with_relevance_order(self):
        # sort theo LLM relevance trước job_id: cursor phải đi hết mọi job, không trùng/sót
        store = JobStore(":memory:")
        ids = [store.insert_job(normalize_job(make_job(
            title=f"Content Writer {i}", source_job_id=str(i),
            source_url=f"https://example.com/job/{i}",
        ))) for i in range(5)]
        store.set_llm_cache(ids[0], "m", "v1", "relevance", {"relevance": 0.9})
        seen, cursor = [], None
        while True:
            res = store.search_jobs(page_size=2, cursor=cursor)
            assert res["total_estimate"] == 5
            seen += [i["job_id"] for i in res["items"]]
            cursor = res["next_cursor"]
            if not cursor:
                break
        assert seen[0] == ids[0]
        assert sorted(seen) == sorted(ids)

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
            "vn_jobs_get_company_feedback", "vn_jobs_smart_search",
            "vn_jobs_get_market_facets",
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

    def test_search_pagination_keeps_filters(self):
        client = self._client()
        import agent_search_job.web.app as appmod
        store = appmod.get_store()
        for i in range(30):
            store.insert_job(normalize_job(make_job(
                title=f"Content Writer {i}", source_job_id=str(i),
                source_url=f"https://example.com/job/{i}",
            )))
        r1 = client.get("/search?q=content&source=vietnamworks")
        assert r1.text.count('class="ticket"') == 25
        assert "page=2" in r1.text and "q=content" in r1.text
        r2 = client.get("/search?q=content&source=vietnamworks&page=2")
        assert r2.status_code == 200
        assert r2.text.count('class="ticket"') == 5
        assert "Content Writer 0<" in r1.text + r2.text
        assert 'value="content"' in r2.text and "page=1" in r2.text

    def test_job_detail_renders_without_waiting_llm(self, monkeypatch):
        import agent_search_job.pipeline.jd_analysis as ja
        import agent_search_job.web.app as appmod
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        client = self._client()
        job_id = appmod.get_store().insert_job(normalize_job(make_job(
            description="x" * 300)))
        calls = []

        async def fake_analyze(self, job):
            calls.append(job["job_id"])
            return ja.JDAnalysis(summary="Tóm tắt AI cho job", key_responsibilities=["Viết bài"])
        monkeypatch.setattr(ja.JDAnalyzer, "analyze", fake_analyze)

        # lần đầu: chưa cache -> trang render ngay, không gọi LLM, có khối chờ
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200 and calls == []
        assert "AI đang phân tích JD, vui lòng chờ" in r.text
        assert f'data-src="/jobs/{job_id}/analysis"' in r.text
        # JS gọi fragment -> LLM chạy, trả khối kết quả và ghi cache
        frag = client.get(f"/jobs/{job_id}/analysis")
        assert frag.status_code == 200 and calls == [job_id]
        assert "Tóm tắt AI cho job" in frag.text and "<html" not in frag.text
        # lần sau: cache -> hiện ngay, không spinner, không gọi LLM lại
        r2 = client.get(f"/jobs/{job_id}")
        assert "Tóm tắt AI cho job" in r2.text and "vui lòng chờ" not in r2.text
        assert calls == [job_id]

    def test_job_detail_llm_off_or_failed(self, monkeypatch):
        import agent_search_job.pipeline.jd_analysis as ja
        import agent_search_job.web.app as appmod
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = self._client()
        job_id = appmod.get_store().insert_job(normalize_job(make_job(
            description="x" * 300)))
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200 and "vui lòng chờ" not in r.text
        assert client.get(f"/jobs/{job_id}/analysis").status_code == 204
        # LLM bật nhưng lỗi -> 204 để trang báo nhẹ, không treo spinner
        monkeypatch.setenv("OPENAI_API_KEY", "k")

        async def broken(self, job):
            return None
        monkeypatch.setattr(ja.JDAnalyzer, "analyze", broken)
        assert client.get(f"/jobs/{job_id}/analysis").status_code == 204
        assert client.get("/jobs/999999/analysis").status_code == 404

    def test_job_detail_formats_jd(self):
        import agent_search_job.web.app as appmod
        client = self._client()
        raw = ('nhân viên bán hàng">\n\n\n   \n'
               '   1. Sản xuất nội dung\n'
               '   • Viết bài PR &amp; SEO\n\n\n'
               '   • Dựng video ngắn\n'
               '\n\nQuyền lợi:\n'
               '  Lương tháng 13\n  Du lịch hằng năm\n')
        job_id = appmod.get_store().insert_job(normalize_job(make_job(description=raw)))
        html = client.get(f"/jobs/{job_id}").text
        jd = html[html.index('<div class="jd">'):]
        jd = jd[:jd.index("</div>")]
        assert '">' not in jd.replace('<div class="jd">', "").replace('<ul class="ticks">', "")
        assert "<h3>1. Sản xuất nội dung</h3>" in jd
        assert "<li>Viết bài PR &amp; SEO</li><li>Dựng video ngắn</li>" in jd
        assert "<h3>Quyền lợi</h3>" in jd
        assert "<li>Lương tháng 13</li><li>Du lịch hằng năm</li>" in jd
        assert "\n\n" not in jd

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
        assert len(tools) == 6

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


# ---------------------------------------------------------------------------
# NL query parser (LLL mock + rule fallback)
# ---------------------------------------------------------------------------

class TestNLQuery:
    async def test_llm_parse_sanitizes(self):
        import agent_search_job.pipeline.nl_query as nq
        import agent_search_job.pipeline.llm_enrich as le

        class FakeResp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": json.dumps({
                    "query": "content tiktok",
                    "role_clusters": ["social_media", "ceo"],
                    "districts": ["Bình Thạnh", "Hà Nội"],
                    "salary_min_vnd": 12000000,
                    "work_modes": ["remote", "teleport"],
                    "posted_within_days": 999,
                    "portfolio_required": "maybe",
                    "source": "topcv",
                })}}]}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return FakeResp()

        orig = nq.httpx.AsyncClient
        nq.httpx.AsyncClient = FakeClient
        try:
            parser = nq.NLQueryParser(le.LLMConfig(api_key="k"))
            parsed = await parser.parse("tìm việc content tiktok lương trên 12 triệu")
            f = parsed.filters
            assert parsed.llm_used
            assert f.query == "content tiktok"
            assert f.role_clusters == ["social_media"]          # 'ceo' bị loại
            assert f.districts == ["Bình Thạnh"]                # 'Hà Nội' ngoài HCM bị loại
            assert f.salary_min_vnd == 12_000_000
            assert f.work_modes == ["remote"]                   # 'teleport' bị loại
            assert f.posted_within_days == 60                   # 999 -> clamp 60
            assert f.portfolio_required is None                 # "maybe" -> None
            assert f.source == "topcv"
        finally:
            nq.httpx.AsyncClient = orig

    async def test_no_key_falls_back_to_rules(self):
        from agent_search_job.pipeline.nl_query import NLQueryParser
        from agent_search_job.pipeline.llm_enrich import LLMConfig
        parser = NLQueryParser(LLMConfig(api_key=""))
        parsed = await parser.parse("việc tiktok remote lương trên 15 triệu")
        assert not parsed.llm_used
        f = parsed.filters
        assert f.salary_min_vnd == 15_000_000
        assert f.work_modes == ["remote"]
        assert f.role_clusters == ["social_media"]

    async def test_llm_error_falls_back(self):
        import agent_search_job.pipeline.nl_query as nq
        from agent_search_job.pipeline.llm_enrich import LLMConfig

        class Boom:
            def __init__(self, *a, **k): raise RuntimeError("down")

        orig = nq.httpx.AsyncClient
        nq.httpx.AsyncClient = Boom
        try:
            parser = nq.NLQueryParser(LLMConfig(api_key="k"))
            parsed = await parser.parse("việc video editor")
            assert not parsed.llm_used
            assert "fallback" in parsed.note
        finally:
            nq.httpx.AsyncClient = orig


# ---------------------------------------------------------------------------
# JD analysis (LLM mock)
# ---------------------------------------------------------------------------

class TestJDAnalysis:
    async def test_analyze_parses_and_clips(self):
        import agent_search_job.pipeline.jd_analysis as ja
        from agent_search_job.pipeline.llm_enrich import LLMConfig

        class FakeResp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": json.dumps({
                    "summary": "Vai trò content toàn diện tại agency",
                    "key_responsibilities": ["Viết nội dung", "Quản lý fanpage",
                                             "Chụp ảnh sản phẩm", "Đăng bài",
                                             "Báo cáo KPI", "Thứ 6 sẽ bị cắt"],
                    "requirements": ["2 năm kinh nghiệm"],
                    "nice_to_have": ["CapCut"],
                    "salary_hint": "15-20 triệu",
                    "perks": ["Bonus quý"],
                    "red_flags": ["Lương thoả thuận"],
                })}}]}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k):
                sent.append(k["json"])
                return FakeResp()

        sent = []
        orig = ja.httpx
        import httpx as real_httpx
        ja.httpx = type("m", (), {"AsyncClient": FakeClient})
        try:
            an = ja.JDAnalyzer(LLMConfig(api_key="k"))
            result = await an.analyze({
                "title": "Content Executive",
                "company_name": "Agency X",
                "description": "x" * 300, "requirements": "y" * 100,
                "salary_text": "Thoả thuận",
            })
            assert result is not None
            assert len(result.key_responsibilities) == 5  # clip 6 -> 5
            assert all(len(r) <= 120 for r in result.key_responsibilities)
            assert result.salary_hint == "15-20 triệu"
            # JD tiếng Anh vẫn phải được dịch sang tiếng Việt
            system = sent[0]["messages"][0]["content"]
            assert "dịch sang tiếng Việt" in system
        finally:
            ja.httpx = orig

    async def test_short_jd_returns_none(self):
        from agent_search_job.pipeline.jd_analysis import JDAnalyzer
        from agent_search_job.pipeline.llm_enrich import LLMConfig
        an = JDAnalyzer(LLMConfig(api_key="k"))
        assert await an.analyze({"title": "t", "description": "ngắn",
                                 "requirements": "", "company_name": "c"}) is None


class TestTopCVDetail:
    async def test_jd_taken_from_h2_block_not_blog_menu(self, monkeypatch):
        """Menu TopCV có bài blog title="...mô tả công việc..." trước JD thật."""
        import types
        from agent_search_job.sources.detail_fetchers import fetch_detail
        page_html = (
            '<a title="Nhân viên bán hàng là gì? Bảng mô tả công việc nhân viên bán hàng">'
            '<div>Nhân viên bán hàng là gì?</div><div>Khám phá mức lương Kế toán mới nhất</div></a>'
            '<div class="box"><h2 class="t">\n  Mô tả công việc\n</h2>'
            '<div><ul><li>  Lập kế hoạch nội dung</li>\n\n\n<li>Viết kịch bản video</li></ul></div>'
            '<h2>Yêu cầu ứng viên</h2><div><p>2 năm kinh nghiệm</p></div>'
            '<h2>Quyền lợi ứng viên</h2><div><p>Lương tháng 13</p><p>BHXH</p></div>'
            '<h2>Địa điểm</h2></div>'
        )

        class Obj:
            def __init__(self, **kw): self.__dict__.update(kw)

        async def noop(*a, **k): return None

        async def boom(*a, **k): raise TimeoutError

        async def content(): return page_html
        page = Obj(goto=noop, click=boom, wait_for_timeout=noop, content=content)

        async def new_page(): return page

        async def new_context(**k): return Obj(new_page=new_page)
        browser = Obj(new_context=new_context, close=noop)

        async def launch(**k): return browser

        class PW:
            async def __aenter__(self): return Obj(chromium=Obj(launch=launch))
            async def __aexit__(self, *a): return False
        mod = types.ModuleType("playwright.async_api")
        mod.async_playwright = PW
        monkeypatch.setitem(sys.modules, "playwright.async_api", mod)

        d = await fetch_detail("topcv", "https://www.topcv.vn/viec-lam/x/1.html")
        assert d.description == "Lập kế hoạch nội dung\nViết kịch bản video"
        assert d.requirements == "2 năm kinh nghiệm"
        assert d.benefits == ["Lương tháng 13", "BHXH"]

        # tin tiếng Anh trên TopCV
        page_html = ('<h2>Job Description</h2><div><p>Plan social content</p></div>'
                     '<h2>Candidate Requirements</h2><div><p>2 years</p></div>'
                     '<h2>Benefits</h2><div><p>13th salary</p></div><h2>Address</h2>')
        d = await fetch_detail("topcv", "https://www.topcv.vn/viec-lam/x/2.html")
        assert (d.description, d.requirements, d.benefits) == (
            "Plan social content", "2 years", ["13th salary"])


# ---------------------------------------------------------------------------
# Smart search (MCP tool qua MCPServer in-process, LLM mock)
# ---------------------------------------------------------------------------

class TestSmartSearch:
    async def test_smart_search_tool(self):
        import agent_search_job.pipeline.nl_query as nq
        from agent_search_job.pipeline.llm_enrich import LLMConfig
        from agent_search_job.mcp.server import MCPServer

        class FakeResp:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": json.dumps({
                    "query": "content", "role_clusters": [], "districts": [],
                    "salary_min_vnd": None, "work_modes": [],
                    "posted_within_days": 60, "portfolio_required": None,
                    "source": "all"})}}]}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return FakeResp()

        orig = nq.httpx.AsyncClient
        nq.httpx.AsyncClient = FakeClient
        import os
        os.environ["OPENAI_API_KEY"] = "k"
        try:
            store = JobStore(":memory:")
            store.insert_job(normalize_job(make_job()))
            srv = MCPServer(store)
            r = await srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "vn_jobs_smart_search",
                                             "arguments": {"text": "việc content nhé"}}})
            sc = r["result"]["structuredContent"]
            assert sc["parser"]["llm_used"]
            assert sc["total_estimate"] >= 1
            assert sc["parsed_filters"]["query"] == "content"
        finally:
            nq.httpx.AsyncClient = orig
            del os.environ["OPENAI_API_KEY"]

    async def test_smart_search_empty_text_error(self):
        from agent_search_job.mcp.server import MCPServer
        srv = MCPServer(JobStore(":memory:"))
        r = await srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "vn_jobs_smart_search",
                                         "arguments": {"text": "   "}}})
        assert r["result"]["isError"] is True


# ---------------------------------------------------------------------------
# Scope: chỉ Truyền thông đa phương tiện
# ---------------------------------------------------------------------------

class TestMultimediaScope:
    def test_scope_filters_marketing_pure_jobs(self):
        from agent_search_job.pipeline.ingest import in_multimedia_scope
        assert in_multimedia_scope("Content Marketing Specialist", "viết nội dung")
        assert in_multimedia_scope("Video Editor", "dựng video")
        assert in_multimedia_scope("Graphic Designer", "")
        assert in_multimedia_scope("Trưởng Nhóm Bán Hàng Mảng Video Và Truyền Thông", "")
        assert in_multimedia_scope("Marketing & Communication Specialist", "")
        assert not in_multimedia_scope("Telesales", "gọi bán hàng")
        assert not in_multimedia_scope("Nhân Viên Bán Hàng", "")
        assert not in_multimedia_scope("Kế toán", "")
        assert not in_multimedia_scope("Performance Marketing Specialist", "chạy ads")
        assert not in_multimedia_scope("Event Executive", "tổ chức sự kiện")

    def test_default_queries_only_multimedia(self):
        from agent_search_job.pipeline.ingest import DEFAULT_QUERIES
        assert "content" in DEFAULT_QUERIES
        assert "video editor" in DEFAULT_QUERIES
        assert "digital marketing" not in DEFAULT_QUERIES

    def test_pipeline_marks_out_of_scope(self):
        # insert 2 job (1 trong scope, 1 ngoài) qua FakeAdapter -> chỉ 1 được lưu
        from agent_search_job.pipeline.ingest import IngestPipeline
        from agent_search_job.domain.models import NormalizedJob

        class FakeAdapter:
            name = "vietnamworks"
            async def health_check(self):
                from agent_search_job.sources.base import SourceHealth
                return SourceHealth(source=self.name, ok=True, latency_ms=1,
                                    checked_at=None)
            async def discover(self, input):
                for t in ("Content Writer viết bài SEO", "Nhân viên Kinh doanh"):
                    j = NormalizedJob(title=t, company_name=f"C_{t[:6]}",
                                      salary_text="10 triệu", description="",
                                      source=self.name, source_job_id=t)
                    yield j

        store = JobStore(":memory:")
        pipeline = IngestPipeline(store, [FakeAdapter()])
        import asyncio
        r = asyncio.run(pipeline.run(queries=["content"], max_results_per_query=10))
        assert r.inserted == 1
        assert r.out_of_scope == 1
        items = store.search_jobs(query="")["items"]
        assert len(items) == 1
        assert "Content Writer" in items[0]["title"]


# ---------------------------------------------------------------------------
# Job level + company search
# ---------------------------------------------------------------------------

class TestJobLevelAndCompany:
    def test_level_classify(self):
        from agent_search_job.domain.normalize import classify_job_level
        assert classify_job_level("Thực Tập Sinh Content", "", None, None) == "intern"
        assert classify_job_level("Senior Content", "", 4, 7) == "senior"
        assert classify_job_level("Nhân viên Content", "", 1, 2) == "junior"
        assert classify_job_level("Marketing Manager", "", None, None) == "manager"

    def test_search_by_level_and_company(self):
        store = JobStore(":memory:")
        j1 = normalize_job(make_job(title="Senior Content Lead",
                                    salary_text="20 triệu"))
        j1.title = "Senior Content Lead"
        store.insert_job(j1)
        j2 = normalize_job(make_job(title="Content Intern",
                                    salary_text="3 triệu"))
        store.insert_job(j2)
        assert store.search_jobs(job_levels=["senior"])["total_estimate"] == 1
        assert store.search_jobs(job_levels=["intern"])["total_estimate"] == 1
        comps = store.search_company_jobs("Công ty")
        assert len(comps) == 2

    def test_contracts_level_validation(self):
        from agent_search_job.mcp.contracts import SearchFilters, ValidationError
        f = SearchFilters(job_levels=["junior"]).validate()
        assert f.job_levels == ["junior"]
        try:
            SearchFilters(job_levels=["boss"]).validate()
            raise AssertionError("phải chặn")
        except ValidationError:
            pass


# ---------------------------------------------------------------------------
# VNG Careers adapter (scraping __NEXT_DATA__)
# ---------------------------------------------------------------------------

class TestVNGCareers:
    def test_vng_adapter_parses_next_data(self):
        import asyncio
        import agent_search_job.sources.vng_careers as vc
        from agent_search_job.sources.vng_careers import VNGCareersAdapter
        from agent_search_job.pipeline.ingest import DiscoverInput

        sample = {"props": {"pageProps": {"jobs": [
            {"job_id": "6922", "title": "Creative Executive, VNGGames",
             "location": "Thành phố Hồ Chí Minh", "description": "<p>x" * 30,
             "slug": "6922-creative-executive-vnggames-vi"},
            {"job_id": "1", "title": "Admin Executive",
             "location": "Sydney", "description": "y", "slug": "1-admin"},
        ], "total": 2}}}

        class FakeResp:
            status_code = 200
            text = ('<html><script id="__NEXT_DATA__" type="application/json">'
                    + json.dumps(sample) + "</script></html>")

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **k): return FakeResp()

        orig = vc.httpx.AsyncClient
        vc.httpx.AsyncClient = FakeClient
        try:
            async def run():
                ad = VNGCareersAdapter()
                jobs = []
                async for j in ad.discover(DiscoverInput(query="creative", max_results=10)):
                    jobs.append(j)
                return jobs
            jobs = asyncio.run(run())
            assert len(jobs) == 2
            hcm = [j for j in jobs if j.is_hcm]
            assert len(hcm) == 1
            assert "Creative Executive" in hcm[0].title
            assert hcm[0].company_name == "VNG Corporation"
            assert hcm[0].source == "vng"
        finally:
            vc.httpx.AsyncClient = orig

    def test_scope_boundary_no_false_positive(self):
        from agent_search_job.pipeline.ingest import in_multimedia_scope
        # 'pr' trong 'Representative' không được tính là PR
        assert not in_multimedia_scope(
            "Business Development Representative, Instantia",
            "this role is designed as a pathway")
        # 'design' trong 'designed' không được tính
        assert not in_multimedia_scope("Fintech Analyst", "system designed for trading")
        assert in_multimedia_scope("PR Executive", "truyền thông")


# ---------------------------------------------------------------------------
# HCM hard requirement (hard-code theo nguồn)
# ---------------------------------------------------------------------------

class TestHCMHardRequirement:
    def test_is_hcm_by_source(self):
        from agent_search_job.pipeline.ingest import IngestPipeline
        j1 = normalize_job(make_job(source="linkedin", address="Gò Công, Vietnam",
                                    city="Gò Công"))
        assert not IngestPipeline._is_hcm(j1)
        j2 = normalize_job(make_job(source="topcv", address="Hà Nội, Việt Nam",
                                    city="Hà Nội"))
        assert not IngestPipeline._is_hcm(j2)
        j3 = normalize_job(make_job(source="topcv", address="",
                                    description="làm việc tại Quận 1, TP.HCM"))
        assert IngestPipeline._is_hcm(j3)

    def test_pipeline_counts_out_of_hcm(self):
        from agent_search_job.pipeline.ingest import IngestPipeline
        from agent_search_job.domain.models import NormalizedJob

        class FakeAdapter:
            name = "linkedin"
            async def health_check(self):
                from agent_search_job.sources.base import SourceHealth
                return SourceHealth(source=self.name, ok=True, latency_ms=1,
                                    checked_at=None)
            async def discover(self, input):
                for t, addr in (("Content HCM", "Ho Chi Minh"),
                                ("Content HN", "Hà Nội")):
                    yield NormalizedJob(title=t, company_name=f"C_{t}",
                                        address=addr, city=addr,
                                        source=self.name, source_job_id=t)

        store = JobStore(":memory:")
        pipeline = IngestPipeline(store, [FakeAdapter()])
        import asyncio
        r = asyncio.run(pipeline.run(queries=["content"], max_results_per_query=10))
        assert r.inserted == 1
        assert r.out_of_hcm == 1

    def test_update_keep_inactive(self):
        store = JobStore(":memory:")
        j = normalize_job(make_job())
        jid = store.insert_job(j)
        with store.conn:
            store.conn.execute("UPDATE jobs SET is_active=0 WHERE job_id=?", (jid,))
        j2 = normalize_job(make_job())
        store.update_job(jid, j2, keep_inactive=True)
        row = store.get_job(jid)
        assert row["is_active"] == 0
        # không keep_inactive -> active lại bình thường
        store.update_job(jid, j2)
        assert store.get_job(jid)["is_active"] == 1

    def test_scope_no_design_verb_false_positive(self):
        from agent_search_job.pipeline.ingest import in_multimedia_scope
        # 'to design and operate' (động từ) không phải nghề design
        assert not in_multimedia_scope(
            "AI Engineer, Zalopay",
            "We are hiring an AI Engineer to design and operate production-grade AI/ML systems")
        # 'xây dựng' không phải 'dựng video'
        assert not in_multimedia_scope(
            "DevOps Engineer", "xây dựng và vận hành các giải pháp AI")
        assert in_multimedia_scope("Video Editor", "dựng video tiktok")
        assert in_multimedia_scope("Nhân viên dựng phim", "montage")
