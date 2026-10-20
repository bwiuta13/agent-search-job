"""Detail fetchers: hard-code selector từng nguồn để lấy JD đầy đủ từ trang chi tiết.

Đã điều tra cấu trúc thật 16/10/2026:
- ITviec: JD server-side sau <h2>Job description</h2>; skills sau "Your skills and
  experience"; perks trong "Why you'll love working here".
- CareerViet: JD trong phần "Mô tả công việc" (trang detail /viec-lam/...-vi.html,
  server render, cần gzip).
- LinkedIn: trang public /jobs/view/{id} (không cần login) có JD trong
  .show-more-less-html__markup. JobSpy fetch_description hay dính 429 rồi trả JD
  rỗng không báo lỗi -> tự fetch, giãn cách + retry khi 429.
- TopCV: cần Playwright (Cloudflare) - job detail /viec-lam/{slug}/{id}.html,
  JD trong .job-detail-info; dùng khi caller bật browser.
- VietnamWorks: search API chỉ trả JD đã cắt ~300 ký tự + "..."; trang detail
  (Next.js) có JD đầy đủ trong payload self.__next_f.push(...) (server render).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Optional

import httpx

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class JobDetail:
    description: str = ""
    requirements: str = ""
    benefits: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    salary_text: str = ""
    experience_text: str = ""
    job_level: str = ""
    company_name: str = ""


def _strip(html: str) -> str:
    # xuống dòng thô trong HTML chỉ là khoảng trắng (JD dán từ Word hay ngắt giữa câu);
    # dòng thật đến từ <br> / thẻ đóng khối bên dưới
    html = re.sub(r"\s+", " ", html.replace("\ufeff", ""))
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</(p|li|div|h\d)>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = unescape(html).replace("\xa0", " ")
    # mỗi dòng bỏ khoảng trắng đầu/cuối, gộp các dòng trống liên tiếp
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in html.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _section_after(html: str, heading_pattern: str, max_len: int = 6000) -> str:
    """Lấy text sau heading cho tới heading kế tiếp (h2/h3) hoặc hết block."""
    m = re.search(heading_pattern, html, re.I)
    if not m:
        return ""
    rest = html[m.end():]
    # cắt ở heading kế tiếp
    stop = re.search(r"<h[23][^>]*>", rest)
    chunk = rest[: stop.start()] if stop else rest[:max_len * 3]
    return _strip(chunk)[:max_len]


class ITviecDetailFetcher:
    """JD ITviec: server render, có 'Job description' + 'Your skills and experience'."""

    async def fetch(self, url: str, client: httpx.AsyncClient) -> Optional[JobDetail]:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        h = resp.text
        d = JobDetail()
        d.description = _section_after(h, r"<h2[^>]*>\s*Job description\s*</h2>")
        d.requirements = _section_after(h, r"<h2[^>]*>\s*Your skills and experience\s*</h2>")
        benefits_raw = _section_after(h, r"<h2[^>]*>\s*Why you&#39;ll love working here\s*</h2>")
        if benefits_raw:
            d.benefits = [ln.strip()[:120] for ln in benefits_raw.split("\n")
                          if ln.strip()][:10]
        m = re.search(r"(\$\s?\d[\d,.]*(?:\s*[-–+]\s*\$?\d[\d,.]*)?\s*(?:USD|M VND|VND))", h)
        if m:
            d.salary_text = m.group(1).strip()
        m = re.search(
            r"Kinh nghiệm(?: làm việc)?\s*[</\\a-z0-9>;\"']*?\s*(\d+\s*[-–]?\s*\d*)\s*(?:năm|years?)",
            h,
        )
        if m:
            d.experience_text = m.group(1)
        m = re.search(r"at\s+([^|<]{3,60})\s*\|\s*ITviec", h)
        if m:
            d.company_name = _strip(m.group(1))
        return d if (d.description or d.requirements) else None


class CareerVietDetailFetcher:
    """JD CareerViet: trang detail server render, mỗi mục là <div class="detail-row"><h2 class="detail-title">…</h2>
    (verify 20/10/2026). Neo vào thẻ h2: JSON-LD đầu trang cũng có chữ "Requirements" (experienceRequirements)
    nên tìm chữ trần sẽ lấy nhầm JSON. Lương, kinh nghiệm ở ô tóm tắt <strong>(<em> icon)Lương</strong><p>…</p>."""

    async def fetch(self, url: str, client: httpx.AsyncClient) -> Optional[JobDetail]:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        h = resp.text
        d = JobDetail()
        d.description = _section_after(h, r"<h2[^>]*>\s*(?:Mô tả công việc|Job Description)\s*</h2>")
        d.requirements = _section_after(
            h, r"<h2[^>]*>\s*(?:Yêu cầu công việc|Yêu cầu ứng viên|Job Requirements?)\s*</h2>")
        benefits_raw = _section_after(h, r"<h2[^>]*>\s*(?:Phúc lợi|Quyền lợi|Benefits)\s*</h2>")
        if benefits_raw:
            d.benefits = [ln.strip()[:120] for ln in benefits_raw.split("\n")
                          if ln.strip()][:10]
        m = re.search(r"<strong>(?:\s*<em[^>]*>\s*</em>)?\s*Kinh nghiệm\s*</strong>\s*<p>(.*?)</p>", h, re.S | re.I)
        if m:
            exp = re.search(r"\d+(?:\s*[-–]\s*\d+)?", _strip(m.group(1)))
            d.experience_text = exp.group(0) if exp else ""
        m = re.search(r"<strong>(?:\s*<em[^>]*>\s*</em>)?\s*Lương\s*</strong>\s*<p>(?:Lương:\s*)?([^<]*)</p>", h, re.I)
        if m:
            d.salary_text = m.group(1).strip()
        return d if d.description else None


class VietnamWorksDetailFetcher:
    """JD VietnamWorks đầy đủ từ payload RSC của trang detail ...-{jobId}-jv."""

    async def fetch(self, url: str, client: httpx.AsyncClient) -> Optional[JobDetail]:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        try:
            flat = "".join(json.loads(c) for c in re.findall(
                r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', resp.text, re.S))
        except json.JSONDecodeError:
            return None
        # JD dài được tách thành dòng text riêng "<id>:T<số byte hex>,<html>" (không có
        # \n kết thúc, dính liền dòng trước) và field chỉ giữ tham chiếu "$<id>" ->
        # đọc tuần tự từng dòng để lấy đúng các dòng T
        texts: dict[str, str] = {}
        raw, pos = flat.encode(), 0
        try:
            while pos < len(raw):
                colon = raw.index(b":", pos)
                if raw[colon + 1:colon + 2] == b"T":
                    comma = raw.index(b",", colon)
                    end = comma + 1 + int(raw[colon + 2:comma], 16)
                    texts[raw[pos:colon].decode()] = raw[comma + 1:end].decode("utf-8", "ignore")
                    pos = end
                else:
                    nl = raw.find(b"\n", colon)
                    pos = len(raw) if nl < 0 else nl + 1
        except ValueError:
            pass  # payload lạ -> dùng những dòng T đã đọc được
        found = {}
        for key in ("jobDescription", "jobRequirement"):
            i = flat.find(f'"{key}":"')
            if i < 0:
                continue
            try:
                value, _ = json.JSONDecoder().raw_decode(flat, i + len(key) + 3)
            except json.JSONDecodeError:
                continue
            ref = re.fullmatch(r"\$([0-9a-f]+)", value) if isinstance(value, str) else None
            if ref:
                value = texts.get(ref.group(1), "")
            found[key] = _strip(value) if isinstance(value, str) else ""
        d = JobDetail(description=found.get("jobDescription", ""),
                      requirements=found.get("jobRequirement", ""))
        return d if d.description else None


class LinkedInDetailFetcher:
    """JD LinkedIn từ trang public /jobs/view/{id}; 429 -> chờ rồi thử lại (tối đa 3 lần)."""

    async def fetch(self, url: str, client: httpx.AsyncClient) -> Optional[JobDetail]:
        for attempt in range(3):
            resp = await client.get(url)
            if resp.status_code != 429:
                break
            log.warning("LinkedIn 429 (bị giới hạn tốc độ) %s — chờ %ds rồi thử lại",
                        url.rsplit("/", 1)[-1], 10 * (attempt + 1))
            await asyncio.sleep(10 * (attempt + 1))
        # bị đẩy sang authwall/login thì không còn là trang job
        if resp.status_code != 200 or "/jobs/view/" not in str(resp.url):
            return None
        m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</section>', resp.text, re.S)
        if not m:
            return None
        d = JobDetail(description=_strip(m.group(1))[:6000])
        return d if d.description else None


class TopCVDetailFetcher:
    """JD TopCV: Cloudflare -> cần Playwright. Chỉ chạy khi caller bật browser.

    Job detail: /viec-lam/{slug}/{id}.html - JD trong khối class job-detail.
    """

    async def fetch(self, url: str) -> Optional[JobDetail]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled",
                          "--no-sandbox"],
                )
                ctx = await browser.new_context(
                    user_agent=UA, viewport={"width": 1440, "height": 900},
                    locale="vi-VN",
                )
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                for sel in ("button:has-text('Chấp nhận tất cả')",):
                    try:
                        await page.click(sel, timeout=3000)
                        break
                    except Exception:  # noqa: BLE001
                        pass
                await page.wait_for_timeout(3000)
                html = await page.content()
                await browser.close()
        except Exception:  # noqa: BLE001 - browser lỗi -> None
            return None
        d = JobDetail()
        # Neo vào thẻ <h2> của khối JD: menu "Cẩm nang" của TopCV có bài blog
        # title="...Bảng mô tả công việc nhân viên bán hàng" nằm trước JD, khớp
        # chữ trần sẽ lấy nhầm menu + blog thay cho JD.
        # tin tiếng Anh dùng Job Description / Candidate Requirements / Benefits
        d.description = _section_after(
            html, r"<h2[^>]*>\s*(?:Mô tả công việc|Job Description)\s*</h2>")
        d.requirements = _section_after(
            html, r"<h2[^>]*>\s*(?:Yêu cầu ứng viên|Candidate Requirements?)\s*</h2>")
        benefits_raw = _section_after(
            html, r"<h2[^>]*>\s*(?:Quyền lợi[^<]*|Benefits)\s*</h2>")
        if benefits_raw:
            d.benefits = [ln.strip()[:120] for ln in benefits_raw.split("\n")
                          if ln.strip()][:10]
        m = re.search(
            r"(?i)kinh nghiệm\s*[</\\a-z0-9>;\"' ]*?\s*(\d+\s*(?:[-–]\s*\d+)?)\s*(?:năm)",
            html,
        )
        if m:
            d.experience_text = m.group(1)
        # cấp bậc: TopCV có box "Cấp bậc: Nhân viên/Trưởng nhóm..."
        m = re.search(r"(?i)cấp bậc\s*[</\\a-z0-9>;\"' ]*?\s*(nhân viên|trưởng nhóm|giám sát|trưởng phòng|giám đốc|thực tập sinh|fresher|junior|senior|middle|manager|intern)", html)
        if m:
            d.job_level = m.group(1).strip()
        return d if d.description else None


async def fetch_detail(source: str, url: str,
                       timeout: float = 20.0) -> Optional[JobDetail]:
    """Dispatch fetcher theo nguồn. Trả None nếu nguồn không cần/không fetch được."""
    if source == "itviec":
        async with httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": UA}, follow_redirects=True,
        ) as client:
            return await ITviecDetailFetcher().fetch(url, client)
    if source == "careerviet":
        async with httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": UA,
                                      "Accept-Encoding": "gzip"},
            follow_redirects=True,
        ) as client:
            return await CareerVietDetailFetcher().fetch(url, client)
    if source == "topcv":
        return await TopCVDetailFetcher().fetch(url)
    if source in ("vietnamworks", "linkedin"):
        fetcher = (VietnamWorksDetailFetcher() if source == "vietnamworks"
                   else LinkedInDetailFetcher())
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": UA, "Accept-Language": "vi,en;q=0.8"},
        ) as client:
            return await fetcher.fetch(url, client)
    if source == "topdev":
        from .topdev import fetch_topdev_detail
        d = await fetch_topdev_detail(url, timeout=timeout)
        if not d:
            return None
        return JobDetail(description=d.get("description", ""),
                         requirements=d.get("requirements", ""),
                         benefits=d.get("benefits", []))
    return None
