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
import logging
import os
import re
import secrets
import zipfile
from html import unescape
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

from ..assistant.agent import run_agent
from ..domain.models import utcnow
from .app import _uid, current_user, get_store, templates

router = APIRouter()
GUEST_COOKIE = "vnjobs_guest"
GUEST_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")  # secrets.token_urlsafe(24)
MAX_CV_BYTES = 5 * 1024 * 1024
HISTORY_MESSAGES = 10
log = logging.getLogger(__name__)


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


@router.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request, user: dict = Depends(current_user)):
    resp = templates.TemplateResponse(request, "assistant.html", {"user": user})
    if user.get("guest") and not GUEST_TOKEN.fullmatch(request.cookies.get(GUEST_COOKIE, "")):
        # khách (public mode): cookie riêng làm owner cho lịch sử/hồ sơ, sống 1 ngày như dữ liệu chat
        resp.set_cookie(GUEST_COOKIE, secrets.token_urlsafe(24), httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", max_age=86400)
    return resp


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
    Route sync để FastAPI chạy trong threadpool (parse không chặn event loop).
    Khách (public mode) không được upload: parse PDF vẫn tốn CPU với file dựng sẵn (Form XObject
    gọi lặp, mảng /Contents) nên chỉ người đã đăng nhập mới tải CV; khách vẫn gõ phần giới thiệu."""
    if owner.startswith("guest:"):
        raise HTTPException(403, "Công chúa đăng nhập để tải CV lên giúp em nhé 💐")
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
            from pypdf import PdfReader, filters
            # PDF bomb (20KB inflate thành hàng trăm MB, parse hàng chục giây): stream giải nén > 4MB thì
            # pypdf ném lỗi -> 400; chỉ đọc 10 trang đầu và dừng khi đủ 20.000 ký tự (đúng mức lưu bên dưới)
            filters.ZLIB_MAX_OUTPUT_LENGTH = 4 * 1024 * 1024
            pages, total = [], 0
            for i, page in enumerate(PdfReader(io.BytesIO(data)).pages):
                if i >= 10 or total >= 20000:
                    break
                pages.append(page.extract_text() or "")
                total += len(pages[-1])
            text = "\n".join(pages)
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


@router.post("/api/assistant/chat", dependencies=[Depends(same_origin)])
async def assistant_chat(request: Request, owner: str = Depends(chat_owner)):
    """Body {conversation_id?, message, deep_think} -> text/event-stream.

    Lỗi biết trước khi stream (body sai 400, quota 429, hội thoại lạ 404) trả JSON {detail}."""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Dữ liệu không hợp lệ")
    body = body if isinstance(body, dict) else {}
    message = str(body.get("message") or "").strip()
    if not 1 <= len(message) <= 4000:
        raise HTTPException(400, "Tin nhắn cần từ 1 đến 4000 ký tự ạ")
    deep_think = bool(body.get("deep_think"))
    store = get_store()
    now = utcnow().isoformat()
    day = now[:10]
    used = store.conn.execute("SELECT messages FROM assistant_usage WHERE owner=? AND day=?",
                              (owner, day)).fetchone()
    if (used["messages"] if used else 0) >= int(os.environ.get("ASSISTANT_DAILY_MESSAGES") or 100) or (
            # khách tự chọn cookie (= owner mới, quota mới) nên còn có trần chung cho mọi khách trong ngày
            owner.startswith("guest:") and store.conn.execute(
                "SELECT COALESCE(SUM(messages), 0) FROM assistant_usage WHERE owner LIKE 'guest:%' AND day=?",
                (day,)).fetchone()[0] >= int(os.environ.get("ASSISTANT_GUEST_DAILY_MESSAGES") or 300)):
        raise HTTPException(429, "Hôm nay công chúa hỏi em nhiều quá rồi, mai mình nói tiếp nha 💐")
    conv_id = body.get("conversation_id")
    new_conv = conv_id is None
    if not new_conv:
        conv = store.conn.execute("SELECT id, title FROM chat_conversations WHERE id=? AND owner=?",
                                  (conv_id, owner)).fetchone()
        if not conv:
            raise HTTPException(404, "Không tìm thấy hội thoại")
        conv_id, title = conv["id"], conv["title"]
        history = [{"role": r["role"], "content": r["content"]} for r in store.conn.execute(
            "SELECT role, content FROM (SELECT id, role, content FROM chat_messages "
            "WHERE conversation_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (conv_id, HISTORY_MESSAGES))]
    else:
        title = message[:60]
        with store.conn:
            conv_id = store.conn.execute(
                "INSERT INTO chat_conversations(owner, title, created_at, updated_at) VALUES (?,?,?,?)",
                (owner, title, now, now)).lastrowid
        history = []
    with store.conn:
        user_mid = store.conn.execute(
            "INSERT INTO chat_messages(conversation_id, role, content, created_at) VALUES (?,?,?,?)",
            (conv_id, "user", message, now)).lastrowid
        store.conn.execute("UPDATE chat_conversations SET updated_at=? WHERE id=?", (now, conv_id))
        store.conn.execute(
            "INSERT INTO assistant_usage(owner, day, messages) VALUES (?,?,1) "
            "ON CONFLICT(owner, day) DO UPDATE SET messages = messages + 1", (owner, day))
    profile = store.conn.execute("SELECT about, cv_text FROM user_profiles WHERE owner=?",
                                 (owner,)).fetchone()

    async def stream():
        answered = False
        try:
            yield f"event: conversation\ndata: {json.dumps({'id': conv_id, 'title': title}, ensure_ascii=False)}\n\n"
            async for name, data in run_agent(store, owner, history, message,
                                              dict(profile) if profile else None, deep_think):
                if name == "final":
                    ts = utcnow().isoformat()
                    with store.conn:
                        mid = store.conn.execute(
                            "INSERT INTO chat_messages(conversation_id, role, content, meta_json, created_at) "
                            "VALUES (?,?,?,?,?)",
                            (conv_id, "assistant", data["content"],
                             json.dumps(data["meta"], ensure_ascii=False), ts)).lastrowid
                        store.conn.execute("UPDATE chat_conversations SET updated_at=? WHERE id=?",
                                           (ts, conv_id))
                    answered = True
                    name, data = "done", {"message_id": mid}
                elif name == "ping":  # comment SSE: giữ kết nối qua proxy, chat.js bỏ qua (không có dòng event:)
                    yield ": ping\n\n"
                    continue
                yield f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        except Exception:  # noqa: BLE001 - lỗi lạ không được làm treo UI đang chờ stream
            log.exception("assistant chat failed")
            err = {"code": "llm_error", "message": "Em gặp lỗi rồi, công chúa thử lại giúp em nha 🥺"}
            yield f"event: error\ndata: {json.dumps(err, ensure_ascii=False)}\n\n"
        finally:
            # Lỗi / bấm Dừng / rớt mạng: bỏ câu hỏi chưa có trả lời (lịch sử chỉ giữ lượt trọn vẹn, "Thử lại"
            # không thành 2 câu hỏi trùng, model không thấy 2 câu user liền nhau); hội thoại vừa tạo mà trống thì
            # bỏ luôn (chat.js cũng quên id của nó)
            if not answered:
                with store.conn:
                    store.conn.execute("DELETE FROM chat_messages WHERE id=?", (user_mid,))
                    if new_conv:
                        store.conn.execute(
                            "DELETE FROM chat_conversations WHERE id=? AND NOT EXISTS "
                            "(SELECT 1 FROM chat_messages WHERE conversation_id=?)", (conv_id, conv_id))

    # Client đóng tab/bấm Dừng -> Starlette ngừng gọi generator (finally ở trên vẫn chạy), câu trả lời dở không được lưu
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
