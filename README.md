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
tests/          normalize, storage, MCP protocol, web API, trợ lý AI — chạy offline
```

## Quick start

```bash
pip install -e ".[dev]"          # hoặc: pip install fastapi uvicorn jinja2 playwright jobspy httpx python-multipart python-dotenv pypdf pytest pytest-asyncio
python -m playwright install chromium   # cho TopCV adapter

# 1) Crawl dữ liệu thật
python scripts/crawl.py --db data/jobs.db --max-results 15
#    Thêm TopCV (ưu tiên cao nhất, chậm ~15s/trang):
python scripts/crawl.py --db data/jobs.db --with-topcv --no-linkedin

# 2) Enrich công ty + feedback
python scripts/enrich_companies.py data/jobs.db

# 3) Chạy server (web + MCP) — Ctrl+C để dừng
make start                       # = python scripts/serve.py --db data/jobs.db --port 8765
make start PORT=8770             # đổi cổng nếu 8765 đang bận

# 4) Lấy mã truy cập từng email (đăng nhập web + MCP bearer token)
python scripts/show_token.py data/jobs.db
```

Mời người dùng: set env `ALLOWLIST_EMAILS="a@gmail.com,b@gmail.com"` trước lần chạy đầu (mặc định `friend@example.com`).
Mở public (không cần đăng nhập, cả web/API/MCP): để `ALLOWLIST_EMAILS=` rỗng trong `.env` (server tự nạp `.env`; biến set trong shell được ưu tiên) hoặc chạy `ALLOWLIST_EMAILS= make start`. Khách xem/tìm được mọi thứ nhưng không lưu việc được; không set biến thì vẫn invite-only.

## Chạy bằng Docker Compose

Cấu hình này dành cho VM **Google Compute Engine** chạy sau Nginx. Container chỉ mở app tại
`127.0.0.1:8765`, vì vậy không cần mở port `8765` trên GCP Firewall. Nginx tiếp tục nhận traffic
public ở port `80/443` và proxy vào app như cấu hình hiện tại.

Trong lần chuyển đầu tiên từ service systemd cũ, dừng service để nhường port cho Docker:

```bash
sudo systemctl disable --now agent-search-job
```

Sau đó, trên server từ thư mục dự án:

```bash
# Tuỳ chọn: chỉnh email được mời và cấu hình LLM.
cp -n .env.example .env

# Build image và chạy app nền; các lần sau vẫn dùng cùng lệnh này.
docker compose up -d

# Kiểm tra trạng thái.
docker compose ps
curl http://127.0.0.1:8765/health
```

Nếu không tạo `.env`, Compose dùng các giá trị mặc định an toàn trong `compose.yaml`.
Thư mục `data/` và `backups/` được mount từ host nên database và backup không mất khi build lại container.
Nếu VM đã có database do service systemd tạo, đặt `DATA_DIR`, `BACKUP_DIR`, `APP_UID` và `APP_GID`
trong `.env` như ví dụ để container dùng trực tiếp dữ liệu cũ mà không phải sao chép.

Các lệnh vận hành thường dùng:

```bash
docker compose logs -f app
docker compose up -d --build       # build lại sau khi cập nhật source
docker compose run --rm app python scripts/crawl.py --db data/jobs.db --max-results 15
docker compose down
```

### Nginx cho Trợ lý AI

Trang `/assistant` cần các dòng sau trong `location /` của Nginx; thiếu thì app vẫn chạy nhưng chat, hồ sơ và upload CV lỗi:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 6m;
    proxy_read_timeout 300s;
    proxy_buffering off;
}
```

- `proxy_set_header Host $host`: app so header `Origin` với `Host` để chặn POST từ trang khác; mặc định Nginx gửi `Host: 127.0.0.1:8765` nên mọi POST chat/hồ sơ/CV bị 403.
- `proxy_set_header X-Forwarded-Proto $scheme`: báo scheme gốc để app đặt cookie `Secure` khi chạy https (uvicorn chỉ tin header này từ các IP trong `FORWARDED_ALLOW_IPS`, mặc định `127.0.0.1,::1`).
- `client_max_body_size 6m`: mặc định 1MB, CV 1–5MB sẽ bị 413.
- `proxy_read_timeout 300s`: mặc định 60 giây; Deep Think có thể suy luận lâu mà chưa có chữ nào. Khi model đang suy luận app gửi `event: thinking` mỗi ~3 giây, còn im lặng hẳn 15 giây thì gửi dòng ping `: ping`; 300 giây là mức dự phòng.
- `proxy_buffering off`: để câu trả lời stream về từng đoạn thay vì đến một lần.

## Đăng nhập & bảo mật

- Mỗi email được mời có một **mã truy cập ngẫu nhiên** (`secrets.token_urlsafe(24)`, lưu ở `users.token`), tạo khi email được seed. Lấy bằng `python scripts/show_token.py data/jobs.db` rồi gửi riêng cho từng người.
- Web: đăng nhập bằng **email + mã truy cập**. Sai email hay sai mã đều trả cùng một lỗi (không dò được email nào đã được mời). Session là token ngẫu nhiên lưu trong bảng `sessions` của SQLite, hết hạn sau 7 ngày, còn nguyên khi restart server; cookie `HttpOnly`, `SameSite=Lax`, `Secure` khi chạy sau HTTPS (Caddy).
- MCP / REST: `Authorization: Bearer <mã truy cập>` (cùng mã với web).
- Nâng cấp từ bản cũ: token cũ là `sha256("invite:"+email)` nên ai biết email cũng tính ra được. Lần đầu mở DB bằng code mới (server, crawl, `show_token.py`...), các token kiểu cũ tự đổi sang token ngẫu nhiên, token cũ hết hiệu lực → chạy lại `show_token.py` và gửi mã mới cho mọi người, cập nhật header MCP client.
- Thu hồi / đổi mã một người (đăng xuất mọi session của họ):

```bash
sqlite3 data/jobs.db "UPDATE users SET token='$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')' WHERE email='a@gmail.com'; DELETE FROM sessions WHERE user_id=(SELECT user_id FROM users WHERE email='a@gmail.com');"
```

## Dọn dữ liệu

- Cuối mỗi lần quét: job quá hạn nộp và job `JOB_STALE_DAYS` (30) ngày không thấy lại trên nguồn vừa quét bị ẩn (`is_active=0`, vẫn còn trong DB).
- `make purge-dry-run`: xem trước số dòng sẽ xoá. `make purge`: backup vào `data/backups/` rồi xoá job đã ẩn quá `JOB_PURGE_AFTER_DAYS` (90) ngày (trừ job có người lưu) kèm cache AI, xoá `crawl_runs` cũ hơn `CRAWL_RUNS_KEEP_DAYS` (90) ngày, rồi `VACUUM`. Công ty + feedback giữ nguyên.

## Website

- `http://localhost:8765/` → đăng nhập bằng email đã mời + mã truy cập → `/search`
- Search: từ khóa + filter nhóm nghề / quận / lương tối thiểu / hình thức / nguồn
- Trang việc: mô tả, yêu cầu, quyền lợi, platform/tool, provenance, nút mở trang nguồn
- Trang công ty: thông tin + **bảng feedback theo nguồn** (rating, số review, confidence, link) + việc đang tuyển

## MCP server

Endpoint: `POST http://localhost:8765/mcp` · Auth: `Authorization: Bearer <mã truy cập>` (xem [Đăng nhập & bảo mật](#đăng-nhập--bảo-mật))

| Tool | Mô tả |
|---|---|
| `vn_jobs_search_jobs` | Tìm việc Content/Multimedia tại TP.HCM. Filter: query, role_clusters, districts, salary_min_vnd, experience_max_years, work_modes, posted_within_days, portfolio_required, source, page_size (≤25), cursor |
| `vn_jobs_get_job` | Bản ghi đầy đủ theo job_id (mô tả, yêu cầu, benefits, provenance) |
| `vn_jobs_get_company` | Hồ sơ công ty + các việc đang tuyển |
| `vn_jobs_get_company_feedback` | Feedback theo nguồn kèm rating/review count/confidence; không có → `unavailable` |
| `vn_jobs_smart_search` | Tìm bằng **câu tiếng Việt tự nhiên** (LLM parse): "tìm việc content tiktok lương trên 15 triệu ở quận 1", "việc social media mới đăng tuần này có thể remote". Trả kèm `parsed_filters` + `parser.llm_used` |
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

Sau khi enrich: search sort theo `llm_relevance` (job đúng nhóm Content/Social/Brand Marketing TP.HCM đứng đầu), badge "phù hợp X%" kèm lý do, field khó (`content_types`, `on_camera_required`, `agency_or_inhouse`, KPI...) lưu trong cache bảng `llm_enrichment`.

## Phạm vi: role list người dùng cần tìm

Pipeline **chỉ lấy và lưu job thuộc role list**: social media, content creator, content marketing, communication, content executive, marketing executive, brand marketing, social content, content writer, copywriter, creative. Job ngoài phạm vi bị lọc bỏ ngay trong ingest (`in_role_scope`): telesales/kinh doanh/sales, performance/field/event marketing, kế toán, HR... và cả **video editor / graphic design / multimedia thuần** (role list không có) — job dựng video chỉ giữ khi title là content creator/social/creative thật.

## Tìm theo cấp bậc + công ty + quét lại nguồn

- **Cấp bậc** (`job_level`): intern/fresher/junior/middle/senior/lead/manager — tự phân loại từ tiêu đề + số năm kinh nghiệm; filter trên web (`?job_level=senior`), REST (`?job_levels=senior,junior`) và MCP (`job_levels`).
- **Công ty đang tuyển**: trang `/companies` (top công ty + số việc mở), filter theo tên công ty trên search (`?company=FPT`), API `/api/hiring-companies?q=...` (kèm danh sách job của từng công ty), MCP `company_query`.
- **Quét lại nguồn (rescan)**: nút "Quét lại dữ liệu nguồn" trên trang tìm kiếm → `POST /rescan` chọn nguồn (VietnamWorks/ITviec/CareerViet/LinkedIn/TopDev/all) + từ khóa → chạy nền, xem trạng thái `GET /api/rescan/status`. Không chặn request.

## JD đầy đủ hard-code theo từng nguồn

| Nguồn | Cách lấy JD |
|---|---|
| VietnamWorks | API trả JD đầy đủ ngay trong search |
| TopCV | Playwright trang chi tiết (Cloudflare) |
| ITviec | Server render: `Job description` / `Your skills and experience` |
| CareerViet | Server render: `Mô tả công việc` / `Yêu cầu ứng viên` |
| TopDev | API + trang detail inline JSON `"description"` |
| LinkedIn | JobSpy `linkedin_fetch_description=True` (giới hạn 429 của LinkedIn) |

## Nguồn dữ liệu (7 nguồn)

VietnamWorks (API nội bộ), TopCV (Playwright), LinkedIn (JobSpy), ITviec, CareerViet, TopDev (API công khai), **VNG Careers** (career.vng.com.vn - trang careers công ty lớn, JD đầy đủ sẵn trong `__NEXT_DATA__`) — tất cả TP.HCM.

Nguồn công ty lớn khác đã điều tra: FPT (fpt.com.vn/careers 404, fpt-software.com 403 Cloudflare) — không có endpoint công khai; việc FPT Software vẫn bắt được qua VietnamWorks/ITviec.

## Phân tích JD bằng AI (tự động trên trang chi tiết)

Mở trang chi tiết việc → hệ thống gọi LLM phân tích JD gốc thành khối **"Phân tích JD bằng AI"**:
- Tóm tắt vai trò (ai làm gì, phù hợp ai)
- Trách nhiệm chính / Yêu cầu bắt buộc / Ưu tiên nếu có
- Quyền lợi nổi bật + **Điểm cần lưu ý (red flags)** — vd "JD bị cắt cụt", "lương thoả thuận chưa rõ"
- Kết quả **cache** trong bảng `llm_enrichment` (lần sau mở trang = tức thì); LLM lỗi → trang vẫn hiện JD gốc bình thường
- REST: `GET /api/jobs/{id}` trả thêm `jd_analysis`

Lưu ý: LLM chỉ chạy cho job có JD ≥ 80 ký tự (hiện chỉ VietnamWorks có JD đầy đủ; TopCV/LinkedIn listing không kèm JD — cần fetch trang chi tiết nguồn, nằm trong backlog).

## Trợ lý AI (`/assistant`)

Trang chat (link "Trợ lý AI" trên thanh menu): người dùng là "công chúa", trợ lý xưng "em". Agent GLM
(`AI_TEXT_MODEL`, qua GreenNode) **tự gọi tool** rồi trả lời dạng stream (SSE):

| Tool | Dữ liệu |
|---|---|
| `search_jobs`, `get_jobs`, `get_company`, `get_company_feedback` | DB job (dùng lại tool MCP `vn_jobs_*`) |
| `market_stats` | số việc + lương thấp/trung vị/cao theo nhóm nghề, cấp bậc, quận, công ty |
| `company_web_search` | một lượt GLM riêng bật tool `web_search` tích hợp của GreenNode — dùng chính cấu hình LLM trong `.env` (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `AI_TEXT_MODEL`), không cần key tra web riêng; tóm tắt + URL nguồn, cache 7 ngày (tin tức 24 giờ) |

- **Deep Think**: cho agent tối đa 10 vòng gọi tool (mặc định 6) và câu trả lời dài hơn.
- **Hồ sơ / CV**: ô "Giới thiệu bản thân" (mọi người, kể cả khách) + upload CV PDF/DOCX ≤ 5MB — **chỉ người đã đăng nhập** (khách nhận 403 "đăng nhập để tải CV"); chỉ lưu phần chữ, không lưu file gốc. PDF đọc bằng `pypdf` (tối đa 10 trang đầu, dừng khi đủ 20.000 ký tự, stream giải nén > 4MB bị từ chối để giảm rủi ro PDF bomb — PDF dựng sẵn vẫn có thể tốn vài giây CPU, nên không mở upload cho khách); DOCX đọc trực tiếp `word/document.xml` trong file zip có giới hạn kích thước giải nén (không dùng python-docx).
- **Dữ liệu chat tự xoá** 24 giờ sau tin nhắn cuối (`ASSISTANT_CHAT_TTL_HOURS`): dọn khi khởi động, mỗi giờ khi server chạy và ở mỗi request API; bản backup của `make purge` không chứa chat và hồ sơ khách.
- **Giới hạn**: `ASSISTANT_DAILY_MESSAGES` (100), `ASSISTANT_DAILY_WEB_SEARCHES` (20) mỗi người mỗi ngày; ở public mode còn có trần chung cho mọi khách (khách tự đổi cookie được): `ASSISTANT_GUEST_DAILY_MESSAGES` (300), `ASSISTANT_GUEST_DAILY_WEB_SEARCHES` (30) mỗi ngày.
- **3D**: Three.js r186 vendor trong `src/agent_search_job/web/static/vendor` (xem `VERSIONS.md`); `src/agent_search_job/web/static/assistant/stage.js` vẽ quả cầu (lõi màu bọc vỏ kính) và **nhân vật 3D thỏ mascot** (`static/assistant/character.glb`, tối ưu từ 10.7MB về 2.7MB: texture 2048→1024 JPEG, attribute lượng tử hoá, meshopt simplify còn 30% tam giác) ở góc dưới trái từ ≥1320px. Nhân vật phản ứng theo hội thoại (procedural — GLB không kèm clip): *đang nghĩ* nghiêng người, *token về* gật đầu theo nhịp (`stage.speak()`), trả lời xong bong bóng chat cạnh nhân vật báo **"Thưa Công Chúa, em đã làm xong yêu cầu của Công Chúa rồi ạ 🌸"**; kéo chuột trên ô nhân vật để xoay (`stage.rotate`), chạm nhẹ là nhảy (`stage.poke`). Muốn đổi nhân vật: thay `character.glb` (GLB có clip `idle` sẽ được ưu tiên chơi clip thay cho procedural).

## Test

```bash
python -m pytest tests/ -q    # chạy offline với fixtures, không cần mạng
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
- Mã truy cập là shared secret dài hạn (không có magic link/OAuth): gửi qua kênh riêng, lộ thì đổi mã như mục Đăng nhập & bảo mật. Muốn có đăng ký/khôi phục tự phục vụ thì nâng lên magic link hoặc OAuth.
- MCP là lớp **đọc**, không khởi động crawl trong tool call (crawl chạy background qua `scripts/crawl.py`/cron).

## Đã verify end-to-end (xem [VERIFICATION.md](VERIFICATION.md))

- 45/45 unit + contract test pass
- Crawl thật: 209 jobs (VietnamWorks 81, LinkedIn 72, TopCV 56), 178 job HCM, 184 companies
- Web: login → search 63 kết quả "content" → trang chi tiết → trang công ty
- MCP qua HTTP: initialize, tools/list (5 tool), search (63 kết quả, cursor), validation error, 401/403 auth + origin
- REST API: toàn bộ endpoint theo contract trong docs
