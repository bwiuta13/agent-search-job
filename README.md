# Agent Search Job

Job search agent cho **Content & Marketing tại TP.HCM**: crawler pipeline đa nguồn, website invite-only và **remote MCP server** cho AI client (Claude, Codex, Cursor...).

> Toàn bộ nguồn dữ liệu và quyết định kỹ thuật được kiểm chứng thực tế ngày 15/10/2026 — xem [docs/research/](docs/research/) và [VERIFICATION.md](VERIFICATION.md).

## Tính năng

- **Pipeline crawl đa nguồn** — VietnamWorks (internal API), TopCV (Playwright bypass Cloudflare), LinkedIn (JobSpy), ITviec + CareerViet (HTML parse). Mỗi nguồn là một adapter riêng, lỗi nguồn không làm sập hệ thống.
- **Chuẩn hóa + chống trùng** — salary VND min/max, kinh nghiệm, quận/huyện TP.HCM, role cluster (7 nhóm nghề Content/Multimedia), dedupe fingerprint theo title+company.
- **Website invite-only tiếng Việt** — search + filter (ngành, quận, lương, hình thức, nguồn), trang chi tiết việc, trang công ty kèm **feedback có nguồn + confidence**, lưu việc. Toàn site `noindex`.
- **Remote MCP server** tại `/mcp` (JSON-RPC 2.0 Streamable HTTP) — 5 tool `vn_jobs_*` với input/output schema, structured content, cursor pagination, bearer token invite-only.
- **Company feedback có provenance** — ITviec review (aggregate, không login). Không có nguồn tin cậy → trả `status: "unavailable"`, không suy đoán.

## Kiến trúc

```text
src/agent_search_job/
  domain/       models + taxonomy + normalize (salary/location/role cluster/dedupe)
  sources/      adapter từng nguồn: vietnamworks, topcv, jobspy (linkedin),
                itviec, careerviet, company_feedback (ITviec review)
  pipeline/     ingest: discover song song -> normalize -> dedupe -> upsert
  storage/      SQLite: jobs, companies, company_feedback_sources,
                crawl_runs, saved_jobs, users (invite-only)
  web/          FastAPI: pages + REST API + /mcp endpoint
  mcp/          MCP server (JSON-RPC) + contracts (validation, giới hạn)
scripts/        crawl.py, enrich_companies.py, serve.py, show_token.py
tests/          45 test (normalize, storage, MCP protocol, web API) — chạy offline
```

## Quick start

```bash
pip install -e ".[dev]"          # hoặc: pip install fastapi uvicorn jinja2 playwright jobspy httpx python-multipart pytest pytest-asyncio
python -m playwright install chromium   # cho TopCV adapter

# 1) Crawl dữ liệu thật
python scripts/crawl.py --db data/jobs.db --max-results 15
#    Thêm TopCV (ưu tiên cao nhất, chậm ~15s/trang):
python scripts/crawl.py --db data/jobs.db --with-topcv --no-linkedin

# 2) Enrich công ty + feedback
python scripts/enrich_companies.py data/jobs.db

# 3) Chạy server (web + MCP)
python scripts/serve.py --db data/jobs.db --port 8765

# 4) Lấy MCP token (email allowlist)
python scripts/show_token.py data/jobs.db
```

Mời người dùng: set env `ALLOWLIST_EMAILS="a@gmail.com,b@gmail.com"` trước lần chạy đầu (mặc định `friend@example.com`).

## Website

- `http://localhost:8765/` → đăng nhập bằng email đã mời → `/search`
- Search: từ khóa + filter nhóm nghề / quận / lương tối thiểu / hình thức / nguồn
- Trang việc: mô tả, yêu cầu, quyền lợi, platform/tool, provenance, nút mở trang nguồn
- Trang công ty: thông tin + **bảng feedback theo nguồn** (rating, số review, confidence, link) + việc đang tuyển

## MCP server

Endpoint: `POST http://localhost:8765/mcp` · Auth: `Authorization: Bearer <token>`

| Tool | Mô tả |
|---|---|
| `vn_jobs_search_jobs` | Tìm việc Content/Multimedia tại TP.HCM. Filter: query, role_clusters, districts, salary_min_vnd, experience_max_years, work_modes, posted_within_days, portfolio_required, source, page_size (≤25), cursor |
| `vn_jobs_get_job` | Bản ghi đầy đủ theo job_id (mô tả, yêu cầu, benefits, provenance) |
| `vn_jobs_get_company` | Hồ sơ công ty + các việc đang tuyển |
| `vn_jobs_get_company_feedback` | Feedback theo nguồn kèm rating/review count/confidence; không có → `unavailable` |
| `vn_jobs_get_market_facets` | Các lựa chọn lọc hiện có để xây truy vấn hợp lệ |

Ví dụ gọi thử:

```bash
TOKEN=$(python scripts/show_token.py data/jobs.db | head -1 | awk '{print $2}')

curl -s -X POST http://localhost:8765/mcp \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

curl -s -X POST http://localhost:8765/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"vn_jobs_search_jobs","arguments":{"query":"content tiktok","page_size":5}}}'
```

Kết nối client MCP (streamable HTTP):

```json
{ "mcpServers": { "vn-jobs": {
    "url": "http://localhost:8765/mcp",
    "headers": { "Authorization": "Bearer <token>" }
} } }
```

## REST API (cùng service với MCP)

```text
GET  /api/jobs                       (query, role_clusters, districts, salary_min_vnd, ...)
GET  /api/jobs/:jobId
GET  /api/companies/:companyId
GET  /api/companies/:companyId/feedback
GET  /api/facets
POST /api/me/saved-jobs              {"job_id": 1}
DELETE /api/me/saved-jobs/:jobId
GET  /api/me/saved-jobs
```

## LLM enrichment (tuỳ chọn, chạy sau ingest)

LLM chỉ dùng cho field khó + relevance scoring — **không** thay rule-based (docs/research-website.md mục 6). Kết quả cache theo `(job_id, model, prompt_version, kind)` nên chạy lại không tốn token; lỗi/không cấu hình LLM thì pipeline vẫn đầy đủ.

```bash
export AI_PROVIDER=openai
export AI_TEXT_MODEL=z-ai/glm-5.3
export OPENAI_API_KEY=<key>
export OPENAI_BASE_URL=https://tokenplan.api.greennode.ai/v1
export OPENAI_REQUEST_TIMEOUT_SECONDS=90

python scripts/llm_enrich.py --db data/jobs.db --limit 50
```

Sau khi enrich: search sort theo `llm_relevance` (job đúng nhóm Content/Multimedia TP.HCM đứng đầu), badge "phù hợp X%" kèm lý do, field khó (`content_types`, `on_camera_required`, `agency_or_inhouse`, KPI...) lưu trong cache bảng `llm_enrichment`.

## Test

```bash
python -m pytest tests/ -q    # 45 test, chạy offline với fixtures, không cần mạng
```

## Nguồn dữ liệu (đã verify 15/10/2026)

| Nguồn | Cách lấy | Ghi chú |
|---|---|---|
| VietnamWorks | `POST ms.vietnamworks.com/job-search/v1.0/search` | Nhanh (~0.2s), data giàu nhất: salary, benefits, company profile. HCM = cityId 29 |
| TopCV | Playwright headless Chromium | Bị Cloudflare chặn HTTP thường + Jina Reader; browser thật qua được. URL `tim-viec-lam-{slug}` |
| LinkedIn | JobSpy (`site_name="linkedin"`) | Ổn định, không cần key |
| ITviec | HTML parse (`curl` đọc được) | Company review aggregate public — nguồn feedback chính |
| CareerViet | HTML parse (gzip) | Bổ sung |
| Indeed VN | JobSpy (`country_indeed="vietnam"`) | Cần test lại khi deploy — từng bị chặn ở research 15/10 |

## Rủi ro & lưu ý vận hành

- API nội bộ (VietnamWorks) có thể đổi bất cứ lúc nào → mỗi adapter có health check riêng, nguồn lỗi không làm sập web/MCP (dữ liệu cũ vẫn phục vụ kèm `last_verified_at`).
- Không dùng dữ liệu cho mục đích thương mại/redistribute (ToS các nguồn).
- Token MVP là hash đơn giản — deploy thật cần nâng lên Clerk/OAuth (thiết kế đã tách hẳn để thay).
- MCP là lớp **đọc**, không khởi động crawl trong tool call (crawl chạy background qua `scripts/crawl.py`/cron).

## Đã verify end-to-end (xem [VERIFICATION.md](VERIFICATION.md))

- 45/45 unit + contract test pass
- Crawl thật: 209 jobs (VietnamWorks 81, LinkedIn 72, TopCV 56), 178 job HCM, 184 companies
- Web: login → search 63 kết quả "content" → trang chi tiết → trang công ty
- MCP qua HTTP: initialize, tools/list (5 tool), search (63 kết quả, cursor), validation error, 401/403 auth + origin
- REST API: toàn bộ endpoint theo contract trong docs
