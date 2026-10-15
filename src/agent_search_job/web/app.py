"""Website FastAPI: invite-only search UI tiếng Việt + REST API + MCP route.

Theo docs/research-website.md mục 10 (UX) và implementation-plan.md mục 5 (API):
- Mọi route sản phẩm yêu cầu login (MVP: token / cookie, allowlist email env).
- noindex toàn site, không sitemap.
- REST: /api/jobs, /api/jobs/:id, /api/companies/:id, /api/companies/:id/feedback,
  /api/facets, /api/me/saved-jobs.
- MCP: POST /mcp (Streamable HTTP, bearer token).
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..mcp.contracts import SearchFilters, ValidationError
from ..mcp.server import MCPServer, PROTOCOL_VERSION
from ..storage.store import JobStore

TEMPLATES_DIR = Path(__file__).parent / "templates"
SESSION_COOKIE = "vnjobs_session"

app = FastAPI(title="Agent Search Job", version="0.1.0", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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
# Auth (invite-only MVP)
# ---------------------------------------------------------------------------

def _issue_session(email: str, response: Response) -> str:
    token = secrets.token_urlsafe(32)
    # lưu session in-memory MVP; deploy thật dùng store riêng + TTL
    _SESSIONS[token] = email
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=86400 * 7,
    )
    return token


_SESSIONS: dict[str, str] = {}


def current_user(
    request: Request,
    vnjobs_session: Optional[str] = Cookie(default=None),
) -> dict:
    token = vnjobs_session or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Cần đăng nhập")
    email = _SESSIONS.get(token)
    if not email:
        store = get_store()
        row = store.verify_token(token)
        if not row:
            raise HTTPException(401, "Token không hợp lệ")
        email = row["email"]
        _SESSIONS[token] = email
    return {"email": email, "token": token}


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
    if not email:
        return RedirectResponse("/?error=1", status_code=302)
    store = get_store()
    row = store.conn.execute(
        "SELECT * FROM users WHERE email=?", (email,)
    ).fetchone()
    if not row:
        return RedirectResponse("/?error=not_invited", status_code=302)
    resp = RedirectResponse("/search", status_code=302)
    _issue_session(email, resp)
    return resp


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = "",
    role: str = "",
    district: str = "",
    work_mode: str = "",
    salary_min: str = "",
    source: str = "all",
    user: dict = Depends(current_user),
):
    store = get_store()
    filters = SearchFilters(
        query=q or None,
        role_clusters=[role] if role else None,
        districts=[district] if district else None,
        work_modes=[work_mode] if work_mode else None,
        salary_min_vnd=int(salary_min) if salary_min.isdigit() else None,
        source=source,
    ).validate()
    res = store.search_jobs(
        query=filters.query, role_clusters=filters.role_clusters,
        districts=filters.districts, work_modes=filters.work_modes,
        salary_min_vnd=filters.salary_min_vnd, source=filters.source,
        page_size=25, posted_within_days=60,
    )
    facets = store.get_facets()
    return templates.TemplateResponse(request, "search.html", {
        "user": user,
        "items": res["items"],
        "total": res["total_estimate"],
        "facets": facets,
        "q": q, "role": role, "district": district,
        "work_mode": work_mode, "salary_min": salary_min, "source": source,
    })


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int, user: dict = Depends(current_user)):
    store = get_store()
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy việc làm")
    return templates.TemplateResponse(request, "job_detail.html", {"user": user, "job": job})


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
        work_modes=f.work_modes, salary_min_vnd=f.salary_min_vnd,
        experience_max_years=f.experience_max_years,
        posted_within_days=f.posted_within_days,
        portfolio_required=f.portfolio_required, source=f.source,
        page_size=f.page_size, cursor=f.cursor,
    )


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: int, user: dict = Depends(current_user)):
    job = get_store().get_job(job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy")
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


@app.get("/api/facets")
async def api_facets(user: dict = Depends(current_user)):
    return get_store().get_facets()


@app.post("/api/me/saved-jobs")
async def api_save_job(request: Request, user: dict = Depends(current_user)):
    body = await request.json()
    job_id = body.get("job_id")
    if not isinstance(job_id, int):
        raise HTTPException(422, "job_id phải là số nguyên")
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
    if not token:
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Thiếu bearer token"}},
            status_code=401,
        )
    store = get_store()
    if not store.verify_token(token):
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
