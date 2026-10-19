"""Trợ lý AI (/assistant): API lịch sử hội thoại, hồ sơ/CV và chat (SSE).

Spec: docs/superpowers/specs/2026-10-19-ai-assistant-chat-design.md.
- owner = 'user:<user_id>' (đăng nhập) | 'guest:<cookie vnjobs_guest>' (public mode).
- Mỗi request API dọn dữ liệu quá hạn trước (JobStore.purge_expired_chat): hội thoại của
  mọi người + hồ sơ khách tự xoá sau ASSISTANT_CHAT_TTL_HOURS (24) giờ không hoạt động.
- Router được include ở cuối web/app.py (import muộn vì dùng get_store/current_user/templates).
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from html import unescape
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from ..domain.models import utcnow
from .app import _uid, current_user, get_store

router = APIRouter()
GUEST_COOKIE = "vnjobs_guest"
GUEST_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")  # secrets.token_urlsafe(24)
MAX_CV_BYTES = 5 * 1024 * 1024


def same_origin(request: Request) -> None:
    """POST/PUT/DELETE từ trang khác (CSRF) -> 403, giống kiểm tra Origin của /mcp."""
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "Origin không được phép")


def chat_owner(request: Request, user: dict = Depends(current_user)) -> str:
    store = get_store()
    if user.get("guest"):
        token = request.cookies.get(GUEST_COOKIE, "")
        if not GUEST_TOKEN.fullmatch(token):
            raise HTTPException(401, "Phiên khách đã hết hạn, công chúa tải lại trang giúp em nhé")
        owner = f"guest:{token}"
    else:
        owner = f"user:{_uid(store, user['email'])}"
    store.purge_expired_chat(int(os.environ.get("ASSISTANT_CHAT_TTL_HOURS") or 24))
    return owner


@router.get("/api/assistant/conversations")
async def list_conversations(owner: str = Depends(chat_owner)):
    rows = get_store().conn.execute(
        "SELECT id, title, updated_at FROM chat_conversations WHERE owner=? "
        "ORDER BY updated_at DESC LIMIT 50", (owner,)).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.get("/api/assistant/conversations/{conv_id}")
async def get_conversation(conv_id: int, owner: str = Depends(chat_owner)):
    store = get_store()
    conv = store.conn.execute(
        "SELECT id, title, updated_at FROM chat_conversations WHERE id=? AND owner=?",
        (conv_id, owner)).fetchone()
    if not conv:
        raise HTTPException(404, "Không tìm thấy hội thoại")
    rows = store.conn.execute(
        "SELECT id, role, content, meta_json, created_at FROM chat_messages "
        "WHERE conversation_id=? ORDER BY id", (conv_id,)).fetchall()
    return {**dict(conv), "messages": [
        {"id": r["id"], "role": r["role"], "content": r["content"],
         "meta": json.loads(r["meta_json"]) if r["meta_json"] else None,
         "created_at": r["created_at"]} for r in rows]}


@router.delete("/api/assistant/conversations/{conv_id}", dependencies=[Depends(same_origin)])
async def delete_conversation(conv_id: int, owner: str = Depends(chat_owner)):
    store = get_store()
    with store.conn:
        n = store.conn.execute(
            "DELETE FROM chat_conversations WHERE id=? AND owner=?", (conv_id, owner)).rowcount
    if not n:
        raise HTTPException(404, "Không tìm thấy hội thoại")
    return {"deleted": True}


@router.get("/api/assistant/profile")
async def get_profile(owner: str = Depends(chat_owner)):
    row = get_store().conn.execute(
        "SELECT about, cv_filename, length(cv_text) AS cv_chars, updated_at "
        "FROM user_profiles WHERE owner=?", (owner,)).fetchone()
    return dict(row) if row else {"about": "", "cv_filename": None, "cv_chars": 0, "updated_at": None}


@router.put("/api/assistant/profile", dependencies=[Depends(same_origin)])
async def put_profile(request: Request, owner: str = Depends(chat_owner)):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Dữ liệu không hợp lệ")
    about = str((body or {}).get("about") or "").strip()
    if len(about) > 4000:
        raise HTTPException(400, "Phần giới thiệu tối đa 4000 ký tự ạ")
    store = get_store()
    with store.conn:
        store.conn.execute(
            "INSERT INTO user_profiles(owner, about, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(owner) DO UPDATE SET about=excluded.about, updated_at=excluded.updated_at",
            (owner, about, utcnow().isoformat()))
    return await get_profile(owner)


@router.delete("/api/assistant/profile", dependencies=[Depends(same_origin)])
async def delete_profile(owner: str = Depends(chat_owner)):
    store = get_store()
    with store.conn:
        store.conn.execute("DELETE FROM user_profiles WHERE owner=?", (owner,))
    return {"deleted": True}


@router.post("/api/assistant/profile/cv", dependencies=[Depends(same_origin)])
def upload_cv(file: UploadFile = File(...), owner: str = Depends(chat_owner)):
    """CV PDF/DOCX -> chỉ lưu phần chữ (≤ 20.000 ký tự) vào hồ sơ; không lưu file gốc.
    Route sync để FastAPI chạy trong threadpool (parse không chặn event loop)."""
    data = file.file.read(MAX_CV_BYTES + 1)
    if len(data) > MAX_CV_BYTES:
        raise HTTPException(400, "File CV tối đa 5MB thôi ạ")
    name = (file.filename or "").lower()
    if name.endswith(".pdf") and data.startswith(b"%PDF-"):
        kind = "pdf"
    elif name.endswith(".docx") and data.startswith(b"PK\x03\x04"):
        kind = "docx"
    else:
        raise HTTPException(400, "Em chỉ đọc được file PDF hoặc DOCX thôi ạ")
    try:
        if kind == "pdf":
            from pypdf import PdfReader
            text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
        else:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                info = z.getinfo("word/document.xml")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    # bzip2/LZMA: zipfile giải nén cả entry, bỏ qua giới hạn read() -> từ chối
                    raise ValueError("kiểu nén không hỗ trợ")
                with z.open(info) as f:
                    xml = f.read(10 * 1024 * 1024 + 1)  # đọc có giới hạn: chặn DOCX zip bomb
            if len(xml) > 10 * 1024 * 1024:
                raise ValueError("document.xml quá lớn")
            xml_text = xml.decode("utf-8", errors="replace")
            xml_text = re.sub(r"</w:p>|<w:br[^<>]*/>|<w:cr/>", "\n", xml_text)
            xml_text = xml_text.replace("<w:tab/>", " ")
            text = unescape(re.sub(r"<[^<>]*>", "", xml_text))  # [^<>] để không backtrack bậc hai
    except Exception:  # noqa: BLE001 - file hỏng/mã hoá/zip bomb -> báo lỗi thân thiện
        raise HTTPException(400, "File CV bị lỗi, em không mở được ạ")
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()[:20000]
    if len(text) < 30:
        raise HTTPException(400, "Em không đọc được chữ trong file này ạ, công chúa dán nội dung "
                                 "vào ô Hồ sơ giúp em nhé")
    filename = (file.filename or "cv")[:200]
    store = get_store()
    with store.conn:
        store.conn.execute(
            "INSERT INTO user_profiles(owner, cv_text, cv_filename, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(owner) DO UPDATE SET cv_text=excluded.cv_text, "
            "cv_filename=excluded.cv_filename, updated_at=excluded.updated_at",
            (owner, text, filename, utcnow().isoformat()))
    return {"cv_filename": filename, "cv_chars": len(text), "preview": text[:300]}
