# VERIFICATION — kết quả kiểm chứng end-to-end

Ngày verify: 15/10/2026 · Máy: macOS, Python 3.13

## 1. Unit + contract tests

```text
$ python -m pytest tests/ -q
45 passed in ~1s
```

Bao phủ:
- Normalize: salary ("15 - 25 triệu" → 15_000_000–25_000_000; "Từ 8 triệu"; "Thoả thuận" → None), experience, HCM district, role cluster classification, normalize toàn pipeline
- Storage: insert/search, dedupe idempotent (ingest 2 lần không sinh duplicate), expired → inactive, filters, saved jobs, company feedback
- MCP contracts: page_size ≤ 25, posted_within_days ≤ 60, enum validation, query ≤ 200 ký tự
- MCP protocol in-process: initialize, tools/list, 5 tool, structuredContent + TextContent, validation error, unknown method
- Web API: login flow, 401 khi chưa login, noindex header, MCP auth (401/403), origin check

## 2. Crawl dữ liệu thật

```text
$ python scripts/crawl.py --db data/jobs.db --max-results 15
Run 1 (VNW + LinkedIn + ITviec + CareerViet):
  discovered=139, active=139, hcm=137, errors=[]

$ python scripts/crawl.py --db data/jobs.db --max-results 20 --with-topcv --no-linkcached
Run 2 (+TopCV Playwright):
  total=209, active=209, hcm=178, topcv health OK

$ python scripts/enrich_companies.py data/jobs.db
  companies=184, feedback sources=1 (FE CREDIT: rating 3.4, 23 reviews — ITviec)
```

Phân bố nguồn: vietnamworks 81 · linkedin 72 · topcv 56
Role clusters: content_marketing 119 · social_media 36 · communications_pr 22 · creative_production 14 · video_production 9

Parser feedback được sửa sau khi phát hiện bug: parse nhầm số "3.1" từ trang 404 → giờ **bắt buộc** khối `Overall rating <x> <n> reviews` (verify với FPT Software: 3.7/2206 reviews). Công ty không có review → `unavailable`, không đoán.

## 3. Website end-to-end (server thật, port 8765)

```text
GET  /health                       → 200 {"status":"ok","stats":{...209 jobs...}}
POST /login (email allowlist)      → 302 → /search (cookie session set)
GET  /search?q=content             → "Tìm thấy 63 việc", 27 job cards render
GET  /search (không login)         → 401
     header                        → x-robots-tag: noindex, nofollow, noarchive
GET  /jobs/1                       → trang chi tiết + provenance + CTA nguồn
GET  /companies/1                  → hồ sơ + feedback + việc đang tuyển
POST /api/me/saved-jobs            → {"saved":true} → list → DELETE OK
```

## 4. MCP endpoint qua HTTP (server thật)

```text
POST /mcp initialize               → protocolVersion 2025-06-18, serverInfo vn-jobs-mcp
POST /mcp tools/list               → 5 tool vn_jobs_* (đủ inputSchema + outputSchema)
POST /mcp vn_jobs_search_jobs
     {"query":"content","page_size":3}
     → total 63, items 3, next_cursor "192", structuredContent + text JSON
     item: "Social Content Creator Jamu Spa" | 9-12 triệu VND (min/max parsed đúng)
POST /mcp search page_size=99      → isError: true "page_size phải từ 1 đến 25"
POST /mcp (không token)            → 401
POST /mcp (token sai)              → 403
POST /mcp Origin: evil.com         → 403
vn_jobs_get_job 1                  → full record + description + provenance
vn_jobs_get_company 1              → company + 1 open job
vn_jobs_get_company_feedback 1     → status "unavailable" + note (không có nguồn tin cậy)
vn_jobs_get_market_facets          → 7 role clusters, 12 districts, sources
```

## 5. REST API contract

Toàn bộ endpoint trong docs/implementation-plan.md mục 5 trả 200 với dữ liệu thật; 401 khi thiếu auth.

## 6. Điểm chưa hoàn thành / hạn chế

- Deploy production (Render/Docker, PostgreSQL, Clerk OAuth) — nằm ngoài scope bản này, thiết kế đã tách để nâng cấp.
- Indeed adapter: research 15/10 cho thấy cần `country_indeed="vietnam"`; chưa crawl trong run cuối vì đã từng bị chặn — bật thêm khi cần.
- `posted_within_days` lọc theo `last_verified_at`; crawl định kỳ mỗi ngày sẽ giữ mốc này chính xác.
- Session in-memory: restart server mất session cookie (đăng nhập lại) — chấp nhận cho MVP.
