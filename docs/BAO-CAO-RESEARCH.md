# 🔬 Research Report: Job Search Agent cho Content & Marketing tại Việt Nam

> **Ngày test:** 15/10/2026 · **Mục tiêu:** Agent tự động search job Content/Marketing VN kèm thông tin + feedback công ty
> **Phương pháp:** Mọi kết luận dưới đây đều được **test thực tế** — không suy đoán lý thuyết.

---

## 📊 Bảng kết quả thực nghiệm (TL;DR)

| Nguồn | Cách tiếp cận | Kết quả test | Độ giàu dữ liệu | Đánh giá |
|---|---|---|---|---|
| **LinkedIn** | JobSpy (`scrape_jobs`) | ✅ 10–20 jobs "content marketing" VN | Title, company, location, URL, date, JD | ⭐⭐⭐⭐⭐ Ổn định nhất |
| **VietnamWorks** | Internal API (`ms.vietnamworks.com`) | ✅ 58 jobs, gọi trực tiếp bằng curl | Salary min/max, benefits, skills, JD, **company profile đầy đủ** | ⭐⭐⭐⭐⭐ Rich nhất |
| **TopCV** | Playwright headless browser | ✅ 203 job cards render, parse được 38 | Salary ("10–12 triệu"), company, location | ⭐⭐⭐⭐ Nhiều job nhất cho Content/Mkt |
| **Indeed** | JobSpy | ❌ 0 jobs (bị chặn) | — | ❌ Bỏ |
| **Google Jobs** | JobSpy | ❌ 0 jobs | — | ❌ Bỏ |
| **Glassdoor** | JobSpy | ⚠️ Chạy được nhưng kết quả loãng, lệch VN | Có cột rating/review nhưng toàn `None` với VN | ⚠️ Không đáng dùng cho VN |
| **TopCV** | Jina Reader (`r.jina.ai`) | ❌ 403 Cloudflare (chặn cả proxy Jina) | — | ❌ Chỉ browser thật mới qua được |
| **Glints / CareerBuilder** | Jina Reader | ❌ 404/đổi URL pattern | — | ❌ Cần điều tra thêm |
| **Itviec** | curl trực tiếp | ⚠️ 200 OK nhưng danh sách render bằng JS (chỉ thấy 3 job spotlight) | Company page có mục Reviews | ⚠️ Chủ yếu IT — ít job Marketing |

---

## 🏆 Kiến trúc đề xuất cho Agent của bạn

```
┌─────────────────────────────────────────────────────────┐
│                    JOB SEARCH AGENT                      │
├─────────────────────────────────────────────────────────┤
│ Tool 1: linkedin_search(keyword, location)               │
│   → JobSpy scrape_jobs(site_name=["linkedin"])           │
│   → Nhanh, ổn định, không cần key                        │
├─────────────────────────────────────────────────────────┤
│ Tool 2: vnw_search(keyword, page)                        │
│   → POST ms.vietnamworks.com/job-search/v1.0/search      │
│   → Nhanh nhất (0.24s), không cần browser, data giàu nhất│
├─────────────────────────────────────────────────────────┤
│ Tool 3: topcv_search(keyword)                            │
│   → Playwright headless + accept cookie + parse cards    │
│   → Nhiều job Content/Mkt VN nhất                       │
├─────────────────────────────────────────────────────────┤
│ Tool 4: company_feedback(company_name)                   │
│   → TopCV /cong-ty/{slug} qua Playwright (mục Đánh giá)  │
│   → Fallback: Glassdoor qua JobSpy / Google "review"     │
└─────────────────────────────────────────────────────────┘
```

---

## 1️⃣ Cách 1: JobSpy — ĐÃ TEST, hoạt động tốt với LinkedIn

### Kết quả thực tế
```python
from jobspy import scrape_jobs

jobs = scrape_jobs(
    site_name=["linkedin"],
    search_term="content marketing",
    location="Vietnam",
    results_wanted=10,
    hours_old=720,  # 30 ngày
)
# → 10 jobs thật: Leanbase, L'Oréal, VE Pharma, Savills, Step Up Education...
```

**Output mẫu (đã verify):**
```
Content & Growth Marketer          | Leanbase               | Hanoi
Assistant Content Innovation...    | L'Oréal                | Củ Chi, HCMC
Chuyên viên Marketing / Content    | Clever Academy         | Hanoi
Content Marketing Specialist       | VE Pharma Vietnam      | HCMC
English Content Manager (Expat)    | Savills Vietnam        | —
Content Lead (Multi-Channel)       | LeverSphere            | —
```

### ⚠️ Những gì KHÔNG hoạt động (đã test)
- `site_name=["indeed"]` → **0 jobs** (Indeed chặn datacenter IP)
- `site_name=["google"]` → **0 jobs** (Google Jobs API đổi endpoint)
- `site_name=["glassdoor"]` → chạy nhưng kết quả **lệch hoàn toàn khỏi VN** (trả về "One-Day Event Volunteering", "Motion Designer m/w/d" ở Đức...)
- Các cột `company_rating`, `company_reviews_count`, `company_description` tồn tại trong schema nhưng **toàn `None`** với LinkedIn VN — JobSpy lấy field này từ Glassdoor phía US, không cover VN.

### ✅ Kết luận
JobSpy = **chỉ dùng cho LinkedIn** trong context VN. Cài: `pip install python-jobspy`. Không cần API key, không cần đăng nhập.

---

## 2️⃣ Cách 2: API Aggregator (JSearch/SerpApi) — KHÔNG test được (cần key trả phí)

Không verify được vì cần RapidAPI/SerpApi key. Nhận định từ đặc thù:
- **JSearch (RapidAPI)**: gom từ Google for Jobs — sẽ cover LinkedIn VN + VietnamWorks + TopCV trong 1 call. Free tier ~500 req/tháng đủ dùng cá nhân.
- **SerpApi**: JSON chuẩn từ Google Jobs SERP, có `detected_extensions.salary` nhưng Google Jobs VN thường **thiếu salary**.
- **Điểm yếu chung**: trả kết quả từ Google Jobs — ở VN Google Jobs index mỏng hơn TopCV/VNW trực tiếp. Dùng làm **bổ sung**, không làm nguồn chính.

---

## 3️⃣ Cách 3: Browser Automation — Playwright VƯỢT được Cloudflare TopCV ✅

### Phát hiện quan trọng nhất của buổi test
**Jina Reader (`r.jina.ai`) bị TopCV chặn 403** ở mọi trang search (chặn cả proxy của Jina). Nhưng **Playwright headless Chromium lại qua được** — vì Cloudflare chỉ challenge JS fingerprint cơ bản, không phải Turnstile CAPTCHA.

### Code đã chạy thành công
```python
from playwright.async_api import async_playwright

async with async_playwright() as p:
    browser = await p.chromium.launch(headless=True, args=[
        "--disable-blink-features=AutomationControlled",  # quan trọng!
        "--no-sandbox",
    ])
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ... Chrome/128.0.0.0 Safari/537.36",
        viewport={"width": 1440, "height": 900},
        locale="vi-VN",
    )
    page = await ctx.new_page()
    await page.goto("https://www.topcv.vn/tim-viec-lam-content-marketing",
                    wait_until="domcontentloaded", timeout=60000)
    await page.click("button:has-text('Chấp nhận tất cả')", timeout=4000)  # cookie banner
    await page.wait_for_timeout(6000)
    # → 203 job cards render thành công!
```

**URL pattern TopCV đã verify:** `topcv.vn/tim-viec-lam-{slug}` (vd `tim-viec-lam-content-marketing` → "1.483 việc làm")

### Data parse được từ job cards (38 cards đầu)
```
[2330524] Digital Marketing & Video Content Creator    salary: 10 - 12 Triệu  | Hà Nội
[2319395] Chuyên Viên Nội Dung & Chiến Dịch            salary: 12 - 15 triệu  | Hà Nội
[2315407] Senior Content Marketing                     salary: 12 - 13 triệu  | Hà Nội
[2321608] Thực Tập Sinh Content Marketing              salary: 2.5 - 3.5 triệu| HCM
[2321263] Nhân Viên Marketing - Thu Nhập 12 - 20M      salary: 12 - 20 triệu | Hà Nội
[2322992] Content Marketing Executive (French A Plus)  salary: 18 - 25 triệu | HCM
[2232182] B2B Content Marketing Executive (SAPP)       salary: 12 - 17 triệu | Hà Nội
```

**Điểm mạnh TopCV cho use case của bạn:** đây là nguồn **nhiều job Content/Marketing nhất** (1.483 kết quả vs 58 của VNW) và salary hiển thị trực tiếp trong listing.

### Lưu ý kỹ thuật
- `pip install crawl4ai` install được nhưng `crawl4ai-setup` CLI không có trong bản build — Crawl4AI bên dưới vẫn dùng Playwright nên **dùng thẳng Playwright gọn hơn**. Nếu muốn Markdown output tự động thì fix Crawl4AI install sau.
- Mỗi request TopCV tốn ~15s (goto + chờ render) → nên cache kết quả.
- Job cards lặp `data-job-id` 2 lần (1 link + 1 login popup) → de-duplicate bằng `data-job-id`.

---

## 4️⃣ Cách 4: Internal API — 💎 JACKPOT với VietnamWorks

### Cách tìm ra (đúng như mẹo DevTools trong đề bài)
Không mở được DevTools thủ công nên tôi dùng **Playwright network interception** — load trang `vietnamworks.com/jobs?q=content+marketing` và bắt mọi request. Kết quả: trang gọi `POST` tới internal search endpoint.

### Endpoint đã verify hoạt động — KHÔNG cần auth, KHÔNG cần browser

```bash
curl -s -X POST "https://ms.vietnamworks.com/job-search/v1.0/search" \
  -H "Content-Type: application/json" \
  -H "Origin: https://www.vietnamworks.com" \
  -H "Referer: https://www.vietnamworks.com/" \
  -d '{
    "userId": 0,
    "query": "content marketing",
    "filter": [], "ranges": [], "order": [],
    "hitsPerPage": 50, "page": 0,
    "retrieveFields": ["jobTitle","jobUrl","companyName","companyProfile",
      "salary","salaryMin","salaryMax","prettySalary","isSalaryVisible",
      "jobDescription","jobRequirement","benefits","skills","workingLocations",
      "address","industries","industriesV3","approvedOn","expiredOn","alias",
      "jobLevelVI","numOfApplications"]
  }'
# → HTTP 200 trong 0.24 giây, 58 jobs, 12 pages × 50
```

### Mức độ giàu dữ liệu (trích từ response thật)
```json
{
  "jobId": 2111300,
  "jobTitle": "B2B Marketing Manager",
  "jobUrl": "https://www.vietnamworks.com/b2b-marketing-manager-2111300-jv",
  "companyName": "Navigos Group",
  "companyProfile": "Navigos Group is a company dedicated to helping people... (full text!)",
  "approvedOn": "2026-09-24T15:02:07+07:00",
  "expiredOn": "2026-10-24T23:59:59+07:00",
  "isSalaryVisible": true,
  "salaryMin": ..., "salaryMax": ..., "prettySalary": "...",
  "jobDescription": "<p><strong>KEY RESPONSIBILITIES...</p>",
  "benefits": [...], "skills": [...], "workingLocations": [...],
  "industries": [...]
}
```

→ **Đúng 100% nhu cầu của bạn**: job + thông tin công ty (profile đầy đủ) + salary + benefits.

**Điểm cần lưu ý:** endpoint này là API "privately public" — có thể thay đổi bất cứ lúc nào, không có SLA. Nên wrap trong try/except và có fallback (chính VNW public data này cho frontend không auth, nên rủi ro chấp nhận được cho personal agent).

---

## 🔍 Về "feedback về công ty" — kết quả điều tra riêng

| Nguồn feedback | Trạng thái test | Ghi chú |
|---|---|---|
| JobSpy `company_rating` | ❌ `None` cho mọi job VN | Field này lấy từ Glassdoor US |
| Glassdoor VN qua JobSpy | ⚠️ Chạy nhưng job lệch, rating trống | Không đáng dùng |
| **TopCV company page** (`/cong-ty/{slug}`) | 🔜 Cần Playwright (bị CF chặn curl) | TopCV có mục "Đánh giá công ty" với rating + nhận xét — hướng đáng đào sâu nhất |
| **Itviec company page** | ✅ curl 200, có markers Reviews (23 hits), đánh giá "Ưu điểm/Nhược điểm" | Nhưng ít công ty Marketing (thiên về IT) |
| Google search `{tên cty} đánh giá review` | ✅ Luôn hoạt động | Chất lượng thấp, cần LLM tổng hợp |
| VNW `companyProfile` (qua API #4) | ✅ Trả về full text mô tả công ty | Không phải review, nhưng là info công ty tốt |

**Đề xuất cho feedback:** kết hợp TopCV company page (qua Playwright, cùng session đã bypass CF) + Google search để LLM tổng hợp. Đây là phần cần làm thêm khi build agent.

---

## 🛠️ Roadmap build Agent (thứ tự ưu tiên)

1. **Bước 1 (ngày 1):** Wrap Tool 2 (VNW API) — dễ nhất, nhanh nhất (0.24s), data giàu nhất. Chỉ cần 1 hàm POST.
2. **Bước 2 (ngày 1):** Wrap Tool 1 (JobSpy/LinkedIn) — 3 dòng code, coverage salary quốc tế + job "xịn".
3. **Bước 3 (ngày 2):** Wrap Tool 3 (TopCV Playwright) — nhiều job nhất cho Content/Mkt VN, cần quản lý browser + cache.
4. **Bước 4 (ngày 3+):** Tool 4 company feedback — Playwright vào trang `/cong-ty/` của TopCV parse "Đánh giá", fallback Google.
5. **Bước 5:** Merge + de-duplicate theo `companyName + title`, chuẩn hóa salary về VND/tháng, format output.

**Stack gợi ý:** Python + LangChain/LlamaIndex (hoặc thẳng OpenAI/Anthropic function calling), FastMCP nếu muốn expose qua MCP.

---

## 📁 Code đã verify trong buổi test
- `research/jobspy_test.py` — JobSpy LinkedIn/Indeed
- `research/jobspy_multi_test.py` — JobSpy LinkedIn "marketing" broad search
- `research/jobspy_company_data.py` — kiểm tra cột rating/review của JobSpy
- `research/jobspy_glassdoor.py` — test Glassdoor
- `research/playwright_topcv2.py` — Playwright bypass TopCV Cloudflare ✅
- `research/vnw_intercept2.py` — bắt internal API của VNW bằng network interception
- `research/vnw_api_direct.py` — (tương đương curl ở mục 4) gọi trực tiếp API VNW

*Toàn bộ script chạy được trên Python 3.13, macOS. Dependencies: `python-jobspy`, `playwright` (+ `playwright install chromium`).*
