# Nghiên cứu MCP cho nền tảng tìm việc Content Marketing tại TP.HCM

**Ngày kiểm chứng:** 15/10/2026  
**Phạm vi:** MCP từ xa cho một nhóm bạn được mời, chỉ đọc dữ liệu việc làm đã được thu thập và chuẩn hóa.

## 1. Kết luận kỹ thuật

Nên xây MCP server bằng TypeScript, dùng SDK chính thức MCP v2:

- `@modelcontextprotocol/server`
- Zod v4 cho `inputSchema` và `outputSchema`
- Streamable HTTP tại `https://<domain>/mcp`
- Chế độ tương thích cả MCP 2026-07-28 và nhóm client 2025 trong giai đoạn đầu
- OAuth 2.1 cho endpoint invite-only; có thể cấp personal access token cho client chưa hỗ trợ OAuth
- MCP chỉ đọc PostgreSQL; crawler không chạy bên trong một tool call

SDK TypeScript v2 hiện là nhánh ổn định và hiện thực MCP spec 2026-07-28. SDK đã tách package server/client và cung cấp adapter cho Node HTTP, Express, Fastify và Hono. Nguồn: [MCP TypeScript SDK](https://github.com/modelcontextprotocol/typescript-sdk), [tài liệu SDK v2](https://ts.sdk.modelcontextprotocol.io/v2/).

Streamable HTTP là transport phù hợp cho server từ xa. Spec yêu cầu endpoint HTTP duy nhất, kiểm tra `Origin`, xác thực kết nối và rate limit. Nguồn: [MCP transport specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports), [MCP tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).

## 2. Vì sao không để MCP scrape trực tiếp

Một tool call cần trả kết quả nhanh, có giới hạn thời gian và có thể bị gọi lặp lại. Browser crawl TopCV có độ trễ lớn, dễ gặp challenge và làm chi phí tăng theo số người dùng. Tách crawler thành worker giúp:

- kiểm soát tần suất truy cập từng nguồn;
- retry có backoff mà không giữ kết nối MCP;
- chống trùng dữ liệu trước khi người dùng tìm kiếm;
- trả kết quả MCP ổn định trong vài trăm mili giây;
- ghi nhận rõ thời điểm thu thập và độ mới của dữ liệu.

MCP và website dùng chung package truy vấn domain. Điều này tránh hai bộ lọc/ranking khác nhau.

## 3. Thiết kế tool đề xuất

Tên tool có tiền tố `vn_jobs_` để tránh trùng khi client kết nối nhiều MCP server.

### `vn_jobs_search_jobs`

Tìm các công việc thuộc Content Marketing và truyền thông đa phương tiện tại TP.HCM.

Input chính:

| Trường | Kiểu | Ghi chú |
|---|---|---|
| `query` | string, optional | Từ khóa tự do như `content tiktok`, `video editor` |
| `role_clusters` | enum[] | `content_marketing`, `social_media`, `communications_pr`, `video_production`, `multimedia_design`, `creative_production` |
| `districts` | string[] | Chấp nhận tên quận/huyện cũ và phường/xã mới |
| `salary_min_vnd` | integer, optional | Lương tối thiểu mong muốn |
| `experience_max_years` | number, optional | Số năm kinh nghiệm tối đa |
| `work_modes` | enum[] | `onsite`, `hybrid`, `remote` |
| `posted_within_days` | integer | Mặc định 14, tối đa 60 |
| `portfolio_required` | boolean, optional | Lọc yêu cầu portfolio |
| `source` | enum | `topcv` hoặc `all`; mặc định `topcv` |
| `page_size` | integer | Mặc định 10, tối đa 25 |
| `cursor` | string, optional | Phân trang ổn định |

Output gồm `items`, `next_cursor`, `total_estimate`, `applied_filters` và `data_freshness`. Mỗi item có `job_id`, tiêu đề, công ty, lương, địa điểm, hạn nộp, mức phù hợp, nguồn, URL gốc và thời điểm thu thập.

### `vn_jobs_get_job`

Lấy bản ghi đầy đủ theo `job_id`, gồm:

- mô tả và yêu cầu đã làm sạch;
- nền tảng nội dung: TikTok, Facebook, YouTube, website;
- công cụ: CapCut, Canva, Adobe Premiere, After Effects;
- loại nội dung và vai trò;
- portfolio/on-camera/client-facing;
- lịch làm việc, làm thứ Bảy hoặc lịch quay;
- provenance theo từng trường và `last_verified_at`;
- URL ứng tuyển gốc.

### `vn_jobs_get_company`

Trả hồ sơ công ty đã chuẩn hóa: tên, alias, ngành, quy mô, website, địa chỉ, kênh chính thức, số việc đang tuyển và các nguồn dùng để xác minh.

### `vn_jobs_get_company_feedback`

Trả dữ liệu đánh giá có nguồn gốc rõ ràng:

- rating tổng hợp và số lượng đánh giá khi tìm thấy nguồn phù hợp;
- các tiêu chí như văn hóa, cân bằng công việc, quản lý;
- nguồn, URL, ngày cập nhật và mức tin cậy;
- tóm tắt xu hướng; không sao chép nguyên văn review dài.

Nếu không có nguồn đủ tin cậy, tool trả `status: "unavailable"` cùng các liên kết để người dùng tự kiểm tra. Không suy diễn rating từ nội dung quảng bá của công ty.

### `vn_jobs_get_market_facets`

Trả các lựa chọn lọc hiện có: role cluster, khu vực, mức lương, kinh nghiệm, công cụ và số lượng job. Tool này giúp agent xây truy vấn hợp lệ trước khi search.

## 4. Chuẩn response

Mỗi tool khai báo cả `inputSchema` và `outputSchema`. Kết quả trả hai dạng:

1. `structuredContent` để agent xử lý chắc chắn;
2. một `TextContent` ngắn chứa JSON hoặc tóm tắt tương đương để tương thích client cũ.

MCP spec yêu cầu `structuredContent` phải khớp `outputSchema` nếu schema được khai báo và khuyến nghị kèm text để tương thích ngược. Nguồn: [Structured Content và Output Schema](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#structured-content).

Mẫu provenance cho một job:

```json
{
  "source": "topcv",
  "source_url": "https://www.topcv.vn/viec-lam/...",
  "source_job_id": "2254903",
  "first_seen_at": "2026-10-15T02:00:00Z",
  "last_verified_at": "2026-10-15T08:00:00Z",
  "expires_at": "2026-10-30T16:59:59Z",
  "is_active": true
}
```

## 5. Xác thực và giới hạn sử dụng

Đề xuất dùng Clerk trong MVP invite-only vì một hệ thống có thể xử lý đăng nhập website, email allowlist và OAuth cho MCP. Clerk công bố hỗ trợ Client ID Metadata Documents, Dynamic Client Registration và PKCE, phù hợp luồng đăng nhập của các MCP client khác nhau. Nguồn: [Clerk MCP server guide](https://clerk.com/docs/guides/ai/mcp/build-mcp-server).

Scope ban đầu:

- `jobs:read`
- `companies:read`
- `feedback:read`

Phiên bản đầu của MCP chỉ có tool đọc nên không cần scope ghi. Saved jobs nằm trong website ở MVP. Nếu sau này thêm tool lưu việc, dùng scope riêng `saved_jobs:write` và không trộn vào quyền mặc định.

Giới hạn đề xuất cho private beta:

- theo user và IP;
- giới hạn `page_size` tối đa 25;
- cache truy vấn phổ biến trong thời gian ngắn;
- timeout database và tool call;
- nhật ký chỉ lưu tool name, latency, result count và mã lỗi; không lưu token hoặc toàn bộ query chứa dữ liệu cá nhân.

## 6. Bảo mật MCP

- Xác minh bearer token và audience cho mọi request.
- Phục vụ OAuth Protected Resource Metadata theo spec.
- Kiểm tra `Origin` và host allowlist.
- Schema từ chối trường thừa và giới hạn độ dài chuỗi/mảng.
- Escape/sanitize dữ liệu nguồn trước khi đưa vào text result để giảm prompt injection từ JD.
- Gắn dữ liệu nguồn trong một object, không nối JD vào system prompt.
- Chặn URL không thuộc allowlist khi trả apply link.
- Rate limit cả HTTP layer và tool layer.
- Ghi audit ID cho mỗi call để truy vết.

Spec yêu cầu server xác thực input, áp dụng access control, rate limit và sanitize output. Nguồn: [MCP tool security considerations](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#security-considerations).

## 7. Kiểm thử MCP

1. Unit test cho schema, cursor, ranking và redaction.
2. Contract test từng tool với `outputSchema`.
3. In-process test bằng MCP client chính thức và `createMcpHandler`; SDK hỗ trợ chạy client/server không cần mở network. Nguồn: [MCP SDK testing guide](https://ts.sdk.modelcontextprotocol.io/v2/testing).
4. Test Streamable HTTP qua reverse proxy, gồm `Origin`, 401, 403, rate limit và timeout.
5. Smoke test với ít nhất ba client: Codex, Claude và VS Code/Cursor.
6. Test prompt injection bằng JD chứa chỉ dẫn giả, HTML và URL lạ.

## 8. Quyết định cần giữ ổn định

- MCP là lớp đọc, không phải crawler.
- `job_id` nội bộ ổn định; URL nguồn có thể thay đổi.
- Pagination dùng opaque cursor, không dùng offset cho dữ liệu liên tục cập nhật.
- Mọi rating/feedback đều có source và timestamp.
- Không có dữ liệu thì trả trạng thái rõ ràng, không để model tự đoán.
- MCP ưu tiên OAuth. Personal access token theo từng người chỉ là fallback cho client chưa hỗ trợ OAuth; token phải được hash, có ngày hết hạn và có thể thu hồi.

