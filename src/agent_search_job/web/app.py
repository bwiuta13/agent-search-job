"""Website FastAPI: invite-only search UI tiếng Việt + REST API + MCP route.

Theo docs/research-website.md mục 10 (UX) và implementation-plan.md mục 5 (API):
- Mọi route sản phẩm yêu cầu login: email được mời + mã truy cập (users.token),
  session lưu trong SQLite (bảng sessions, hết hạn sau 7 ngày).
- noindex toàn site, không sitemap.
- REST: /api/jobs, /api/jobs/:id, /api/companies/:id, /api/companies/:id/feedback,
  /api/facets, /api/me/saved-jobs.
- MCP: POST /mcp (Streamable HTTP, bearer token).
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta
import re
import secrets
from html import unescape
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..domain.models import utcnow
from ..mcp.contracts import SearchFilters, ValidationError
from ..mcp.server import MCPServer, PROTOCOL_VERSION
from ..storage.store import JobStore

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "vnjobs_session"
SESSION_DAYS = 7


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Dọn hội thoại trợ lý AI quá hạn khi khởi động và mỗi giờ — chat bị xoá sau TTL kể cả
    khi không ai gọi API (dữ liệu không nằm lại trong DB quá 24 giờ + tối đa 1 giờ).
    Quét định kỳ (SCHEDULED_SCAN_ENABLED=1): quét nguồn -> xoá job hết hạn/ngừng
    tuyển -> gửi mail job mới, đúng các mốc SCAN_TIMES (08:00, 20:00 giờ Việt Nam)."""
    ttl = int(os.environ.get("ASSISTANT_CHAT_TTL_HOURS") or 24)
    get_store().purge_expired_chat(ttl)

    async def sweep():
        while True:
            await asyncio.sleep(3600)
            try:
                get_store().purge_expired_chat(ttl)
            except sqlite3.Error:  # DB bận -> thử lại giờ sau
                pass

    task = asyncio.create_task(sweep())

    # vòng lặp quét định kỳ: None nếu SCHEDULED_SCAN_ENABLED != 1 (dùng cron/scripts)
    from ..pipeline.scheduled_scan import ScheduledScanManager
    scan_task = await ScheduledScanManager.start_loop(get_store())
    # xoá hẳn job quá hạn nộp mỗi EXPIRED_PURGE_HOURS (6) giờ, độc lập với lịch quét
    purge_hours = int(os.environ.get("EXPIRED_PURGE_HOURS") or 6)
    purge_task = (asyncio.create_task(ScheduledScanManager.purge_expired_loop(get_store(), purge_hours))
                  if purge_hours > 0 else None)

    yield
    task.cancel()
    if scan_task is not None:
        scan_task.cancel()
    if purge_task is not None:
        purge_task.cancel()


app = FastAPI(title="Agent Search Job", version="0.1.0", docs_url=None, redoc_url=None,
              lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# .mjs (DOMPurify ESM) phải là JS cho <script type=module>; .glb cho nhân vật 3D sau này
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("model/gltf-binary", ".glb")
# CSS, icon sprite (Lucide), favicon — public, không chứa dữ liệu
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_JD_MARKER = re.compile(r"^(?:[-–•●▪◦·*+✓✔➢➤►»]+|\d{1,2}[.)])\s*")


def _jd_blocks(text: Optional[str]) -> list[dict]:
    """Filter Jinja `jd_blocks`: JD thô -> khối {kind: h|p|ul} để render gọn.

    Gộp dòng trống, bỏ khoảng trắng đầu dòng, tag/entity HTML sót và mảnh
    thuộc tính bị cắt (vd. `nhân viên bán hàng">`). Dòng có ký hiệu đầu dòng
    hoặc chuỗi >= 2 dòng ngắn liền nhau -> bullet; dòng kết thúc ':' / mục đánh
    số ngắn / dòng IN HOA ngắn -> tiêu đề nhỏ; còn lại -> đoạn văn.
    """
    text = re.sub(r"<[^>]*>", " ", unescape(text or "")).replace("\xa0", " ")
    rows: list[list] = []  # [kind, text]; kind "x" = dòng thường chưa phân loại
    for ln in text.splitlines():
        ln = re.sub(r"\s+", " ", ln).strip()
        if not ln or re.fullmatch(r'[^"<>]{0,120}"\s*/?>', ln):
            continue
        m = _JD_MARKER.match(ln)
        body = ln[m.end():] if m else ln
        numbered = bool(m) and ln[0].isdigit()
        if not body:
            continue
        if ((ln.endswith(":") and len(ln) <= 100)
                or (numbered and len(body) <= 80 and body[-1] not in ".;,")
                or (not m and len(ln) <= 60 and ln.isupper())):
            rows.append(["h", ln.rstrip(":") if not numbered else ln])
        else:
            rows.append(["li" if m else "x", body])
    # chuỗi dòng thường liền nhau: >= 2 dòng, đều ngắn -> bullet; ngược lại -> đoạn
    i = 0
    while i < len(rows):
        if rows[i][0] != "x":
            i += 1
            continue
        j = i
        while j < len(rows) and rows[j][0] == "x":
            j += 1
        as_list = j - i >= 2 and all(len(r[1]) <= 300 for r in rows[i:j])
        for r in rows[i:j]:
            r[0] = "li" if as_list else "p"
        i = j
    blocks: list[dict] = []
    for kind, t in rows:
        if kind == "li":
            if blocks and blocks[-1]["kind"] == "ul":
                blocks[-1]["items"].append(t)
            else:
                blocks.append({"kind": "ul", "items": [t]})
        else:
            blocks.append({"kind": kind, "text": t})
    return blocks


templates.env.filters["jd_blocks"] = _jd_blocks


def _relative_time(iso: Optional[str]) -> str:
    """Filter Jinja `relative_time`: ISO -> '23 phút trước / Hôm qua / 3 ngày trước'.

    first_seen_at là UTC (isoformat +00:00); so với now UTC.
    """
    if not iso:
        return ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
    except ValueError:
        return iso[:10]
    if secs < 0:
        secs = 0
    if secs < 3600:
        return "vài phút trước"
    if secs < 86400:
        h = secs // 3600
        return f"{int(h)} giờ trước"
    d = secs // 86400
    if d == 1:
        return "Hôm qua"
    if d < 30:
        return f"{int(d)} ngày trước"
    return f"{int(d // 30)} tháng trước"


templates.env.filters["relative_time"] = _relative_time
templates.env.globals["relative_time"] = _relative_time  # macro gọi như hàm
from datetime import date as _date  # noqa: E402

templates.env.globals["now_date"] = _date.today().isoformat()

# Nhãn tiếng Việt cho các giá trị lọc (sidebar, dropdown, chip trên thẻ việc)
from ..domain.models import (  # noqa: E402
    JOB_LEVEL_LABELS_VI, ROLE_CLUSTER_LABELS_VI,
)

SOURCE_LABELS_VI: dict[str, str] = {
    "vietnamworks": "VietnamWorks", "linkedin": "LinkedIn", "itviec": "ITviec",
    "careerviet": "CareerViet", "topcv": "TopCV", "topdev": "TopDev",
    "vng": "VNG Careers", "all": "Mọi nguồn",
}
WORK_MODE_LABELS_VI: dict[str, str] = {
    "onsite": "Tại văn phòng", "hybrid": "Hybrid", "remote": "Remote",
}


def _vi_label(value: Optional[str], mapping: dict) -> str:
    """Giá trị lọc -> nhãn tiếng Việt (không có trong map thì giữ nguyên)."""
    if not value:
        return ""
    return mapping.get(value, value)


templates.env.globals["role_label"] = lambda v: _vi_label(v, ROLE_CLUSTER_LABELS_VI)
templates.env.globals["level_label"] = lambda v: _vi_label(v, JOB_LEVEL_LABELS_VI)
templates.env.globals["source_label"] = lambda v: _vi_label(v, SOURCE_LABELS_VI)
templates.env.globals["work_mode_label"] = lambda v: _vi_label(v, WORK_MODE_LABELS_VI)
# ?v=<mtime> cho file static: sửa CSS/icon là trình duyệt tải bản mới, không dính cache cũ
templates.env.globals["asset_v"] = lambda name: int((STATIC_DIR / name).stat().st_mtime)

_store: Optional[JobStore] = None
_mcp: Optional[MCPServer] = None


def get_store() -> JobStore:
    global _store
    if _store is None:
        db_path = os.environ.get("JOBS_DB", "data/jobs.db")
        _store = JobStore(db_path)
    return _store


def get_mcp() -> MCPServer:
    global _mcp
    if _mcp is None:
        _mcp = MCPServer(get_store())
    return _mcp


# ---------------------------------------------------------------------------
# Auth (invite-only: email + mã truy cập; MCP/API dùng cùng mã làm bearer token)
# ---------------------------------------------------------------------------


def _public_mode() -> bool:
    """ALLOWLIST_EMAILS được set nhưng rỗng -> website + API + MCP public, không cần đăng nhập.

    Không set biến -> giữ invite-only như cũ (mặc định friend@example.com).
    """
    allowlist = os.environ.get("ALLOWLIST_EMAILS")
    return allowlist is not None and not allowlist.replace(",", "").strip()


def current_user(
    request: Request,
    vnjobs_session: Optional[str] = Cookie(default=None),
) -> dict:
    token = vnjobs_session or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    # chế độ public: không có/không hợp lệ token -> khách (email rỗng), vẫn dùng được site
    guest = {"email": "", "token": "", "guest": True}
    if not token:
        if _public_mode():
            return guest
        raise HTTPException(401, "Cần đăng nhập")
    store = get_store()
    # cookie session (còn hạn) trước, rồi tới bearer token = users.token
    row = store.conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.user_id=s.user_id "
        "WHERE s.token=? AND s.expires_at > ?", (token, utcnow().isoformat()),
    ).fetchone() or store.verify_token(token)
    if not row:
        if _public_mode():
            return guest
        raise HTTPException(401, "Token không hợp lệ")
    return {"email": row["email"], "token": token}


# ---------------------------------------------------------------------------
# Security headers + noindex (website kín)
# ---------------------------------------------------------------------------

@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Trang chủ: login nếu chưa có session, redirect sang search nếu có."""
    try:
        current_user(request, request.cookies.get(SESSION_COOKIE))
        return RedirectResponse("/search", status_code=302)
    except HTTPException:
        return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    code = str(form.get("code", "")).strip()
    store = get_store()
    row = store.conn.execute(
        "SELECT * FROM users WHERE email=?", (email,)
    ).fetchone()
    # cùng một lỗi cho "chưa mời" và "sai mã" để không dò được email nào đã được mời
    if not row or not code or not secrets.compare_digest(row["token"], code):
        return RedirectResponse("/?error=1", status_code=302)
    session = secrets.token_urlsafe(32)
    now = utcnow()
    with store.conn:
        store.conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now.isoformat(),))
        store.conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (session, row["user_id"], (now + timedelta(days=SESSION_DAYS)).isoformat()),
        )
    resp = RedirectResponse("/search", status_code=302)
    # sau Caddy (X-Forwarded-Proto từ 127.0.0.1) scheme là https -> cookie chỉ gửi qua HTTPS
    resp.set_cookie(
        SESSION_COOKIE, session, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", max_age=86400 * SESSION_DAYS,
    )
    return resp


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = "",
    role: str = "",
    district: str = "",
    work_mode: str = "",
    job_level: str = "",
    company: str = "",
    salary_min: str = "",
    source: str = "all",
    page: int = 1,
    user: dict = Depends(current_user),
):
    page_size = 25
    page = max(page, 1)
    store = get_store()
    filters = SearchFilters(
        query=q or None,
        role_clusters=[role] if role else None,
        districts=[district] if district else None,
        work_modes=[work_mode] if work_mode else None,
        job_levels=[job_level] if job_level else None,
        company_query=company.strip() or None,
        salary_min_vnd=int(salary_min) if salary_min.isdigit() else None,
        source=source,
    ).validate()
    res = store.search_jobs(
        query=filters.query, role_clusters=filters.role_clusters,
        districts=filters.districts, work_modes=filters.work_modes,
        job_levels=filters.job_levels, company_query=filters.company_query,
        salary_min_vnd=filters.salary_min_vnd, source=filters.source,
        page_size=page_size, cursor=str((page - 1) * page_size),
        posted_within_days=60,
    )
    facets = store.get_facets()
    # query string giữ nguyên filter khi chuyển trang (bỏ tham số rỗng / mặc định)
    page_qs = urlencode({k: v for k, v in {
        "q": q, "role": role, "district": district, "work_mode": work_mode,
        "job_level": job_level, "company": company,
        "salary_min": salary_min, "source": "" if source == "all" else source,
    }.items() if v})
    return templates.TemplateResponse(request, "search.html", {
        "user": user,
        "items": res["items"],
        "total": res["total_estimate"],
        "facets": facets,
        "q": q, "role": role, "district": district,
        "work_mode": work_mode, "job_level": job_level, "company": company,
        "salary_min": salary_min, "source": source,
        "page": page, "pages": max(-(-res["total_estimate"] // page_size), 1),
        "page_qs": page_qs,
    })


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int, user: dict = Depends(current_user)):
    from ..pipeline.llm_enrich import LLMConfig
    store = get_store()
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy việc làm")
    # Không chờ LLM: có cache thì hiện luôn, chưa có thì trang render ngay với
    # khối "AI đang phân tích" và JS tải /jobs/{id}/analysis sau.
    analysis = await get_jd_analysis(store, job, cached_only=True)
    return templates.TemplateResponse(request, "job_detail.html", {
        "user": user, "job": job, "analysis": analysis,
        "analysis_pending": (analysis is None and LLMConfig.from_env().ok
                             and bool(job.get("description") or job.get("requirements"))),
    })


@app.get("/jobs/{job_id}/analysis", response_class=HTMLResponse)
async def job_analysis_fragment(
    request: Request, job_id: int, user: dict = Depends(current_user)
):
    """Khối phân tích JD (HTML) cho trang chi tiết tải bất đồng bộ; 204 nếu LLM tắt/lỗi."""
    store = get_store()
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy việc làm")
    analysis = await get_jd_analysis(store, job)
    if not analysis:
        return Response(status_code=204)
    return templates.TemplateResponse(request, "_jd_analysis.html", {"analysis": analysis})


async def get_jd_analysis(
    store: JobStore, job: dict, cached_only: bool = False
) -> Optional[dict]:
    """Phân tích JD bằng LLM với cache trong bảng llm_enrichment (kind='jd_analysis').

    cached_only=True: chỉ đọc cache, không gọi LLM (dùng khi render trang).
    """
    from ..pipeline.llm_enrich import LLMConfig, PROMPT_VERSION
    from ..pipeline.jd_analysis import JDAnalyzer
    cfg = LLMConfig.from_env()
    if not cfg.ok:
        return None
    cached = store.get_llm_cache(job["job_id"], cfg.model, PROMPT_VERSION, "jd_analysis")
    if cached or cached_only:
        return cached or None
    result = await JDAnalyzer(cfg).analyze(job)
    if result is None:
        return None
    payload = result.to_dict()
    store.set_llm_cache(job["job_id"], cfg.model, PROMPT_VERSION, "jd_analysis", payload)
    return payload


@app.get("/companies/{company_id}", response_class=HTMLResponse)
async def company_page(
    request: Request, company_id: int, user: dict = Depends(current_user)
):
    store = get_store()
    company = store.get_company(company_id)
    if not company:
        raise HTTPException(404, "Không tìm thấy công ty")
    feedback = store.get_feedback(company_id)
    open_jobs = store.company_open_jobs(company_id)
    return templates.TemplateResponse(request, "company.html", {
        "user": user, "company": company, "feedback": feedback, "open_jobs": open_jobs,
    })


@app.get("/saved", response_class=HTMLResponse)
async def saved_page(request: Request, user: dict = Depends(current_user)):
    store = get_store()
    items = store.saved_jobs_for_user(_uid(store, user["email"]))
    return templates.TemplateResponse(request, "saved.html", {"user": user, "items": items})


@app.get("/health")
async def health():
    store = get_store()
    return {"status": "ok", "stats": store.stats()}


# ---------------------------------------------------------------------------
# REST API (cùng domain service với MCP)
# ---------------------------------------------------------------------------

@app.get("/api/jobs")
async def api_jobs(
    request: Request,
    query: Optional[str] = None,
    role_clusters: Optional[str] = None,
    districts: Optional[str] = None,
    work_modes: Optional[str] = None,
    job_levels: Optional[str] = None,
    company_query: Optional[str] = None,
    salary_min_vnd: Optional[int] = None,
    experience_max_years: Optional[float] = None,
    posted_within_days: int = 14,
    portfolio_required: Optional[bool] = None,
    source: str = "all",
    page_size: int = 10,
    cursor: Optional[str] = None,
    user: dict = Depends(current_user),
):
    try:
        f = SearchFilters(
            query=query,
            role_clusters=role_clusters.split(",") if role_clusters else None,
            districts=districts.split(",") if districts else None,
            work_modes=work_modes.split(",") if work_modes else None,
            job_levels=job_levels.split(",") if job_levels else None,
            company_query=company_query or None,
            salary_min_vnd=salary_min_vnd,
            experience_max_years=experience_max_years,
            posted_within_days=posted_within_days,
            portfolio_required=portfolio_required,
            source=source, page_size=page_size, cursor=cursor,
        ).validate()
    except ValidationError as e:
        raise HTTPException(422, str(e))
    store = get_store()
    return store.search_jobs(
        query=f.query, role_clusters=f.role_clusters, districts=f.districts,
        work_modes=f.work_modes, job_levels=f.job_levels,
        company_query=f.company_query, salary_min_vnd=f.salary_min_vnd,
        experience_max_years=f.experience_max_years,
        posted_within_days=f.posted_within_days,
        portfolio_required=f.portfolio_required, source=f.source,
        page_size=f.page_size, cursor=f.cursor,
    )


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: int, user: dict = Depends(current_user)):
    store = get_store()
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy")
    analysis = await get_jd_analysis(store, job)
    if analysis:
        job["jd_analysis"] = analysis
    return job


@app.get("/api/companies/{company_id}")
async def api_company(company_id: int, user: dict = Depends(current_user)):
    store = get_store()
    company = store.get_company(company_id)
    if not company:
        raise HTTPException(404, "Không tìm thấy")
    return {"company": company, "open_jobs": store.company_open_jobs(company_id)}


@app.get("/api/companies/{company_id}/feedback")
async def api_company_feedback(company_id: int, user: dict = Depends(current_user)):
    return {"feedback": get_store().get_feedback(company_id)}


@app.get("/companies", response_class=HTMLResponse)
async def companies_page(
    request: Request, q: str = "", user: dict = Depends(current_user),
):
    """Trang danh sách công ty đang tuyển + tìm theo tên."""
    store = get_store()
    facets = store.get_facets()
    comps = facets.get("companies") or []
    if q:
        ql = q.lower()
        comps = [c for c in comps if ql in c["value"].lower()]
    # job đếm theo công ty đã có trong facets; gán company_id nếu tìm được
    return templates.TemplateResponse(request, "companies.html", {
        "user": user, "companies": comps[:50], "q": q,
    })


@app.get("/api/hiring-companies")
async def api_companies_search(
    q: str = "", limit: int = 50, user: dict = Depends(current_user),
):
    """API: công ty đang tuyển (tên + số job mở) và job của công ty đó."""
    store = get_store()
    facets = store.get_facets()
    comps = facets.get("companies") or []
    if q:
        ql = q.lower()
        comps = [c for c in comps if ql in c["value"].lower()]
    out = []
    for c in comps[:max(1, min(limit, 100))]:
        jobs = store.search_company_jobs(c["value"], limit=25)
        out.append({
            "company_name": c["value"], "open_jobs": c["count"],
            "jobs": [{"job_id": j["job_id"], "title": j["title"],
                      "salary_text": j["salary_text"], "district": j["district"],
                      "source": j["source"]} for j in jobs[:10]],
        })
    return {"companies": out}


@app.post("/rescan")
async def rescan(request: Request, user: dict = Depends(current_user)):
    """User chủ động quét lại nguồn tùy thích (chạy nền, không chặn request)."""
    from ..pipeline.rescan import RescanManager
    form = await request.form()
    source = str(form.get("source") or "all")
    if source not in ("all", "vietnamworks", "itviec", "careerviet", "linkedin",
                      "topdev", "topcv", "vng"):
        source = "all"
    query = str(form.get("query") or "content marketing").strip()[:100]
    try:
        max_results = max(1, min(50, int(form.get("max_results") or 15)))
    except (TypeError, ValueError):
        max_results = 15
    st = await RescanManager.start(get_store(), source, query, max_results)
    return RedirectResponse(
        f"/search?rescan={'running' if st.running else 'busy'}&source={source}",
        status_code=303,
    )


@app.get("/api/rescan/status")
async def rescan_status(user: dict = Depends(current_user)):
    from ..pipeline.rescan import RescanManager
    st = RescanManager.status()
    return {
        "running": st.running, "source": st.source,
        "started_at": st.started_at, "finished_at": st.finished_at,
        "message": st.message, "ok": st.ok, "log": st.log, "summary": st.summary,
    }


@app.get("/api/scheduled-scan/status")
async def scheduled_scan_status(user: dict = Depends(current_user)):
    """Trạng thái vòng lặp quét định kỳ (12h): cấu hình, lượt gần nhất, 10 lượt trước."""
    from ..pipeline.scheduled_scan import ScheduledScanManager, last_scans
    out = ScheduledScanManager.status()
    out["history"] = last_scans(get_store(), limit=10)
    return out


@app.get("/api/facets")
async def api_facets(user: dict = Depends(current_user)):
    return get_store().get_facets()


@app.post("/api/me/saved-jobs")
async def api_save_job(request: Request, user: dict = Depends(current_user)):
    body = await request.json()
    job_id = body.get("job_id")
    if not isinstance(job_id, int):
        raise HTTPException(422, "job_id phải là số nguyên")
    if user.get("guest"):
        raise HTTPException(401, "Cần đăng nhập để lưu việc")
    store = get_store()
    if not store.get_job(job_id):
        raise HTTPException(404, "Không tìm thấy job")
    ok = store.save_job_for_user(_uid(store, user["email"]), job_id)
    return {"saved": ok}


@app.delete("/api/me/saved-jobs/{job_id}")
async def api_unsave_job(job_id: int, user: dict = Depends(current_user)):
    store = get_store()
    ok = store.unsave_job_for_user(_uid(store, user["email"]), job_id)
    return {"removed": ok}


@app.get("/api/me/saved-jobs")
async def api_saved_jobs(user: dict = Depends(current_user)):
    store = get_store()
    return {"items": store.saved_jobs_for_user(_uid(store, user["email"]))}


# ---------------------------------------------------------------------------
# MCP endpoint (Streamable HTTP)
# ---------------------------------------------------------------------------

@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP Streamable HTTP: kiểm tra Origin (nếu có), bearer token, rồi dispatch."""
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        from urllib.parse import urlparse
        if urlparse(origin).netloc != host:
            return JSONResponse({"error": "Origin không được phép"}, status_code=403)
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not token and not _public_mode():
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Thiếu bearer token"}},
            status_code=401,
        )
    store = get_store()
    if not _public_mode() and not store.verify_token(token):
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32002, "message": "Token không hợp lệ"}},
            status_code=403,
        )
    body = await request.json()
    if isinstance(body, list):  # batch
        results = []
        for msg in body:
            r = await get_mcp().handle(msg)
            if r:
                results.append(r)
        return JSONResponse(results, headers={"mcp-protocol-version": PROTOCOL_VERSION})
    result = await get_mcp().handle(body)
    if result is None:
        return Response(status_code=202)
    return JSONResponse(result, headers={"mcp-protocol-version": PROTOCOL_VERSION})


def _uid(store: JobStore, email: str) -> int:
    row = store.conn.execute("SELECT user_id FROM users WHERE email=?", (email,)).fetchone()
    return row["user_id"] if row else 0


# Trợ lý AI (/assistant) — import cuối file vì assistant_routes dùng get_store/current_user/templates ở trên
from .assistant_routes import router as assistant_router  # noqa: E402

app.include_router(assistant_router)
