# Kế hoạch triển khai MCP và website tìm việc

**Mục tiêu:** triển khai được một website và remote MCP invite-only cho một nhóm bạn tìm việc Content Marketing/truyền thông đa phương tiện tại TP.HCM, với TopCV là nguồn ưu tiên.

## 1. Các giả định đã chốt

- UI và nội dung chính bằng tiếng Việt.
- Website kín; mọi route sản phẩm yêu cầu đăng nhập và email allowlist/lời mời.
- Đăng nhập cần cho lưu job và kết nối MCP.
- Phiên bản đầu của MCP chỉ đọc dữ liệu.
- Website mở URL nguồn để ứng tuyển, không tự động apply.
- TopCV có độ ưu tiên cao nhất trong ranking và badge nguồn.
- Công ty feedback phải có source, timestamp và confidence; không có dữ liệu thì hiển thị trạng thái chưa có.

## 2. Hai đường kỹ thuật chạy song song

### Product track

Xây website, MCP, account, search và company pages dựa trên fixture contract ổn định.

### Data pipeline track

- xây TopCV crawler và parser;
- chuẩn hóa, phân loại và chống trùng;
- đo freshness, parser success rate và source health;
- bổ sung company enrichment và nguồn dự phòng.

Hai track gặp nhau tại shared database và domain/search package, nên UI và MCP không phụ thuộc trực tiếp vào crawler.

## 3. Mốc triển khai đề xuất

| Mốc | Thời lượng ước tính cho 1 developer | Kết quả |
|---|---:|---|
| M0. Technical discovery | 2-3 ngày | Data contract và TopCV crawler spike |
| M1. Foundation | 3-4 ngày | Monorepo, CI, local stack, environments |
| M2. Data vertical slice | 7-10 ngày | TopCV adapter nội bộ -> normalize -> PostgreSQL |
| M3. Invite-only search web | 6-8 ngày | Search, filters, job/company detail, noindex |
| M4. Accounts và saved jobs | 4-6 ngày | Login, save/unsave, privacy controls |
| M5. Company enrichment | 5-8 ngày | Company resolution, source-aware feedback |
| M6. Remote MCP | 4-6 ngày | OAuth/PAT, 5 tools, contract tests |
| M7. Deployment hardening | 4-6 ngày | Deploy, monitoring, security checks |
| Buffer và pilot | 3-5 ngày | Parser fixes, ranking tuning, runbook |

Private beta kỹ thuật: khoảng **5-7 tuần**.

## 4. Work breakdown

### M0 — Technical discovery

**Công việc**

- Lập data inventory cho từng field TopCV và company feedback.
- Chạy spike Playwright trên 10-15 URL thuộc đủ role cluster.
- Xác nhận selector, JSON-LD, deadline, location mới/cũ và apply URL.
- Định nghĩa `JobSourceAdapter` và fixture contract.
- Đo thời gian tải, tỷ lệ 403/challenge và số request cho mỗi job.
- Xác định backoff, concurrency và cache TTL ban đầu.

**Nghiệm thu**

- Parser lấy được ít nhất 95% trường bắt buộc trên tập fixture.
- Xác định được browser flow có thể chạy lặp lại và tự phục hồi sau lỗi tạm thời.
- Job hết hạn và ngoài TP.HCM được nhận diện đúng trên tập fixture.
- Có fallback source plan nếu TopCV không ổn định hoặc tạm thời bị chặn.

### M1 — Foundation

**Công việc**

- Khởi tạo pnpm TypeScript monorepo theo kiến trúc `apps/*` và `packages/*`.
- Thêm lint, typecheck, unit test và build trong CI.
- Docker Compose cho PostgreSQL và Redis.
- Quản lý env schema và secret; tách local/staging/production.
- Tạo migration pipeline và seed fixtures.

**Nghiệm thu**

- Một lệnh khởi động local stack.
- CI chạy typecheck, test và build từ checkout sạch.
- Không có secret trong repository.

### M2 — Data vertical slice

**Công việc**

- Tạo schema `jobs`, `job_occurrences`, `companies`, `locations`, `job_skills`, `crawl_runs`.
- Viết TopCV adapter: discover, fetch detail, normalize, health check.
- Dùng BullMQ cho crawl, retry/backoff, concurrency và source rate limit.
- Chuẩn hóa salary, experience, work mode, deadline và location TP.HCM.
- Rule classifier cho role cluster, platform, tool và content type.
- Dedupe bằng source ID, canonical URL và fingerprint/fuzzy company-title.
- Mark inactive theo deadline và verification state.

**Nghiệm thu**

- Một crawl run có thể resume và chạy lại idempotent.
- Không sinh duplicate job canonical khi ingest cùng fixture hai lần.
- 100% kết quả có source URL, HCM verified và `last_verified_at`.
- Job hết hạn không xuất hiện trong search mặc định.

### M3 — Invite-only search website

**Công việc**

- Xây home/search, job detail và company detail.
- Search PostgreSQL với `unaccent`, `pg_trgm`, taxonomy và hard filters.
- Filter: role, lương, kinh nghiệm, khu vực, work mode, tool, portfolio.
- Cursor pagination và URL query state.
- Thêm auth wall, `noindex, nofollow`, không phát hành sitemap và disallow crawler.
- Loading, empty, error và stale-data states.
- Responsive và keyboard/screen-reader pass.

**Nghiệm thu**

- Search `content tiktok`, `video editor`, `truyền thông đa phương tiện` trả kết quả liên quan tại TP.HCM.
- Kết quả ngoài TP.HCM và job expired bị loại.
- Largest Contentful Paint mục tiêu dưới 2.5 giây ở percentile 75 trên production-like environment.
- Không có lỗi accessibility nghiêm trọng trong automated audit và keyboard smoke test.

### M4 — Account và saved jobs

**Công việc**

- Tích hợp Clerk Google/email login.
- Save/unsave job; trang danh sách đã lưu.
- Account deletion và xóa dữ liệu liên quan.
- Rate limit search API, anti-abuse và audit events.

**Nghiệm thu**

- Chỉ email đã được mời mới search và xem chi tiết.
- User chỉ đọc/sửa saved jobs của chính mình.
- Xóa account loại bỏ dữ liệu cá nhân theo retention policy.

### M5 — Company enrichment và feedback

**Công việc**

- Company resolver dựa trên tên chính thức, alias, website domain và address.
- Enrich từ website/social chính thức với provenance.
- Tạo `company_feedback_sources` và policy theo nguồn.
- Tích hợp rating aggregate từ các nguồn đã chọn.
- Hiển thị confidence, review count, timestamp và link nguồn.
- Admin correction flow cho merge/split company.

**Nghiệm thu**

- Hai tên biến thể của cùng công ty được merge có thể audit/revert.
- Không trộn first-party company profile với independent feedback.
- Mỗi rating có source và ngày cập nhật.
- Review và rating luôn giữ link nguồn, timestamp và confidence.

### M6 — Remote MCP

**Công việc**

- Gắn MCP handler SDK TypeScript v2 tại `/mcp` trong `apps/web` để giảm chi phí vận hành.
- Thêm OAuth qua Clerk, Protected Resource Metadata, scopes và consent; cấp PAT có hạn cho client không hỗ trợ OAuth.
- Đăng ký 5 tool trong tài liệu nghiên cứu MCP.
- Dùng chung repositories/search service với website.
- Structured output, cursor, provenance và actionable errors.
- Rate limit theo user/IP và audit log không chứa token.

**Nghiệm thu**

- `tools/list` và tất cả tool chạy qua client SDK chính thức.
- Output khớp `outputSchema` trên success và error cases.
- Kết nối thành công từ Codex, Claude và VS Code/Cursor hoặc ghi rõ client chưa hỗ trợ version nào.
- 401/403, insufficient scope, invalid cursor và timeout có hành vi dự đoán được.
- Không tool nào khởi động browser crawl.

### M7 — Nguồn bổ sung

Thực hiện sau khi TopCV vertical slice ổn định:

1. Glints;
2. VietnamWorks qua API/feed ổn định nếu tìm được;
3. CareerViet/JobsGO;
4. Indeed/Glassdoor chỉ qua quyền đối tác/API phù hợp;
5. LinkedIn chỉ khi có phương thức hợp lệ và chịu được rate limit.

JobSpy hữu ích cho nghiên cứu và smoke test, nhưng không nên là data foundation của bản deploy dùng chung vì connector dễ gặp rate limit hoặc thay đổi layout ngoài kiểm soát.

**Nghiệm thu**

- Mỗi adapter có rate limit, cache/retention và provenance riêng.
- Dedupe giữ đủ provenance khi một job xuất hiện ở nhiều nguồn.
- Source failure không làm search/MCP ngừng phục vụ dữ liệu đã có.

### M8 — Deployment hardening

**Công việc**

- Docker images cho web có MCP route và worker Playwright.
- Render Blueprint cho web service, worker, cron, Postgres và Key Value.
- Migrations/release command, backup và rollback.
- OpenTelemetry, error tracking, dashboards và alerts.
- Security headers, CSP, CSRF, dependency scan, secret scan và abuse limits.
- Load test search và MCP; soak test worker.
- Runbook cho parser break, 403/429, queue backlog, database restore và OAuth outage.
- Trang thông tin nội bộ về data source, freshness và cách báo dữ liệu sai.

**Nghiệm thu**

- Deploy staging và production từ CI với health check.
- Restore test từ backup thành công.
- Alert phát hiện crawler không có record và parser null-rate tăng.
- Search/MCP vẫn đọc được dữ liệu khi worker dừng.
- TopCV adapter có kill switch và không làm web/MCP mất khả dụng khi source lỗi.

## 5. API contract của website

Các endpoint công khai dùng cùng domain service với MCP:

```text
GET /api/jobs
GET /api/jobs/:jobId
GET /api/companies/:companyId
GET /api/companies/:companyId/feedback
GET /api/facets
POST /api/me/saved-jobs
DELETE /api/me/saved-jobs/:jobId
GET /api/me/saved-jobs
```

Mọi endpoint cần auth, allowlist và rate limit. Endpoint `/api/me/*` còn kiểm tra authorization theo user ID.

## 6. Test strategy

| Lớp | Test có ý nghĩa |
|---|---|
| Parser | Golden fixtures theo nhiều layout TopCV; contract required fields |
| Normalize | Salary, deadline, HCM address, role/tool extraction |
| Dedupe | Cùng nguồn, nhiều nguồn, alias company, false-positive cases |
| Search | Relevance set cho các query mục tiêu và negative queries |
| Web | Playwright user journeys: search -> detail -> source; login -> save |
| MCP | Client/server contract, schema, auth, rate limit, prompt injection |
| Worker | Retry/backoff, idempotency, graceful shutdown, queue recovery |
| Production | Health, backup restore, load, accessibility và security smoke |

## 7. Go-live checklist

- [ ] TopCV crawler chạy ổn qua ít nhất 7 ngày pilot.
- [ ] Company feedback hiển thị source, timestamp và confidence.
- [ ] Data-source disclosure và freshness đã hiển thị cho nhóm.
- [ ] Active/expired logic đã chạy ổn qua ít nhất 7 ngày pilot.
- [ ] Parser alert và source kill switch hoạt động.
- [ ] Backup restore đã diễn tập.
- [ ] OAuth MCP được test với các client mục tiêu.
- [ ] Rate limit và abuse protection đã bật.
- [ ] Không có P0/P1 bug, lỗi accessibility nghiêm trọng hoặc secret leak.
- [ ] Runbook và người nhận alert đã được chỉ định.

## 8. Tiêu chí thành công của private beta

- Ít nhất 95% job hiển thị còn active tại thời điểm kiểm tra.
- Ít nhất 90% job có role cluster đúng trên tập đánh giá thủ công.
- Ít nhất 90% kết quả top 10 liên quan cho bộ query chuẩn.
- Ít nhất 95% tool call MCP thành công, không tính invalid input.
- p95 search API dưới 800 ms khi cache miss trên tải beta dự kiến.
- Không có job ngoài TP.HCM trong search mặc định.
- 100% job và feedback có provenance hiển thị được.

## 9. Thứ tự bắt đầu thực tế

1. Chốt data contract và fixture trong M0.
2. Hoàn thành một vertical slice duy nhất: TopCV fixture -> DB -> web search -> MCP search.
3. Chạy TopCV crawler pilot với concurrency thấp và health metrics.
4. Sau khi slice chạy end-to-end mới mở rộng company feedback và nguồn khác.
5. Bật crawler cho nhóm sau khi pilot ổn định và kill switch đã được kiểm thử.

