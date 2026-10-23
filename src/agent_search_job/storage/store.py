"""Storage layer: SQLite schema + repositories + search.

Theo docs/research-website.md mục 8 (data model cốt lõi):
jobs, job_occurrences (gộp vào jobs.source_* ở MVP SQLite), companies,
company_feedback_sources, crawl_runs, saved_jobs, users (invite-only MVP).

SQLite được chọn cho MVP thay PostgreSQL để chạy local không cần Docker;
schema giữ field tương đương để chuyển lên Postgres sau không đổi domain.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..domain.models import Company, CompanyFeedback, NormalizedJob, utcnow

log = logging.getLogger(__name__)
from ..domain.normalize import dedupe_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    company_name TEXT NOT NULL,
    company_id INTEGER DEFAULT 0,
    role_cluster TEXT NOT NULL DEFAULT 'content_marketing',
    role_cluster_confidence REAL DEFAULT 0,
    salary_min_vnd INTEGER,
    salary_max_vnd INTEGER,
    salary_text TEXT DEFAULT '',
    experience_years_min REAL,
    experience_years_max REAL,
    job_level TEXT DEFAULT '',
    work_mode TEXT DEFAULT 'onsite',
    district TEXT DEFAULT '',
    city TEXT DEFAULT '',
    is_hcm INTEGER DEFAULT 1,
    address TEXT DEFAULT '',
    platforms TEXT DEFAULT '[]',
    creative_tools TEXT DEFAULT '[]',
    portfolio_required INTEGER,
    deadline TEXT,
    description TEXT DEFAULT '',
    requirements TEXT DEFAULT '',
    benefits TEXT DEFAULT '[]',
    apply_url TEXT DEFAULT '',
    languages TEXT DEFAULT '[]',
    is_active INTEGER DEFAULT 1,
    source TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    source_job_id TEXT DEFAULT '',
    dedupe_key_src TEXT DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_role ON jobs(role_cluster);
CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(is_active);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_salary ON jobs(salary_min_vnd);
CREATE INDEX IF NOT EXISTS idx_jobs_city ON jobs(city);

CREATE TABLE IF NOT EXISTS companies (
    company_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    aliases TEXT DEFAULT '[]',
    industry TEXT DEFAULT '',
    size TEXT DEFAULT '',
    website TEXT DEFAULT '',
    address TEXT DEFAULT '',
    profile TEXT DEFAULT '',
    profile_source_url TEXT DEFAULT '',
    tax_code TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_feedback_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(company_id),
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    rating REAL,
    review_count INTEGER,
    summary TEXT DEFAULT '',
    confidence TEXT DEFAULT 'low',
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fb_company ON company_feedback_sources(company_id);

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    queries TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    result_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    token TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_enrichment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(job_id),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, model, prompt_version, kind)
);

CREATE TABLE IF NOT EXISTS saved_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    job_id INTEGER NOT NULL REFERENCES jobs(job_id),
    saved_at TEXT NOT NULL,
    UNIQUE(user_id, job_id)
);

-- Trợ lý AI (/assistant). owner = 'user:<user_id>' | 'guest:<token>'
CREATE TABLE IF NOT EXISTS chat_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_conv_owner ON chat_conversations(owner, updated_at);
CREATE INDEX IF NOT EXISTS idx_chat_conv_updated ON chat_conversations(updated_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS user_profiles (
    owner TEXT PRIMARY KEY,
    about TEXT NOT NULL DEFAULT '',
    cv_text TEXT NOT NULL DEFAULT '',
    cv_filename TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_search_cache (
    key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    payload TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assistant_usage (
    owner TEXT NOT NULL,
    day TEXT NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    web_searches INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner, day)
);
"""


class JobStore:
    """SQLite storage với thread-local connection (an toàn cho FastAPI + worker)."""

    _MEM_URI: str | None = None
    _MEM_KEEPALIVE = None

    def __init__(self, db_path: str = "jobs.db", fresh: bool = False):
        self._local = threading.local()
        if db_path == ":memory:":
            import uuid
            # mỗi instance một DB shared-cache riêng (uuid) để test cách nhau
            uri = f"file:vnjobs_{uuid.uuid4().hex}?mode=memory&cache=shared"
            type(self)._MEM_URI = uri
            self.db_path = uri
            self._keepalive = sqlite3.connect(  # giữ DB sống theo instance
                uri, uri=True, check_same_thread=False)
        else:
            self.db_path = db_path
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_schema()
        self._migrate()
        self._seed_users()

    def _migrate(self):
        """Thêm cột mới cho DB cũ - SQLite không có ADD COLUMN IF NOT EXISTS."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "job_level" not in cols:
            with self.conn:
                self.conn.execute(
                    "ALTER TABLE jobs ADD COLUMN job_level TEXT DEFAULT ''")
        # token cũ = sha256("invite:"+email)[:32] -> ai biết email cũng tính ra được;
        # đổi sang token ngẫu nhiên (token cũ hết hiệu lực, chạy lại không đổi gì)
        for row in self.conn.execute("SELECT user_id, email, token FROM users").fetchall():
            if row["token"] == hashlib.sha256(f"invite:{row['email']}".encode()).hexdigest()[:32]:
                with self.conn:
                    self.conn.execute("UPDATE users SET token=? WHERE user_id=?",
                                      (secrets.token_urlsafe(24), row["user_id"]))

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            is_uri = self.db_path.startswith("file:")
            conn = sqlite3.connect(
                self.db_path, timeout=15, uri=is_uri)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self):
        with self.conn:
            self.conn.executescript(_SCHEMA)

    def _seed_users(self):
        # invite list qua env ALLOWLIST_EMAILS (comma separated); token ngẫu nhiên vừa là
        # mã truy cập web vừa là MCP bearer token (lấy bằng scripts/show_token.py)
        emails = [e.strip().lower() for e in
                  os.environ.get("ALLOWLIST_EMAILS", "friend@example.com").split(",")
                  if e.strip()]
        for email in emails:
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO users(email, token, created_at) VALUES (?,?,?)",
                    (email, secrets.token_urlsafe(24), utcnow().isoformat()),
                )
        # Tài khoản cố định (LOGIN_EMAIL + LOGIN_CODE trong .env, vd. khi chạy Docker): mã truy cập do người vận hành đặt
        # thay vì ngẫu nhiên. Đổi LOGIN_CODE -> mã cũ hết hiệu lực và mọi phiên đăng nhập của email đó bị đăng xuất.
        # Mã < 12 ký tự bị bỏ qua (site chạy public trên Internet): email vẫn được mời, mã đang dùng giữ nguyên.
        email = os.environ.get("LOGIN_EMAIL", "").strip().lower()
        code = os.environ.get("LOGIN_CODE", "").strip()
        if not email:
            return
        if len(code) < 12:
            if code:
                log.warning("LOGIN_CODE ngắn hơn 12 ký tự nên bị bỏ qua; %s vẫn dùng mã cũ / mã ngẫu nhiên", email)
            code = ""
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO users(email, token, created_at) VALUES (?,?,?)",
                (email, code or secrets.token_urlsafe(24), utcnow().isoformat()),
            )
            if code and self.conn.execute(
                    "UPDATE users SET token=? WHERE email=? AND token!=?", (code, email, code)).rowcount:
                self.conn.execute(
                    "DELETE FROM sessions WHERE user_id=(SELECT user_id FROM users WHERE email=?)", (email,))

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def insert_job(self, job: NormalizedJob) -> int:
        job.dedupe_key = dedupe_key(job.title, job.company_name)  # type: ignore[attr-defined]
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO jobs (dedupe_key, title, company_name, company_id,
                   role_cluster, role_cluster_confidence, salary_min_vnd, salary_max_vnd,
                   salary_text, experience_years_min, experience_years_max, job_level, work_mode,
                   district, city, is_hcm, address, platforms, creative_tools,
                   portfolio_required, deadline, description, requirements, benefits,
                   apply_url, languages, is_active, source, source_url, source_job_id,
                   dedupe_key_src, first_seen_at, last_verified_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job.dedupe_key, job.title, job.company_name, job.company_id,
                    job.role_cluster, job.role_cluster_confidence,
                    job.salary_min_vnd, job.salary_max_vnd, job.salary_text,
                    job.experience_years_min, job.experience_years_max,
                    job.job_level,
                    job.work_mode, job.district, job.city,
                    int(job.is_hcm), job.address,
                    json.dumps(job.platforms), json.dumps(job.creative_tools),
                    (1 if job.portfolio_required else
                     0 if job.portfolio_required is False else None),
                    job.deadline.isoformat() if job.deadline else None,
                    job.description, job.requirements, json.dumps(job.benefits),
                    job.apply_url, json.dumps(job.languages), int(job.is_active),
                    job.source, job.source_url, job.source_job_id,
                    f"{job.source}:{job.source_job_id}",
                    job.first_seen_at.isoformat(), job.last_verified_at.isoformat(),
                ),
            )
            return cur.lastrowid

    def find_by_dedupe_key(self, key: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT job_id FROM jobs WHERE dedupe_key=?", (key,)
        ).fetchone()
        return row["job_id"] if row else None

    def update_job(self, job_id: int, job: NormalizedJob, keep_inactive: bool = False):
        """Update các field mutable; giữ first_seen_at.

        description/requirements rỗng (nguồn lần này không lấy được JD, vd. LinkedIn
        dính 429, TopCV search không có JD) -> giữ JD đã lưu, không xoá trắng.

        keep_inactive: nếu job đang inactive (bị lọc scope/out-of-HCM/hết hạn),
        crawl mới KHÔNG được bật lại is_active - tránh job đã loại quay lại.
        """
        active_flag = int(bool(job.is_active))
        if keep_inactive:
            row = self.conn.execute(
                "SELECT is_active FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row and not row["is_active"]:
                active_flag = 0  # không mutate job object
        with self.conn:
            self.conn.execute(
                """UPDATE jobs SET title=?, company_name=?, role_cluster=?,
                   role_cluster_confidence=?, salary_min_vnd=?, salary_max_vnd=?,
                   salary_text=?, experience_years_min=?, experience_years_max=?, job_level=?,
                   work_mode=?, district=?, city=?, is_hcm=?, address=?, platforms=?,
                   creative_tools=?, portfolio_required=?, deadline=?,
                   description=COALESCE(NULLIF(?, ''), description),
                   requirements=COALESCE(NULLIF(?, ''), requirements),
                   benefits=?, apply_url=?, languages=?,
                   is_active=?, source=?, source_url=?, source_job_id=?,
                   last_verified_at=?
                   WHERE job_id=?""",
                (
                    job.title, job.company_name, job.role_cluster,
                    job.role_cluster_confidence, job.salary_min_vnd,
                    job.salary_max_vnd, job.salary_text, job.experience_years_min,
                    job.experience_years_max, job.job_level,
                    job.work_mode, job.district,
                    job.city, int(job.is_hcm), job.address,
                    json.dumps(job.platforms), json.dumps(job.creative_tools),
                    (1 if job.portfolio_required else
                     0 if job.portfolio_required is False else None),
                    job.deadline.isoformat() if job.deadline else None,
                    job.description, job.requirements, json.dumps(job.benefits),
                    job.apply_url, json.dumps(job.languages), active_flag,
                    job.source, job.source_url, job.source_job_id,
                    job.last_verified_at.isoformat(), job_id,
                ),
            )

    def mark_expired_inactive(self, now: Optional[datetime] = None, stale_days: int = 0,
                              stale_sources: tuple[str, ...] = ()) -> dict:
        """Ẩn job quá hạn nộp; stale_days > 0: ẩn thêm job của stale_sources không thấy
        lại trên nguồn quá stale_days ngày (last_verified_at cũ). Chỉ truyền nguồn vừa
        quét được job, để nguồn lâu không quét không bị ẩn oan. Trả số job mới bị ẩn.
        """
        now = now or utcnow()
        with self.conn:
            expired = self.conn.execute(
                "UPDATE jobs SET is_active=0 WHERE is_active=1 AND deadline IS NOT NULL AND deadline != '' "
                "AND julianday(deadline) < julianday(?)", (now.isoformat(),),  # julianday: đúng cả khi khác múi giờ
            ).rowcount
            stale = 0
            if stale_days > 0 and stale_sources:
                stale = self.conn.execute(
                    f"UPDATE jobs SET is_active=0 WHERE is_active=1 AND last_verified_at < ? "
                    f"AND source IN ({','.join('?' * len(stale_sources))})",
                    ((now - timedelta(days=stale_days)).isoformat(), *stale_sources),
                ).rowcount
        return {"expired": expired, "stale": stale}

    def get_job(self, job_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        for k in ("platforms", "creative_tools", "benefits", "languages"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except (TypeError, json.JSONDecodeError):
                d[k] = []
        if d.get("portfolio_required") is not None:
            d["portfolio_required"] = bool(d["portfolio_required"])
        return d

    # ------------------------------------------------------------------
    # Search (đơn giản hóa so với Postgres FTS: LIKE + filter, đủ cho MVP)
    # ------------------------------------------------------------------

    def search_jobs(
        self,
        query: Optional[str] = None,
        role_clusters: Optional[list[str]] = None,
        districts: Optional[list[str]] = None,
        salary_min_vnd: Optional[int] = None,
        experience_max_years: Optional[float] = None,
        work_modes: Optional[list[str]] = None,
        job_levels: Optional[list[str]] = None,
        company_query: Optional[str] = None,
        posted_within_days: int = 14,
        portfolio_required: Optional[bool] = None,
        source: str = "all",
        page_size: int = 10,
        cursor: Optional[str] = None,
        include_inactive: bool = False,
    ) -> dict:
        where = []
        params: list = []
        if not include_inactive:
            where.append("is_active=1")
        where.append("is_hcm=1")
        if query:
            # tách từ: mỗi từ khớp riêng (OR) trong title/description/company
            import re as _re
            words = [w for w in _re.split(r"\s+", query.lower().strip()) if len(w) >= 2][:6]
            if words:
                conds = []
                for w in words:
                    like = f"%{w}%"
                    conds.append(
                        "(LOWER(title) LIKE ? OR LOWER(description) LIKE ? OR LOWER(company_name) LIKE ?)"
                    )
                    params.extend([like, like, like])
                where.append("(" + " OR ".join(conds) + ")")
        if role_clusters:
            qs = ",".join("?" * len(role_clusters))
            where.append(f"role_cluster IN ({qs})")
            params.extend(role_clusters)
        if districts:
            conds = []
            for d in districts:
                conds.append("LOWER(district) LIKE ?")
                params.append(f"%{d.lower()}%")
            where.append("(" + " OR ".join(conds) + ")")
        if salary_min_vnd is not None:
            where.append("(salary_max_vnd IS NULL OR salary_max_vnd >= ?)")
            params.append(salary_min_vnd)
        if experience_max_years is not None:
            where.append("(experience_years_min IS NULL OR experience_years_min <= ?)")
            params.append(experience_max_years)
        if work_modes:
            qs = ",".join("?" * len(work_modes))
            where.append(f"work_mode IN ({qs})")
            params.extend(work_modes)
        if job_levels:
            # job_level là chuỗi comma nhiều cấp ("junior,middle") -> match bất kỳ
            conds = []
            for lv in job_levels:
                conds.append(
                    "(job_level = ? OR job_level LIKE ? ESCAPE '\\' "
                    "OR job_level LIKE ? ESCAPE '\\')"
                )
                params.extend([lv, lv + ",%", "%," + lv])
            where.append("(" + " OR ".join(conds) + ")")
        if company_query:
            where.append("LOWER(company_name) LIKE ?")
            params.append(f"%{company_query.lower()}%")
        if portfolio_required is not None:
            where.append("portfolio_required=1" if portfolio_required else "portfolio_required=0")
        if source != "all":
            where.append("source=?")
            params.append(source)
        # freshness: last_verified_at trong posted_within_days
        cutoff = (utcnow() - timedelta(days=posted_within_days)).isoformat()
        where.append("(last_verified_at >= ? OR first_seen_at >= ?)")
        params.extend([cutoff, cutoff])
        # cursor pagination: cursor = offset (opaque string). Không dùng "job_id < last"
        # vì thứ tự sort theo relevance trước nên keyset theo job_id sẽ sót/trùng job.
        offset = 0
        if cursor:
            try:
                offset = max(int(cursor), 0)
            except ValueError:
                pass  # cursor hỏng -> bỏ qua, trả trang đầu

        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        base = f"FROM jobs{wsql}"
        total = self.conn.execute(f"SELECT COUNT(*) c {base}", params).fetchone()["c"]
        # sort: LLM relevance (nếu có) giảm dần, rồi job_id DESC (job mới trước)
        order = (
            " ORDER BY (SELECT MAX(CAST(json_extract(e.payload_json,'$.relevance') AS REAL)) "
            "FROM llm_enrichment e WHERE e.job_id=jobs.job_id AND e.kind='relevance') DESC, "
            "job_id DESC"
        )
        rows = self.conn.execute(
            f"SELECT * {base}{order} LIMIT ? OFFSET ?", (*params, page_size + 1, offset)
        ).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = []
        for r in rows:
            d = self._row_to_dict(r)
            # gắn LLM relevance vào item (nếu đã enrich)
            er = self.conn.execute(
                """SELECT payload_json FROM llm_enrichment
                   WHERE job_id=? AND kind='relevance'
                   ORDER BY id DESC LIMIT 1""",
                (d["job_id"],),
            ).fetchone()
            if er:
                try:
                    pl = json.loads(er["payload_json"])
                    d["llm_relevance"] = pl.get("relevance")
                    d["llm_reason"] = pl.get("reason")
                except (TypeError, json.JSONDecodeError):
                    pass
            items.append(d)
        next_cursor = str(offset + page_size) if has_more else None
        return {
            "items": items,
            "next_cursor": next_cursor,
            "total_estimate": total,
        }

    # ------------------------------------------------------------------
    # Facets
    # ------------------------------------------------------------------

    def get_facets(self) -> dict:
        out: dict = {"role_clusters": [], "districts": [], "work_modes": [],
                     "sources": [], "job_levels": [], "companies": []}
        for row in self.conn.execute(
            "SELECT role_cluster v, COUNT(*) c FROM jobs WHERE is_active=1 GROUP BY role_cluster ORDER BY c DESC"
        ):
            out["role_clusters"].append({"value": row["v"], "count": row["c"]})
        for row in self.conn.execute(
            "SELECT district v, COUNT(*) c FROM jobs WHERE is_active=1 AND district != '' GROUP BY district ORDER BY c DESC LIMIT 25"
        ):
            out["districts"].append({"value": row["v"], "count": row["c"]})
        for row in self.conn.execute(
            "SELECT work_mode v, COUNT(*) c FROM jobs WHERE is_active=1 GROUP BY work_mode"
        ):
            out["work_modes"].append({"value": row["v"], "count": row["c"]})
        # Dropdown nguồn: LUÔN hiện đủ 7 nguồn chuẩn (kể cả 0 job active) -
        # tránh option "biến mất" âm thầm khi nguồn đó chưa quét/quét lỗi,
        # người dùng vẫn thấy nguồn tồn tại để chọn rescan.
        src_counts: dict[str, int] = {}
        for row in self.conn.execute(
            "SELECT source v, COUNT(*) c FROM jobs WHERE is_active=1 GROUP BY source"
        ):
            src_counts[row["v"]] = row["c"]
        for src in ("vietnamworks", "linkedin", "itviec", "careerviet", "topcv", "topdev", "vng"):
            out["sources"].append({"value": src, "count": src_counts.get(src, 0)})
        # nguồn lạ ngoài danh sách chuẩn (nếu có) vẫn hiện
        for src, c in src_counts.items():
            if src not in ("vietnamworks", "linkedin", "itviec", "careerviet", "topcv", "topdev", "vng"):
                out["sources"].append({"value": src, "count": c})
        # job_level là chuỗi comma ("junior,middle") -> TÁCH thành từng cấp chuẩn,
        # đếm job match mỗi cấp (dropdown chỉ hiện 7 option cố định, không hiện giá trị gộp)
        level_counts: dict[str, int] = {}
        for row in self.conn.execute(
            "SELECT job_level v, COUNT(*) c FROM jobs WHERE is_active=1 AND job_level != '' GROUP BY job_level"
        ):
            for lv in str(row["v"]).split(","):
                lv = lv.strip()
                if lv:
                    level_counts[lv] = level_counts.get(lv, 0) + row["c"]
        order = ["intern", "fresher", "junior", "middle", "senior", "lead", "manager"]
        for lv in order:
            if lv in level_counts:
                out["job_levels"].append({"value": lv, "count": level_counts[lv]})
        for row in self.conn.execute(
            """SELECT company_name v, COUNT(*) c FROM jobs
               WHERE is_active=1 AND company_name != '' AND is_hcm=1
               GROUP BY company_name ORDER BY c DESC LIMIT 50"""
        ):
            out["companies"].append({"value": row["v"], "count": row["c"]})
        return out

    # ------------------------------------------------------------------
    # Companies + feedback
    # ------------------------------------------------------------------

    def upsert_company(self, c: Company) -> int:
        with self.conn:
            row = self.conn.execute(
                "SELECT company_id FROM companies WHERE name=?", (c.name,)
            ).fetchone()
            if row:
                self.conn.execute(
                    """UPDATE companies SET aliases=?, industry=?, size=?, website=?,
                       address=?, profile=?, profile_source_url=?, tax_code=? WHERE company_id=?""",
                    (json.dumps(c.aliases), c.industry, c.size, c.website,
                     c.address, c.profile, c.profile_source_url, c.tax_code,
                     row["company_id"]),
                )
                return row["company_id"]
            cur = self.conn.execute(
                """INSERT INTO companies (name, aliases, industry, size, website,
                   address, profile, profile_source_url, tax_code, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (c.name, json.dumps(c.aliases), c.industry, c.size, c.website,
                 c.address, c.profile, c.profile_source_url, c.tax_code,
                 utcnow().isoformat()),
            )
            return cur.lastrowid

    def get_company(self, company_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM companies WHERE company_id=?", (company_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["aliases"] = json.loads(d.get("aliases") or "[]")
        except (TypeError, json.JSONDecodeError):
            d["aliases"] = []
        return d

    def find_company_by_name(self, name: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM companies WHERE name LIKE ?", (f"%{name}%",)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["aliases"] = json.loads(d.get("aliases") or "[]")
        except (TypeError, json.JSONDecodeError):
            d["aliases"] = []
        return d

    def save_feedback(self, fb: CompanyFeedback) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO company_feedback_sources
                   (company_id, source, source_url, rating, review_count, summary,
                    confidence, fetched_at) VALUES (?,?,?,?,?,?,?,?)""",
                (fb.company_id, fb.source, fb.source_url, fb.rating,
                 fb.review_count, fb.summary, fb.confidence,
                 fb.fetched_at.isoformat()),
            )
            return cur.lastrowid

    def get_feedback(self, company_id: int) -> list[dict]:
        rows = self.conn.execute(
            """SELECT source, source_url, rating, review_count, summary, confidence,
               fetched_at FROM company_feedback_sources WHERE company_id=?
               ORDER BY review_count DESC NULLS LAST""",
            (company_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_company_jobs(self, company_name: str, limit: int = 50) -> list[dict]:
        """Tất cả job đang tuyển của công ty theo tên (LIKE)."""
        rows = self.conn.execute(
            """SELECT * FROM jobs WHERE LOWER(company_name) LIKE ? AND is_active=1
               ORDER BY job_id DESC LIMIT ?""",
            (f"%{company_name.lower()}%", limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def company_open_jobs(self, company_id: int, limit: int = 20) -> list[dict]:
        comp = self.get_company(company_id)
        if not comp:
            return []
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE company_name LIKE ? AND is_active=1 ORDER BY job_id DESC LIMIT ?",
            (f"%{comp['name']}%", limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # LLM enrichment cache (model + prompt version + job unique)
    # ------------------------------------------------------------------

    def get_llm_cache(self, job_id: int, model: str, prompt_version: str,
                      kind: str) -> Optional[dict]:
        row = self.conn.execute(
            """SELECT payload_json FROM llm_enrichment
               WHERE job_id=? AND model=? AND prompt_version=? AND kind=?""",
            (job_id, model, prompt_version, kind),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None

    def set_llm_cache(self, job_id: int, model: str, prompt_version: str,
                      kind: str, payload: dict) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO llm_enrichment
                   (job_id, model, prompt_version, kind, payload_json, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (job_id, model, prompt_version, kind,
                 json.dumps(payload, ensure_ascii=False), utcnow().isoformat()),
            )

    # ------------------------------------------------------------------
    # Trợ lý AI: dọn dữ liệu quá hạn (gọi ở mỗi request API + sweep mỗi giờ)
    # ------------------------------------------------------------------

    def purge_expired_chat(self, ttl_hours: int) -> int:
        """Xoá hội thoại (mọi owner) + hồ sơ khách không hoạt động quá ttl_hours, cache web
        hết hạn và bộ đếm quota cũ hơn 2 ngày. Trả số hội thoại đã xoá."""
        now = utcnow()
        cut = (now - timedelta(hours=ttl_hours)).isoformat()
        with self.conn:
            n = self.conn.execute(
                "DELETE FROM chat_conversations WHERE updated_at < ?", (cut,)).rowcount
            self.conn.execute(
                "DELETE FROM user_profiles WHERE owner LIKE 'guest:%' AND updated_at < ?", (cut,))
            self.conn.execute("DELETE FROM web_search_cache WHERE expires_at < ?", (now.isoformat(),))
            self.conn.execute("DELETE FROM assistant_usage WHERE day < ?",
                              ((now - timedelta(days=2)).date().isoformat(),))
        return n

    def jobs_needing_llm(self, model: str, prompt_version: str,
                         limit: int = 50) -> list[dict]:
        """Job active, HCM, chưa có cache relevance của model/prompt hiện tại."""
        rows = self.conn.execute(
            """SELECT j.* FROM jobs j
               WHERE j.is_active=1 AND j.is_hcm=1
                 AND NOT EXISTS (
                   SELECT 1 FROM llm_enrichment e
                   WHERE e.job_id=j.job_id AND e.model=? AND e.kind='relevance'
                     AND e.prompt_version=?)
               ORDER BY j.job_id DESC LIMIT ?""",
            (model, prompt_version, limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Crawl runs
    # ------------------------------------------------------------------

    def begin_crawl_run(self, queries: list[str]) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO crawl_runs (queries, started_at) VALUES (?,?)",
                (json.dumps(queries), utcnow().isoformat()),
            )
            return cur.lastrowid

    def finish_crawl_run(self, run_id: int, result) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE crawl_runs SET finished_at=?, result_json=? WHERE run_id=?",
                (utcnow().isoformat(), json.dumps(result.to_dict()), run_id),
            )

    def last_crawl_run(self) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM crawl_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["result"] = json.loads(d.get("result_json") or "{}")
        except json.JSONDecodeError:
            d["result"] = {}
        return d

    # ------------------------------------------------------------------
    # Users + saved jobs (invite-only MVP)
    # ------------------------------------------------------------------

    def verify_token(self, token: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM users WHERE token=?", (token,)
        ).fetchone()
        return dict(row) if row else None

    def save_job_for_user(self, user_id: int, job_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO saved_jobs (user_id, job_id, saved_at) VALUES (?,?,?)",
                (user_id, job_id, utcnow().isoformat()),
            )
            return cur.rowcount > 0

    def unsave_job_for_user(self, user_id: int, job_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM saved_jobs WHERE user_id=? AND job_id=?",
                (user_id, job_id),
            )
            return cur.rowcount > 0

    def saved_jobs_for_user(self, user_id: int) -> list[dict]:
        rows = self.conn.execute(
            """SELECT j.* FROM saved_jobs s JOIN jobs j ON j.job_id=s.job_id
               WHERE s.user_id=? ORDER BY s.saved_at DESC""",
            (user_id,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def stats(self) -> dict:
        one = lambda q: self.conn.execute(q).fetchone()[0]  # noqa: E731
        return {
            "total_jobs": one("SELECT COUNT(*) FROM jobs"),
            "active_jobs": one("SELECT COUNT(*) FROM jobs WHERE is_active=1"),
            "hcm_jobs": one("SELECT COUNT(*) FROM jobs WHERE is_hcm=1 AND is_active=1"),
            "companies": one("SELECT COUNT(*) FROM companies"),
            "crawl_runs": one("SELECT COUNT(*) FROM crawl_runs"),
            "last_crawl": (self.last_crawl_run() or {}).get("finished_at"),
        }
