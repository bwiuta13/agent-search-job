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
