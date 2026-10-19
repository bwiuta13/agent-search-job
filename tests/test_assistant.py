"""Trợ lý AI (/assistant): lịch sử, hồ sơ/CV, agent SSE + tool, dọn dữ liệu — chạy offline.

LLM + Tavily giả bằng FakeHTTP (thay httpx.AsyncClient của module agent), giống TestNLQuery.
"""
from __future__ import annotations

import copy
import io
import json
import re
import zipfile
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


def make_pdf(text: str) -> bytes:
    """PDF 1 trang tối thiểu có chữ (Helvetica, ASCII) để pypdf trích được."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCX_CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Default Extension="xml" ContentType="application/xml"/>'
    b'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    b'officedocument.wordprocessingml.document.main+xml"/></Types>')
DOCX_RELS = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    b'relationships/officeDocument" Target="word/document.xml"/></Relationships>')


def make_docx(body: bytes, method: int = zipfile.ZIP_DEFLATED) -> bytes:
    """.docx tối thiểu (zipfile thuần): word/document.xml UTF-8 với body nằm trong <w:body>,
    nén theo method (chỉ áp cho word/document.xml)."""
    xml = (b'<?xml version="1.0" encoding="UTF-8"?>'
           b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           b'<w:body>' + body + b'</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", DOCX_CONTENT_TYPES)
        z.writestr("_rels/.rels", DOCX_RELS)
        z.writestr("word/document.xml", xml, compress_type=method)
    return buf.getvalue()


class TestCvUpload:
    def test_pdf_cv_is_extracted_and_original_not_kept(self, app):
        client = login(app)
        pdf = make_pdf("Kinh nghiem 3 nam content TikTok va Facebook cho my pham")
        r = client.post("/api/assistant/profile/cv", files={"file": ("cv.pdf", pdf, "application/pdf")})
        assert r.status_code == 200, r.text
        assert r.json()["cv_chars"] > 30 and "TikTok" in r.json()["preview"]
        prof = client.get("/api/assistant/profile").json()
        assert prof["cv_filename"] == "cv.pdf" and prof["cv_chars"] == r.json()["cv_chars"]
        row = app.get_store().conn.execute("SELECT cv_text FROM user_profiles").fetchone()
        assert "%PDF" not in row["cv_text"]

    def test_docx_cv_keeps_about(self, app):
        client = login(app)
        client.put("/api/assistant/profile", json={"about": "Muốn làm Quận 1"})
        docx = make_docx("<w:p><w:r><w:t>Kinh nghiệm 3 năm viết content TikTok, Facebook "
                         "cho thương hiệu mỹ phẩm</w:t></w:r></w:p>"
                         "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Canva</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
                         .encode())
        r = client.post("/api/assistant/profile/cv", files={"file": ("CV Lan.docx", docx, DOCX_MIME)})
        assert r.status_code == 200 and "mỹ phẩm" in r.json()["preview"]
        assert "Canva" in r.json()["preview"]
        assert client.get("/api/assistant/profile").json()["about"] == "Muốn làm Quận 1"

    @pytest.mark.parametrize("name,data,msg", [
        ("cv.txt", b"hello world", "PDF hoặc DOCX"),
        ("cv.pdf", b"PK\x03\x04 not a pdf", "PDF hoặc DOCX"),
        ("cv.pdf", b"%PDF-1.4 broken", "bị lỗi"),
        ("cv.pdf", b"%PDF-" + b"0" * (5 * 1024 * 1024), "5MB"),
    ])
    def test_bad_files_rejected(self, app, name, data, msg):
        client = login(app)
        r = client.post("/api/assistant/profile/cv", files={"file": (name, data, "application/octet-stream")})
        assert r.status_code == 400 and msg in r.json()["detail"]

    def test_docx_zip_bomb_rejected(self, app):
        client = login(app)
        pad = (b" " * (1024 * 1024) + b"<w:p/>") * 12  # ~12MB XML hợp lệ, upload nén còn vài chục KB
        docx = make_docx("<w:p><w:r><w:t>Kinh nghiệm 3 năm viết content TikTok</w:t></w:r></w:p>".encode() + pad)
        assert len(docx) < 200 * 1024
        r = client.post("/api/assistant/profile/cv", files={"file": ("bomb.docx", docx, DOCX_MIME)})
        assert r.status_code == 400 and "bị lỗi" in r.json()["detail"]

    @pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
    def test_docx_unsupported_compression_rejected(self, app, method):
        client = login(app)
        xml = ("<w:p><w:r><w:t>Kinh nghiệm 3 năm viết content TikTok, Facebook "
                "cho thương hiệu mỹ phẩm</w:t></w:r></w:p>")
        docx = make_docx(xml.encode(), method)
        r = client.post("/api/assistant/profile/cv", files={"file": ("cv.docx", docx, DOCX_MIME)})
        assert r.status_code == 400 and "bị lỗi" in r.json()["detail"]

    def test_docx_stored_compression_accepted(self, app):
        client = login(app)
        xml = ("<w:p><w:r><w:t>Kinh nghiệm 3 năm viết content TikTok, Facebook "
                "cho thương hiệu mỹ phẩm</w:t></w:r></w:p>")
        docx = make_docx(xml.encode(), zipfile.ZIP_STORED)
        r = client.post("/api/assistant/profile/cv", files={"file": ("cv.docx", docx, DOCX_MIME)})
        assert r.status_code == 200 and "mỹ phẩm" in r.json()["preview"]

    def test_docx_without_document_xml_rejected(self, app):
        client = login(app)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/styles.xml", b"<w:styles/>")
        r = client.post("/api/assistant/profile/cv", files={"file": ("empty.docx", buf.getvalue(), DOCX_MIME)})
        assert r.status_code == 400 and "bị lỗi" in r.json()["detail"]

    def test_pdf_without_text_rejected(self, app):
        client = login(app)
        r = client.post("/api/assistant/profile/cv", files={"file": ("scan.pdf", make_pdf(""), "application/pdf")})
        assert r.status_code == 400 and "không đọc được chữ" in r.json()["detail"]


def add_job(appmod, **kw) -> int:
    fields = dict(title="Content Marketing Executive", company_name="Công Ty TNHH ABC",
                  salary_text="15 - 25 triệu", address="123 Nguyễn Huệ, Quận 1, TP.HCM",
                  city="Hồ Chí Minh", description="Viết content TikTok, Facebook. Dùng Canva, CapCut.",
                  requirements="2 năm kinh nghiệm content", source="vietnamworks",
                  source_url="https://example.com/job/1", source_job_id="1")
    fields.update(kw)
    return appmod.get_store().insert_job(normalize_job(NormalizedJob(**fields)))


def sse(*deltas) -> list[str]:
    """Một lượt stream giả của /chat/completions: mỗi delta 1 dòng `data:` rồi [DONE]."""
    return [f"data: {json.dumps({'choices': [{'delta': d}]}, ensure_ascii=False)}"
            for d in deltas] + ["data: [DONE]"]


def tool_call(name, args, call_id="call_1", raw=None) -> dict:
    return {"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {
        "name": name, "arguments": raw if raw is not None else json.dumps(args, ensure_ascii=False)}}]}


class FakeHTTP:
    """Thay httpx.AsyncClient trong agent: stream() trả lần lượt các lượt SSE, post() trả Tavily."""

    def __init__(self, rounds, tavily=None):
        self.rounds, self.tavily = list(rounds), tavily
        self.requests, self.posts = [], []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, headers=None, json=None):
        self.requests.append(copy.deepcopy(json))  # agent còn sửa messages sau khi gửi
        lines = self.rounds.pop(0)

        class Resp:
            async def __aenter__(s):
                if isinstance(lines, Exception):
                    raise lines
                return s

            async def __aexit__(s, *a):
                return False

            def raise_for_status(s):
                pass

            async def aiter_lines(s):
                for line in lines:
                    yield line
        return Resp()

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json)
        payload = self.tavily

        class Resp:
            status_code = 200

            def raise_for_status(s):
                pass

            def json(s):
                return payload
        return Resp()


def fake_llm(monkeypatch, rounds, tavily=None) -> FakeHTTP:
    import agent_search_job.assistant.agent as agent
    fake = FakeHTTP(rounds, tavily)
    monkeypatch.setattr(agent.httpx, "AsyncClient", fake)
    return fake


def events(r) -> list[tuple[str, dict]]:
    return [(e, json.loads(d)) for e, d in re.findall(r"event: (\w+)\ndata: (.*)\n\n", r.text)]


class TestChat:
    def test_tool_round_then_answer_is_streamed_and_saved(self, app, monkeypatch):
        client = login(app)
        jid = add_job(app)
        fake = fake_llm(monkeypatch, [
            sse({"reasoning_content": "cần tìm việc"}, {"content": "Dạ công chúa, em tìm nha!"},
                tool_call("search_jobs", {"query": "content", "limit": 5})),
            sse({"content": f"Có [Content Marketing Executive](/jobs/{jid}) nè 💐"}),
        ])
        r = client.post("/api/assistant/chat", json={"message": "tìm việc content"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        ev = events(r)
        assert [e for e, _ in ev] == ["conversation", "token", "token", "tool_start", "tool_end",
                                      "token", "done"]
        assert ev[3][1]["label"] == "Em đang tìm việc…"
        card = ev[4][1]["jobs"][0]
        assert ev[4][1]["ok"] and card["job_id"] == jid and card["url"] == f"/jobs/{jid}"
        assert fake.requests[0]["tools"] and fake.requests[0]["stream"] is True
        assert fake.requests[0]["tool_choice"] == "auto"
        sent = fake.requests[1]["messages"]
        assert sent[-2]["role"] == "assistant" and sent[-2]["reasoning_content"] == "cần tìm việc"
        assert sent[-2]["tool_calls"][0]["function"]["name"] == "search_jobs"
        assert sent[-1]["role"] == "tool" and sent[-1]["tool_call_id"] == "call_1"
        assert json.loads(sent[-1]["content"])["total_estimate"] == 1
        conv = client.get(f"/api/assistant/conversations/{ev[0][1]['id']}").json()
        assert conv["title"] == "tìm việc content"
        assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
        assert conv["messages"][1]["content"] == (
            f"Dạ công chúa, em tìm nha!\n\nCó [Content Marketing Executive](/jobs/{jid}) nè 💐")
        assert conv["messages"][1]["meta"]["jobs"][0]["job_id"] == jid
        assert conv["messages"][1]["meta"]["tools"] == [
            {"name": "search_jobs", "summary": "1 kết quả", "ok": True}]

    def test_follow_up_sends_history_and_profile(self, app, monkeypatch):
        client = login(app)
        client.put("/api/assistant/profile", json={"about": "3 năm content TikTok, muốn 20 triệu"})
        fake = fake_llm(monkeypatch, [sse({"content": "Dạ"}), sse({"content": "Dạ tiếp"})])
        cid = events(client.post("/api/assistant/chat", json={"message": "câu 1"}))[0][1]["id"]
        client.post("/api/assistant/chat", json={"message": "câu 2", "conversation_id": cid})
        msgs = fake.requests[1]["messages"]
        assert "3 năm content TikTok" in msgs[0]["content"] and "công chúa" in msgs[0]["content"]
        assert [(m["role"], m["content"]) for m in msgs[1:]] == [
            ("user", "câu 1"), ("assistant", "Dạ"), ("user", "câu 2")]

    def test_get_jobs_returns_full_jd(self, app, monkeypatch):
        client = login(app)
        jid = add_job(app, description="Mô tả dài " * 50)
        fake = fake_llm(monkeypatch, [sse(tool_call("get_jobs", {"job_ids": [jid, 99999]})),
                                      sse({"content": "ok"})])
        ev = events(client.post("/api/assistant/chat", json={"message": "phân tích JD"}))
        out = json.loads(fake.requests[1]["messages"][-1]["content"])
        assert out["jobs"][0]["description"].startswith("Mô tả dài")
        assert out["jobs"][1] == {"job_id": 99999, "error": "không tìm thấy việc này"}
        assert [d for e, d in ev if e == "tool_end"][0]["summary"] == "đọc 1 JD"

    def test_bad_tool_arguments_go_back_to_model(self, app, monkeypatch):
        client = login(app)
        fake = fake_llm(monkeypatch, [sse(tool_call("search_jobs", None, raw="{hỏng")),
                                      sse({"content": "Em thử lại nhé"})])
        ev = events(client.post("/api/assistant/chat", json={"message": "x"}))
        end = [d for e, d in ev if e == "tool_end"][0]
        assert end["ok"] is False
        assert "error" in json.loads(fake.requests[1]["messages"][-1]["content"])
        assert ev[-1][0] == "done"

    def test_round_limit_forces_answer_without_tools(self, app, monkeypatch):
        import agent_search_job.assistant.agent as agent
        monkeypatch.setattr(agent, "MAX_ROUNDS", 1)
        client = login(app)
        fake = fake_llm(monkeypatch, [sse(tool_call("search_jobs", {"query": "x"})),
                                      sse({"content": "Em trả lời luôn ạ"})])
        ev = events(client.post("/api/assistant/chat", json={"message": "x"}))
        assert "tools" in fake.requests[0] and "tools" not in fake.requests[1]
        assert ev[-1][0] == "done"

    def test_deep_think_uses_more_rounds_and_tokens(self, app, monkeypatch):
        client = login(app)
        fake = fake_llm(monkeypatch, [sse({"content": "ok"})])
        client.post("/api/assistant/chat", json={"message": "x", "deep_think": True})
        assert fake.requests[0]["max_tokens"] == 6000

    def test_llm_not_configured(self, app, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY")
        client = login(app)
        ev = events(client.post("/api/assistant/chat", json={"message": "x"}))
        assert [e for e, _ in ev] == ["conversation", "error"]
        assert ev[1][1]["code"] == "llm_not_configured"
        conv = client.get(f"/api/assistant/conversations/{ev[0][1]['id']}").json()
        assert [m["role"] for m in conv["messages"]] == ["user"]

    def test_llm_connection_error(self, app, monkeypatch):
        import httpx
        client = login(app)
        fake_llm(monkeypatch, [httpx.ConnectError("down")])
        ev = events(client.post("/api/assistant/chat", json={"message": "x"}))
        assert ev[-1] == ("error", {"code": "llm_error",
                                    "message": "Em bị lỗi kết nối AI mất rồi, công chúa bấm thử lại giúp em nha 🥺"})

    def test_validation_and_ownership(self, app, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY")
        client = login(app)
        assert client.post("/api/assistant/chat", json={"message": "  "}).status_code == 400
        assert client.post("/api/assistant/chat", json={"message": "x" * 4001}).status_code == 400
        other = add_conv(app, f"user:{uid(app, 'b@example.com')}")
        assert client.post("/api/assistant/chat",
                           json={"message": "x", "conversation_id": other}).status_code == 404

    def test_daily_message_limit(self, app, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY")
        monkeypatch.setenv("ASSISTANT_DAILY_MESSAGES", "1")
        client = login(app)
        assert client.post("/api/assistant/chat", json={"message": "1"}).status_code == 200
        r = client.post("/api/assistant/chat", json={"message": "2"})
        assert r.status_code == 429 and "mai mình nói tiếp" in r.json()["detail"]
