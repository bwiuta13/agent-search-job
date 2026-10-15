# Research: nguồn dữ liệu cho Job Search Agent (Việt Nam)

> Ngày research: 2026-10-15. Mục tiêu: agent tìm job đang tuyển + thông tin công ty + feedback/review công ty.
> Ký hiệu: **[H]** = đã test thực tế từ máy này · **[R]** = chỉ đọc tài liệu/trang giá, chưa gọi thử.

## 1. Kết luận nhanh

- **Không cần trả tiền để có MVP tốt.** Các trang Việt Nam quan trọng nhất đều lấy được miễn phí:
  - VietnamWorks và TopDev có JSON API nội bộ trả về thông tin công ty rất đầy đủ.
  - ITviec và CareerViet là HTML render sẵn phía server, `curl` thường đọc được.
- **JobSpy dùng được cho Việt Nam** qua Indeed VN và LinkedIn. **Google Jobs của JobSpy hiện không chạy.**
- **Review công ty là phần khó nhất:**
  - ITviec là nguồn tốt nhất cho công ty IT: điểm tổng, 5 tiêu chí, tiêu đề review đều public. Muốn đọc full text thì phải login.
  - Indeed VN cho điểm rating và số review, lấy được ngay qua JobSpy.
  - Glassdoor chỉ lấy được qua API bên thứ ba (trả phí).
  - Phần còn lại dùng web search API (Tavily, Brave) rồi để LLM tóm tắt.
- **Thông tin pháp lý của công ty** (mã số thuế, ngày thành lập, người đại diện): lấy từ masothue.com.
  - Cần gửi header giống trình duyệt đầy đủ. Nếu thiếu header sẽ bị 403.

## 2. Đối chiếu 4 cách trong gợi ý ban đầu với thực tế

| Cách | Gợi ý nói | Thực tế kiểm chứng |
|---|---|---|
| 1. JobSpy | Lấy được LinkedIn/Indeed/Glassdoor/Google Jobs, miễn phí | ✅ Miễn phí, chạy tốt **[H]**: v1.3.0 (08/10/2026), không cần proxy ở lượng nhỏ.<br>❌ **Google trả 0 kết quả.** FAQ của chính JobSpy ghi Google "currently unavailable".<br>⚠️ Glassdoor (VN) chỉ có tên công ty.<br>⭐ **Indeed VN là nguồn tốt nhất**: có rating, số review, quy mô, doanh thu, website, đôi khi có lương VND. |
| 2. JSearch / SerpApi / Apify | JSearch free vài trăm request/tháng | ⚠️ Free tier trực tiếp trên OpenWeb Ninja chỉ **50 request**. Quota RapidAPI chưa xác minh được **[R]**.<br>SerpApi free **250 search/tháng** **[R]**.<br>Apify có actor cho VietnamWorks/TopCV/CareerViet, nhưng ít người dùng, dễ gãy. **Chưa có actor ITviec.** |
| 3. AI scraper cho ITviec/TopCV vì Cloudflare chặn | ITviec, TopCV chặn chặt | ❌ **ITviec không chặn**: `curl` trả 200, HTML render sẵn **[H]**.<br>✅ **TopCV chặn thật**: 403 với cả `curl` lẫn Jina. Crawl4AI (headless Chromium) thì vào được **[H]**.<br>⚠️ Jina Reader **fail với VietnamWorks** (ra "0 việc làm"). |
| 4. Reverse-engineer API nội bộ | Hiệu quả cho VietnamWorks/TopDev | ✅ **Đúng, cách tốt nhất** **[H]**.<br>Cả hai API trả JSON có size, industry, địa chỉ, benefits, skills, lương.<br>⚠️ Xem lưu ý ToS/robots ở mục 6. |

## 3. Nguồn job

| Nguồn | Cách lấy | Thông tin công ty kèm theo | Ghi chú |
|---|---|---|---|
| **VietnamWorks** | `POST ms.vietnamworks.com/job-search/v1.0/search` **[H]** | Rất đủ: `companySize`, `industriesV3`, `address`, `companyProfile`, `benefits[]`, `skills[]`, lương min/max | Bắt buộc `order: relevant`, nếu không sẽ ra tin quảng cáo. HCM = `cityId 29` |
| **TopDev** | `GET api.topdev.vn/td/v2/jobs` + `/td/v2/companies/{id}` **[H]** | Rất đủ: size, số nhân viên, industry, quốc tịch, benefits, website, tech stack | Phải truyền `fields[...]`. HCM = `region_ids=79`. Search khá "lỏng" |
| **ITviec** | Parse HTML `/it-jobs/<kw>/ho-chi-minh-hcm?page=N` **[H]** | Trang `/companies/<slug>`: type, industry, size, OT policy, tech stack, perks | Lương cần login. 20 job/trang |
| **CareerViet** | Parse HTML (cần `--compressed`) **[H]** | Trang nhà tuyển dụng: địa chỉ, quy mô, website | Có lương dạng text |
| **TopCV** | Crawl4AI / Playwright headless **[H]** | Trang `/cong-ty/...`: size, industry, địa chỉ | Chặn HTTP thường và Jina. Mất ~3.7 phút để cài Crawl4AI + Chromium |
| **Indeed VN** (JobSpy) | `scrape_jobs(site_name="indeed", country_indeed="vietnam")` **[H]** | rating, review count, size, revenue, website | Địa chỉ trả về là trụ sở chính (HQ) toàn cầu, không phải văn phòng VN. Nhiều tin trùng TopCV/CareerViet |
| **LinkedIn** (JobSpy) | `site_name="linkedin"`, `linkedin_fetch_description=True` **[H]** | industry, logo, job level | Burst request bị 429 |
| JSearch (Google for Jobs) | REST hoặc MCP của OpenWeb Ninja **[R]** | employer website/logo, `employer_reviews[]` | ~$0.003/request. Chưa test độ phủ `country=vn` |
| SerpApi Google Jobs | REST hoặc MCP **[R]** | Ít (chỉ tên công ty) | 250 search/tháng miễn phí |

## 4. Nguồn thông tin công ty và review

| Nguồn | Dữ liệu | Truy cập | Đánh giá |
|---|---|---|---|
| **ITviec review** `/companies/<slug>/review` | Điểm tổng, % recommend, 5 sub-score, tiêu đề + sao của ~10 review/trang, 1 review đầy đủ. VD: FPT Software 2,206 review **[H]** | Scrape HTML, không cần login cho phần aggregate | ⭐ Nguồn review chính cho công ty IT. Full text cần login: **không tự động login** |
| **Indeed VN rating** | rating + review count (qua JobSpy) **[H]** | Có sẵn khi search job | Bổ sung tốt, nhưng không có nội dung review |
| **masothue.com** | Tên pháp lý, MST, người đại diện, địa chỉ, ngày hoạt động, ngành nghề **[H]** | Scrape, cần full browser headers | Dùng để xác minh pháp nhân. Một tên thương hiệu có thể ứng với nhiều pháp nhân (FPT ra ~10 kết quả) |
| TopDev / VietnamWorks / ITviec company page | Size, benefits, tech stack, mô tả **[H]** | API / HTML | Hồ sơ "mềm" của công ty |
| Glassdoor | Review, lương | Chặn bot **[H]**. Lấy qua OpenWeb Ninja "Real-Time Glassdoor Data" hoặc Apify **[R]** | Dữ liệu VN mỏng. Chỉ đáng trả tiền nếu nhắm công ty nước ngoài |
| Web search API (Tavily / Brave / Exa) | Snippet từ Voz, Reddit, Indeed, báo chí | API key. Tavily free 1k credit/tháng **[R]** | Lấp chỗ trống cho công ty non-IT và thảo luận diễn đàn |
| LinkedIn company page | Size, industry, HQ | Fetch lẻ được, lặp nhiều bị login wall **[H]** | Chỉ dùng qua search snippet |
| Google Maps reviews | Review văn phòng | Places API / SerpApi, trả phí **[R]** | Đa phần là review của khách, ít giá trị cho mục đích này |
| ~~1900.com.vn~~, ~~reviewcongty~~ | | **Đã chết** **[H]** | Bỏ qua |
| Crunchbase, Voz, Comparably, Blind | | 403 / quá đắt **[H]** | Bỏ qua, hoặc chỉ lấy qua web search |

## 5. Kiến trúc đề xuất

Viết dưới dạng **MCP server (Python, FastMCP)**. Cách này cắm được vào Claude Desktop/Code hoặc bất kỳ framework agent nào (LangGraph, CrewAI...). Server gồm 3 tool:

```
search_jobs(query, location="Ho Chi Minh", sources=[...], limit=20)
  ├─ VietnamWorks API ─┐
  ├─ TopDev API        │
  ├─ ITviec HTML       ├─► chuẩn hoá về 1 schema chung
  ├─ CareerViet HTML   │    → khử trùng lặp (tên công ty + tiêu đề đã chuẩn hoá)
  ├─ JobSpy (Indeed VN + LinkedIn)
  └─ (tuỳ chọn) TopCV qua Crawl4AI
      → LLM chấm điểm độ liên quan (vì search ở mọi nguồn đều lỏng)

get_company_profile(name | tax_code)
  ├─ masothue.com: thông tin pháp lý (MST, ngày thành lập, người đại diện)
  ├─ ITviec / TopDev / VietnamWorks: size, industry, tech stack, benefits
  └─ cache theo MST/slug 7–30 ngày, ghi rõ nguồn của từng field

get_company_reviews(name)
  ├─ ITviec: điểm tổng + sub-score + tiêu đề review gần đây
  ├─ Indeed VN: rating + review count
  ├─ Tavily: "<công ty> review", "<công ty> đánh giá nhân viên", "<công ty> voz OR reddit"
  ├─ (tuỳ chọn, trả phí) Glassdoor qua OpenWeb Ninja
  └─ LLM tóm tắt pros/cons/OT/lương, kèm link nguồn + mức độ tin cậy theo số lượng review
```

**Chi phí MVP: ~$0.** Tất cả nguồn ở trên đều miễn phí, trừ Tavily, mà Tavily cũng có free 1k credit/tháng. Nâng cấp tuỳ chọn: JSearch + Glassdoor qua MCP của OpenWeb Ninja (vài USD/tháng cho dùng cá nhân).

## 6. Rủi ro và lưu ý

- **ToS:**
  - VietnamWorks và TopDev cấm sao chép/khai thác thương mại nội dung.
  - VietnamWorks cấm dùng dữ liệu để xây dựng app tuyển dụng tương tự nhằm mục đích thương mại.
  - Agent dùng cho cá nhân thì rủi ro thấp. **Không nên publish/redistribute dữ liệu.**
- **robots.txt:** `api.topdev.vn/robots.txt` disallow mọi bot trừ Google. ITviec, TopCV, VietnamWorks, CareerViet cho phép trang search/company.
- **Độ bền:** API nội bộ và HTML có thể đổi bất cứ lúc nào.
  - Mỗi nguồn nên là một adapter riêng, lỗi thì bỏ qua, không làm sập cả agent.
  - Pin version JobSpy.
- **Rate limit:** giữ lượng request thấp, có cache. LinkedIn trả 429 khi bị burst.
- **TopDev:** `salary.value` bị che, nhưng `min_filter`/`max_filter` lộ số thật. Có vẻ là lỗi phía họ, cân nhắc trước khi dùng.
- **Review ẩn danh = ý kiến chưa kiểm chứng.** Agent nên trích nguồn và ghi rõ mức độ tin cậy.
- **Không** tự động login ITviec/LinkedIn bằng tài khoản thật, **không** bypass CAPTCHA (VD: cổng dangkykinhdoanh.gov.vn).

## 7. Endpoint mẫu (đã chạy được 2026-10-15)

```bash
# VietnamWorks: AI Engineer tại HCM (cityId 29), sắp theo độ liên quan
curl -X POST https://ms.vietnamworks.com/job-search/v1.0/search -H 'Content-Type: application/json' \
 -d '{"userId":0,"query":"ai engineer","filter":[{"field":"workingLocations.cityId","value":"29"}],"ranges":[],"order":[{"field":"relevant","value":"asc"}],"hitsPerPage":10,"page":0}'

# TopDev: job list (region 79 = HCM) + chi tiết công ty
curl 'https://api.topdev.vn/td/v2/jobs?keyword=AI%20Engineer&region_ids=79&page=1&page_size=15&locale=vi_VN&fields%5Bjob%5D=id,title,salary,skills_str,addresses,company,detail_url&fields%5Bcompany%5D=id,display_name,company_size,industries_str,detail_url'
curl 'https://api.topdev.vn/td/v2/companies/95300?locale=vi_VN&fields%5Bcompany%5D=display_name,description,company_size,num_employees,industries_str,benefits,website,addresses,skills_str'

# ITviec
https://itviec.com/it-jobs/ai-engineer/ho-chi-minh-hcm?page=2
https://itviec.com/companies/fpt-software/review

# CareerViet (kl8 = HCM)
curl --compressed https://careerviet.vn/viec-lam/ai-engineer-tai-ho-chi-minh-kl8-vi.html
```

```python
# JobSpy: Indeed VN là nguồn có nhiều thông tin công ty nhất
from jobspy import scrape_jobs
df = scrape_jobs(site_name=["indeed", "linkedin"], search_term="AI Engineer",
                 location="Ho Chi Minh City, Vietnam", country_indeed="vietnam",
                 results_wanted=20, linkedin_fetch_description=True)
```

## 8. Chưa kiểm chứng

- Số kết quả thực tế cho Việt Nam của JSearch, SerpApi và Glassdoor API (cần API key để test).
- Quota RapidAPI của JSearch.
- ToS của ITviec, CareerViet, TopCV (không tìm thấy trang điều khoản).

## Nguồn chính

- JobSpy: https://github.com/speedyapply/JobSpy · https://pypi.org/project/python-jobspy/
- OpenWeb Ninja (JSearch, Glassdoor, MCP): https://www.openwebninja.com/mcp · https://agents.openwebninja.com/manifest.json
- SerpApi: https://serpapi.com/pricing · https://serpapi.com/google-jobs-api
- Apify MCP: https://docs.apify.com/platform/integrations/mcp
- Firecrawl: https://www.firecrawl.dev/pricing · Jina Reader: https://jina.ai/reader/ · Crawl4AI: https://pypi.org/project/Crawl4AI/
- Proxycurl đóng cửa: https://nubela.co/blog/goodbye-proxycurl/
- ITviec review: https://itviec.com/companies/fpt-software/review · masothue: https://masothue.com/0101601092-cong-ty-tnhh-phan-mem-fpt
- reviewcongty bị xử lý: https://znews.vn/website-reviewcongty-bi-to-dang-thong-tin-sai-su-that-ve-doanh-nghiep-post1440492.html
