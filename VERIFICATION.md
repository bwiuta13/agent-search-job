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

## 6. LLM enrichment (model z-ai/glm-5.3 qua Greennode)

```text
$ python scripts/llm_enrich.py --db data/jobs.db --limit 10
Jobs cần enrich: 10 (model=z-ai/glm-5.3, prompt=v1)
  ✓ [209] Marketing & Communications Team Leader | rel=0.7 | Vị trí Marcom tại TP.HCM...
  ✓ [192] Social Media Content Executive         | rel=1.0 | Đúng nhóm content/social...
  ✓ [195] Social Media Executive                 | rel=0.9 | ...
  (Email Marketing / Marketing tổng hợp -> 0.5: liên quan nhưng lệch nhóm chính)
Enriched: 10 | LLM fail: 0
```

- Relevance phân biệt đúng: social/content chuyên = 0.9–1.0; marketing tổng hợp = 0.5
- Cache hoạt động: chạy lại 2 lần -> `jobs_needing_llm` trả danh sách trống cho các job đã enrich
- Search join: `/api/jobs?query=social` giờ sort theo `llm_relevance` DESC (verify: 4 job rel=1.0 đứng đầu kèm `llm_reason`)
- Không cấu hình key -> script in hướng dẫn và thoát êm, không lỗi
- 5 test LLM mới (mock httpx): parse JSON, sanitize enum, thiếu key -> None, lỗi mạng -> None, cache roundtrip + search join

Lưu ý model GLM-5.3 là reasoning model: cần `max_tokens` lớn (1.500–2.500) vì token reasoning trừ vào cùng pool — bug đã sửa trong phiên bản này.

## 7. Điểm chưa hoàn thành / hạn chế

- Deploy production (Render/Docker, PostgreSQL, Clerk OAuth) — nằm ngoài scope bản này, thiết kế đã tách để nâng cấp.
- Indeed adapter: research 15/10 cho thấy cần `country_indeed="vietnam"`; chưa crawl trong run cuối vì đã từng bị chặn — bật thêm khi cần.
- `posted_within_days` lọc theo `last_verified_at`; crawl định kỳ mỗi ngày sẽ giữ mốc này chính xác.
- Session in-memory: restart server mất session cookie (đăng nhập lại) — chấp nhận cho MVP.


## 8. LLM thông minh: NL query + phân tích JD (16/10/2026)

### vn_jobs_smart_search (LLM parse câu tự nhiên)
```text
"tìm việc content tiktok lương trên 12 triệu"
  -> llm_used: true, parsed: query="content tiktok", salary_min_vnd=12000000
  -> total: 69 (Social Content Creator Jamu Spa 9-12tr, Social Media Content Executive 13-15tr...)
"việc social media mới đăng tuần này ở Bình Thạnh"
  -> parsed: role_clusters=["social_media"], districts=["Bình Thạnh"], posted_within_days=7
  -> total: 1 (Social Media Leader | Bình Thạnh)
```
- Sanitize: role/mode ngoài enum bị loại ("ceo", "teleport"), posted_days clamp 60, districts chỉ giữ quận HCM
- LLM không cấu hình/lỗi -> fallback rule parser (lương "trên X triệu", keyword role, remote)

### Bug search sửa kèm: multi-word query
"content tiktok" cũ = 0 kết quả (LIKE cả cụm liền) -> giờ tách từ, OR từng từ: **73 kết quả**

### Phân tích JD trên trang chi tiết (job 36 - Marketing Assistant Manager, VietnamWorks)
- Lần 1 (LLM thật): 19.7s -> đầy đủ: tóm tắt, 3 trách nhiệm, 5 yêu cầu, nice-to-have,
  lương $1,000-1,700, red flags ("JD bị cắt cụt", "lương rộng chưa rõ cách xếp")
- Lần 2 (cache): 0.029s
- 60/60 tests pass (thêm 8 test: NL parse sanitize, rule fallback, JD clip, smart search tool, empty text error)


## 9. Hard-code JD từng nguồn + TopDev + cấp bậc/công ty + rescan + scope (16/10/2026)

### JD đầy đủ hard-code theo nguồn (đã verify cấu trúc thật từng trang)
- `sources/detail_fetchers.py`: ITviec (server render, `Job description`/`Your skills and experience`),
  CareerViet (`Mô tả công việc`), TopCV (Playwright), TopDev (inline JSON)
- Kết quả sau rescan: JD đầy đủ (>200 chars): **vietnamworks 77/77, topdev 30/30, topcv 21/45**
  (LinkedIn bị 429 rate-limit khi fetch JD hàng loạt - giới hạn thật của LinkedIn guest access)

### Nguồn mới: TopDev (api.topdev.vn công khai)
- `fields[job]=csv` (KHÔNG dùng `fields[job][0]=` -> 500); salary là dict, min/max bị che '*'
  -> dùng `min_filter`/`max_filter`; HCM = `region_ids=79`
- Verify: health 200/560ms, discover 10 jobs thật (Marketing Intern ATALINK, Performance Marketing...),
  rescan topdev: 30 jobs, 30/30 có JD đầy đủ

### Cấp bậc (job_level) + tìm theo công ty
- `classify_job_level`: title keyword (Thực tập/Senior/Leader/Manager...) -> fallback số năm KN (mid of range)
- Backfill 119/209 job cũ; phân bố: senior 35, manager 35, middle 17, lead 11, junior 11, intern 6, fresher 4
- Web `?job_level=senior` = 28 việc; `?company=Publicis` = 5 việc; trang `/companies` 50 cards
- MCP: `job_levels` + `company_query` (enum chặn 'boss'...) + smart_search hiểu "content senior lương trên 15 triệu"
  -> parsed: query='content', levels=['senior'], salary=15000000, total 6

### Rescan (user chủ động quét lại nguồn tùy thích)
- `POST /rescan` (form: source, query, max_results) -> chạy nền qua asyncio task, không chặn request
- `GET /api/rescan/status` -> {running, source, started_at, message}
- Verify thật: rescan topdev (30 jobs JD đủ), rescan all (vietnamworks 77, linkedin 62, topcv 45, topdev 30)
- MCP vẫn read-only (đúng docs) - rescan chỉ qua web/REST

### Scope: chỉ Truyền thông đa phương tiện
- `in_multimedia_scope`: title có dấu hiệu truyền thông -> giữ; exclude telesales/sales/kinh doanh/
  performance/event/kế toán/HR; hint bắt buộc (content/social/video/design/creative/...)
- Deactivate 59 job ngoài scope + restore 16 job có dấu hiệu truyền thông trong title
- Sau rescan all: **214 job active, 0 job ngoài scope**; clusters: content_marketing 106,
  social_media 37, communications_pr 24, creative_production 20, copywriter_seo 13,
  video_production 11, multimedia_design 3
- DEFAULT_QUERIES đổi theo scope (bỏ 'digital marketing', thêm 'video editor', 'graphic design', 'creative')

### Tests: 66/66 pass (+6: scope filter, default queries, pipeline out_of_scope,
level classify, search level+company, contracts validation)


## 10. Nguồn VNG Careers (công ty lớn) (16/10/2026)

### career.vng.com.vn - Next.js server render
- `__NEXT_DATA__` -> `props.pageProps.jobs`: 10 jobs/trang, total 78, JD đầy đủ (1660-3929 chars)
  ngay trong list - không cần fetch detail riêng
- Job HCM: `location == "Thành phố Hồ Chí Minh"`; detail URL `/vi/job/{slug}`
- Verify thật: health 200/505ms, discover 30 jobs (27 HCM), rescan qua web `POST /rescan source=vng`
- Scope lọc đúng: chỉ giữ job truyền thông (Creative Executive VNGGames); loại đúng
  Business Development Representative + Project Management Intern (không phải truyền thông)

### FPT: không thêm được (điều tra thật)
- fpt.com.vn/careers + /tuyen-dung -> 404; jobs.fpt.com.vn + career.fpt.com.vn -> không DNS
- fpt-software.com/careers -> 403 Cloudflare (chặn curl lẫn httpx)
- Ghi nhận trong docs là hạn chế; FPT Software jobs đã có qua nguồn VietnamWorks/ITviec

### Fix scope boundary (false positive)
- 'pr' trong 'Representative', 'design' trong 'designed' không còn tính là dấu hiệu truyền thông
  (word boundary `(?<![a-zà-ỹ])hint(?![a-zà-ỹ])` cho cả title hints + text hints)
- Deactivate thêm 15 job match nhầm; active còn 202, 0 ngoài scope

### Trạng thái cuối: 202 job active scope Truyền thông đa phương tiện
- vietnamworks 72, linkedin 55, topcv 44, topdev 30, vng 1 (VNG tuyển ít vị trí truyền thông)
- 68/68 tests pass (+2: VNG adapter parse __NEXT_DATA__, boundary false positive)


## 11. Fix HCM hard requirement + VNG pagination + scope leak (16/10/2026, theo feedback user)

### Bug 1: VNG chỉ lấy được 10/78 job (mất Content Marketing Collaborator)
- Nguyên nhân: URL `/co-hoi-nghe-nghiep?page=N` BỎ QUA page param (mọi trang trả
  y hệt 10 job đầu); phải dùng `/vi/tim-kiem-viec-lam?page=N` (verify: page 2 job
  đầu khác page 1, 78 unique jobs qua 8 trang)
- Fix: đổi LIST URL + dedupe job_id trong discover + dừng khi trang lặp
- Kết quả: rescan VNG -> 13 job active scope truyền thông, **có Content Marketing
  Collaborator** + Social Marketing/Chinese Localization/Marketing Operations
  Collaborator, Digital Marketing Intern, Brand Specialist, Growth Marketing Exec...

### Bug 2: 26 job ngoài HCM vẫn active (Hà Nội, Gò Công...)
- Fix: `IngestPipeline._is_hcm()` hard-code theo nguồn:
  - vietnamworks/itviec/topdev: tin query HCM, chỉ loại khi text rõ ràng nơi khác
    (Hà Nội/Bangkok/Jakarta...) mà không có dấu hiệu HCM
  - linkedin/topcv/vng/careerviet: PHẢI có dấu hiệu HCM (hồ chí minh/ho chi minh/
    quận 1-7/bình thạnh/thủ đức...) hoặc flag is_hcm từ normalize
- Pipeline stat mới `out_of_hcm`; deactivate 26 job trong DB

### Bug 3: crawl mới bật lại job đã bị lọc (AI/QC/Backend Engineer của VNG active)
- Nguyên nhân 1: `update_job` luôn set is_active=1 khi job đã tồn tại
  -> Fix: `keep_inactive=True` từ ingest (job bị lọc scope/HCM không sống lại)
- Nguyên nhân 2: scope false positive - 'design' trong "to design and operate"
  (động từ) và 'dựng' trong 'xây dựng' tính là hint nghề
  -> Fix: bỏ 'design'/'editor'/'writer' khỏi TEXT hints (giữ title hints);
  'dựng' chỉ tính khi cạnh video/film/phim
- Deactivate thêm 8 job engineer; **183 active, 0 ngoài HCM, 0 ngoài scope**

### Trạng thái cuối: 183 job active (tất cả HCM, tất cả truyền thông đa phương tiện)
- vietnamworks 68, linkedin 53, topdev 28, topcv 21, vng 13
- 76/76 tests pass (+4: _is_hcm theo nguồn, pipeline out_of_hcm, keep_inactive,
  scope design/dựng false positive)
