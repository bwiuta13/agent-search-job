"""Detail fetchers: hard-code selector từng nguồn để lấy JD đầy đủ từ trang chi tiết.

Đã điều tra cấu trúc thật 16/10/2026:
- ITviec: JD server-side sau <h2>Job description</h2>; skills sau "Your skills and
  experience"; perks trong "Why you'll love working here".
- CareerViet: JD trong phần "Mô tả công việc" (trang detail /viec-lam/...-vi.html,
  server render, cần gzip).
- LinkedIn (JobSpy): fetch description qua linkedin_fetch_description=True.
- TopCV: cần Playwright (Cloudflare) - job detail /viec-lam/{slug}/{id}.html,
  JD trong .job-detail-info; dùng khi caller bật browser.
- VietnamWorks: internal API đã trả JD đầy đủ ngay trong search (không cần fetch).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from typing import Optional

import httpx

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
    """JD CareerViet: trang detail server render có 'Mô tả công việc'."""

    async def fetch(self, url: str, client: httpx.AsyncClient) -> Optional[JobDetail]:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        h = resp.text
        d = JobDetail()
        d.description = _section_after(h, r"(?:Mô tả công việc|Job Description)")
        d.requirements = _section_after(h, r"(?:Yêu cầu ứng viên|Requirements)")
        benefits_raw = _section_after(h, r"(?:Quyền lợi|Benefits)")
        if benefits_raw:
            d.benefits = [ln.strip()[:120] for ln in benefits_raw.split("\n")
                          if ln.strip()][:10]
        m = re.search(
            r"(?i)kinh nghiệm\s*[</\\a-z0-9>;\"']*?\s*(\d+\s*(?:[-–]\s*\d+)?)\s*(?:năm)",
            h,
        )
        if m:
            d.experience_text = m.group(1)
        m = re.search(r"(?i)(\d+[\d.,]*\s*(?:triệu|tri)\s*(?:[-–]\s*\d+[\d.,]*\s*(?:triệu|tri))?)", h)
        if m:
            d.salary_text = m.group(1)
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
    if source == "topdev":
        from .topdev import fetch_topdev_detail
        d = await fetch_topdev_detail(url, timeout=timeout)
        if not d:
            return None
        return JobDetail(description=d.get("description", ""),
                         requirements=d.get("requirements", ""),
                         benefits=d.get("benefits", []))
    # vietnamworks: JD có sẵn từ search API; linkedin: dùng JobSpy
    return None
