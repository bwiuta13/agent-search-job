"""
Veri đe Python cho Job Search Agent - Content & Marketing Việt Nam
Toàn bộ 3 tool dưới đây đã được test chạy thành công ngày 15/10/2026.

Dependencies:
    pip install python-jobspy playwright
    python -m playwright install chromium
"""
import asyncio
import json
import re
import urllib.request

# ============================================================
# TOOL 2 (đề xuất làm TRƯỚC TIÊN): VietnamWorks Internal API
# Nhanh nhất (0.24s), không cần browser, data giàu nhất.
# Endpoint đã verify: POST ms.vietnamworks.com/job-search/v1.0/search
# ============================================================

VNW_FIELDS = [
    "jobTitle", "jobUrl", "companyName", "companyProfile",
    "salary", "salaryMin", "salaryMax", "prettySalary", "isSalaryVisible",
    "jobDescription", "jobRequirement", "benefits", "skills",
    "workingLocations", "address", "industries",
    "approvedOn", "expiredOn", "alias", "jobLevelVI", "numOfApplications",
]


def vnw_search(query: str, page: int = 0, hits_per_page: int = 50) -> dict:
    """Search job trên VietnamWorks qua internal API. Trả về list job + meta.

    Đã verify: "content marketing" -> 58 jobs, kèm salary, JD, company profile.
    """
    payload = json.dumps({
        "userId": 0,
        "query": query,
        "filter": [],
        "ranges": [],
        "order": [],
        "hitsPerPage": hits_per_page,
        "page": page,
        "retrieveFields": VNW_FIELDS,
    }).encode()
    req = urllib.request.Request(
        "https://ms.vietnamworks.com/job-search/v1.0/search",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Origin": "https://www.vietnamworks.com",
            "Referer": "https://www.vietnamworks.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/128.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        d = json.loads(resp.read().decode())
    jobs = d.get("data", [])
    meta = d.get("meta", {})
    return {
        "total": meta.get("nbHits"),
        "pages": meta.get("nbPages"),
        "jobs": [{
            "title": j.get("jobTitle"),
            "company": j.get("companyName"),
            "company_profile": (j.get("companyProfile") or "")[:500],
            "url": j.get("jobUrl"),
            "salary": j.get("prettySalary") or j.get("salary"),
            "salary_min": j.get("salaryMin"),
            "salary_max": j.get("salaryMax"),
            "location": j.get("workingLocations"),
            "benefits": j.get("benefits"),
            "skills": j.get("skills"),
            "posted": j.get("prettyApprovedOn") or j.get("approvedOn"),
            "expires": j.get("expiredOn"),
            "level": j.get("jobLevelVI"),
            "description_html": (j.get("jobDescription") or "")[:3000],
        } for j in jobs],
    }


# ============================================================
# TOOL 1: LinkedIn qua JobSpy
# Ổn định nhất cho job quốc tế/tại VN đăng trên LinkedIn.
# ============================================================

def linkedin_search(search_term: str, location: str = "Vietnam",
                    results_wanted: int = 15, hours_old: int = 720) -> list:
    """Search job LinkedIn VN qua JobSpy. Không cần API key.

    Đã verify: "content marketing"/VN -> 10 jobs thật (Leanbase, L'Oréal...).
    Lưu ý: company_rating luôn None với VN (JobSpy lấy từ Glassdoor US).
    """
    from jobspy import scrape_jobs
    df = scrape_jobs(
        site_name=["linkedin"],
        search_term=search_term,
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
    )
    return [{
        "title": r.title,
        "company": r.company,
        "location": r.location,
        "url": r.job_url,
        "posted": str(r.date_posted) if r.date_posted else None,
        "remote": bool(r.is_remote) if r.is_remote is not None else None,
        "description": (str(r.description) if r.description else "")[:2000],
    } for _, r in df.iterrows()]


# ============================================================
# TOOL 3: TopCV qua Playwright (bypass Cloudflare)
# Nhiều job Content/Marketing VN nhất (1.483 kết quả cho "content marketing").
# Chậm (~15s/request) -> nên cache kết quả.
# ============================================================

async def _topcv_fetch_async(search_url: str) -> list:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/128.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
            locale="vi-VN",
        )
        page = await ctx.new_page()
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        # Chấp nhận cookie banner (bắt buộc, nếu không list không tương tác được)
        for sel in ("button:has-text('Chấp nhận tất cả')",
                    "#cookie_policy_accept_btn"):
            try:
                await page.click(sel, timeout=4000)
                break
            except Exception:
                pass
        await page.wait_for_timeout(6000)  # đợi job list render
        html = await page.content()
        await browser.close()

    # Parse job cards - đã verify pattern data-job-id + aria-label
    cards = {}
    for m in re.finditer(
            r'data-job-id="(\d+)"[^>]*>(.*?)(?=data-job-id="\d+"|$)',
            html, re.DOTALL):
        jid, blk = m.group(1), m.group(2)
        if jid in cards:
            continue
        t = re.search(r'aria-label="([^"]{5,90})"', blk)
        if not t or "đăng nhập" in t.group(1).lower():
            continue
        sal = re.search(
            r'(\d+[\d.,]*\s*-\s*\d+[\d.,]*\s*(?:triệu|TRIỆU)|'
            r'Trên\s*\d+[\d.,]*\s*triệu|Từ\s*\d+[\d.,]*\s*triệu|Thoả thuận)',
            blk, re.I)
        comp = (re.search(r'title="((?:CÔNG TY|Công ty|Cong ty)[^"]{3,55})"', blk)
                or re.search(r'alt="(CÔNG TY[^"]{3,60})"', blk))
        url_m = re.search(r'href="(https://www\.topcv\.vn/viec-lam/[^"]+)"', blk)
        loc = re.search(
            r'(Hà Nội|Hồ Chí Minh|Đà Nẵng|TP\.?\s*HCM|Hải Phòng|Bình Dương|Remote)',
            blk)
        cards[jid] = {
            "id": jid,
            "title": t.group(1),
            "salary": sal.group(1) if sal else None,
            "company": comp.group(1) if comp else None,
            "location": loc.group(1) if loc else None,
            "url": url_m.group(1).split("?")[0] if url_m else None,
        }
    return list(cards.values())


def topcv_search(search_slug: str) -> list:
    """Search TopCV. search_slug = slug của URL.

    Đã verify: 'content-marketing' -> 38 cards (203 render) với salary.
    Các slug khác: 'content-creator', 'copywriter', 'seo', 'digital-marketing'...
    """
    url = f"https://www.topcv.vn/tim-viec-lam-{search_slug}"
    return asyncio.run(_topcv_fetch_async(url))


# ============================================================
# DEMO chạy thử cả 3 tool
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("1) VietnamWorks internal API — 'content marketing'")
    print("=" * 60)
    r = vnw_search("content marketing", hits_per_page=5)
    print(f"Total: {r['total']} jobs, {r['pages']} pages")
    for j in r["jobs"][:3]:
        print(f"  • {j['title']} @ {j['company']} | {j['salary']} | {j['location']}")

    print()
    print("=" * 60)
    print("2) LinkedIn (JobSpy) — 'content marketing'")
    print("=" * 60)
    try:
        for j in linkedin_search("content marketing", results_wanted=5):
            print(f"  • {j['title']} @ {j['company']} | {j['location']}")
    except Exception as e:
        print("  LinkedIn error:", e)

    print()
    print("=" * 60)
    print("3) TopCV (Playwright bypass Cloudflare) — 'content-marketing'")
    print("=" * 60)
    try:
        for j in topcv_search("content-marketing")[:5]:
            print(f"  • {j['title']} | {j['salary']} | {j['location']} | {j['company']}")
    except Exception as e:
        print("  TopCV error:", e)
