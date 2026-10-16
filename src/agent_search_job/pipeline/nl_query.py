"""LLM natural-language query parser: câu tiếng Việt tự nhiên -> SearchFilters.

Ví dụ:
  "tìm việc content tiktok lương trên 15 triệu ở quận 1"
    -> query="content tiktok", salary_min_vnd=15000000, districts=["Quận 1"]
  "việc social media part-time có thể làm remote, mới đăng 7 ngày"
    -> role_clusters=["social_media"], work_modes=["remote","hybrid"], posted_within_days=7

LLM trả JSON; mọi trường bị sanitize về enum hợp lệ của contracts. LLM lỗi/thiếu
key -> fallback: dùng nguyên câu làm query text (search thường vẫn chạy được).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from ..mcp.contracts import (
    MAX_POSTED_WITHIN_DAYS,
    VALID_JOB_LEVELS,
    SearchFilters,
    VALID_ROLE_CLUSTERS,
    VALID_WORK_MODES,
    VALID_SOURCES,
)
from .llm_enrich import LLMConfig

SYSTEM_PARSE = """Bạn chuyển câu tìm việc tiếng Việt (tự nhiên) thành JSON filter cho hệ thống tìm việc Content/Marketing TP.HCM.
Chỉ trả JSON đúng schema, không thêm chữ nào:
{"query": "từ khóa search còn lại sau khi tách filter", "role_clusters": [], "districts": [],
 "job_levels": [], "company_query": null,
 "salary_min_vnd": null, "work_modes": [], "posted_within_days": 14, "portfolio_required": null, "source": "all"}
Quy tắc:
- role_clusters chỉ nhận: content_marketing, copywriter_seo, social_media, communications_pr, video_production, multimedia_design, creative_production
- job_levels chỉ nhận: intern, fresher, junior, middle, senior, lead, manager. "thực tập"=intern; "chưa có kinh nghiệm/fresher"=fresher; "junior/1-2 năm"=junior; "middle/2-4 năm"=middle; "senior/4+ năm"=senior; "trưởng nhóm"=lead; "trưởng phòng/quản lý"=manager
- company_query: tên công ty nếu câu nhắc rõ ("việc ở FPT", "công ty Jamu")
- work_modes chỉ nhận: onsite, hybrid, remote. "làm từ xa"=remote; "linh hoạt/kết hợp"=hybrid
- districts: tên quận/huyện TP.HCM (Quận 1, Quận 3, Bình Thạnh, Gò Vấp, Thủ Đức...) nếu câu nói rõ khu vực; [] nếu không
- salary_min_vnd: số VND/tháng ("trên 15 triệu"=15000000, "15tr+"=15000000); null nếu không nói lương
- posted_within_days: "mới đăng hôm qua"=1, "tuần này"=7, "2 tuần"=14, "tháng này"=30 (tối đa 60)
- portfolio_required: true nếu câu nhắc "portfolio/hồ sơ năng lực"
- source: "topcv" nếu câu nói rõ chỉ TopCV, "all" mặc định
- query: từ khóa còn lại (vd "content tiktok", "video editor", "seo") — bỏ hết số lương/quận/từ chỉ thời gian
Suy nghĩ ngắn rồi trả JSON ở dòng cuối."""


@dataclass
class ParsedQuery:
    filters: SearchFilters
    raw_text: str
    llm_used: bool = False
    note: str = ""


# Fallback rule-based: bắt các pattern phổ biến khi LLM không khả dụng
_QUICK_PATTERNS = [
    (re.compile(r"trên\s+(\d+[\d.,]*)\s*(?:triệu|tr)\b", re.I), "salary"),
    (re.compile(r"(\d+[\d.,]*)\s*(?:triệu|tr)\s*\+", re.I), "salary"),
]


def rule_parse(text: str) -> SearchFilters:
    """Parse thô không LLM: lương + vài keyword role."""
    f = SearchFilters(query=text.strip()[:200])
    for pat, kind in _QUICK_PATTERNS:
        m = pat.search(text)
        if m:
            v = m.group(1).replace(".", "").replace(",", ".")
            try:
                f.salary_min_vnd = int(float(v) * 1_000_000)
            except ValueError:
                pass
            break
    low = text.lower()
    role_map = {
        "tiktok": "social_media", "social": "social_media",
        "video": "video_production", "dựng": "video_production",
        "seo": "copywriter_seo", "copywriter": "copywriter_seo",
        "pr": "communications_pr", "truyền thông": "communications_pr",
        "design": "multimedia_design", "thiết kế": "multimedia_design",
    }
    roles = {v for k, v in role_map.items() if k in low}
    if roles:
        f.role_clusters = sorted(roles)
    if "remote" in low or "từ xa" in low:
        f.work_modes = ["remote"]
    return f


class NLQueryParser:
    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig.from_env()

    async def parse(self, text: str) -> ParsedQuery:
        text = (text or "").strip()
        if not text:
            return ParsedQuery(filters=SearchFilters(), raw_text="", note="câu rỗng")
        if not self.config.ok:
            f = rule_parse(text).validate()
            return ParsedQuery(
                filters=f, raw_text=text, llm_used=False,
                note="LLM chưa cấu hình - dùng rule parser (lương/role/remote)",
            )
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.config.model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PARSE},
                            {"role": "user", "content": text},
                        ],
                        "max_tokens": 1500,
                        "temperature": 0.1,
                    },
                )
                resp.raise_for_status()
                msg = resp.json()["choices"][0]["message"]
                content = msg.get("content") or ""
        except Exception:  # noqa: BLE001 - LLM lỗi -> fallback rule
            f = rule_parse(text).validate()
            return ParsedQuery(
                filters=f, raw_text=text, llm_used=False,
                note="LLM lỗi - fallback rule parser",
            )
        d = self._extract_json(content)
        if d is None:
            f = rule_parse(text).validate()
            return ParsedQuery(
                filters=f, raw_text=text, llm_used=False,
                note="LLM trả không parse được JSON - fallback rule parser",
            )
        f = self._to_filters(d, text)
        return ParsedQuery(filters=f, raw_text=text, llm_used=True)

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _to_filters(d: dict, original: str) -> SearchFilters:
        # sanitize từng field về enum/giới hạn hợp lệ
        query = d.get("query") or original
        query = str(query).strip()[:200] or None
        roles = [r for r in (d.get("role_clusters") or [])
                 if r in VALID_ROLE_CLUSTERS]
        modes = [m for m in (d.get("work_modes") or [])
                 if m in VALID_WORK_MODES]
        levels = [l for l in (d.get("job_levels") or [])
                  if l in VALID_JOB_LEVELS]
        company_q = str(d.get("company_query") or "").strip()[:200] or None
        from ..domain.normalize import parse_district
        districts = []
        for x in (d.get("districts") or [])[:10]:
            name = str(x).strip()
            if name and parse_district(name):  # chỉ giữ tên quận/huyện TP.HCM hợp lệ
                districts.append(parse_district(name))
            elif name:
                # không phải tên quận HCM -> bỏ (tránh lọc lệch khỏi thị trường mục tiêu)
                continue
        try:
            salary = int(d.get("salary_min_vnd") or 0) or None
            if salary is not None and (salary < 0 or salary > 999_000_000):
                salary = None
        except (TypeError, ValueError):
            salary = None
        try:
            days = int(d.get("posted_within_days") or 14)
            days = max(1, min(MAX_POSTED_WITHIN_DAYS, days))
        except (TypeError, ValueError):
            days = 14
        source = d.get("source") if d.get("source") in VALID_SOURCES else "all"
        portfolio = d.get("portfolio_required")
        if portfolio not in (True, False):
            portfolio = None
        try:
            page_size = int(d.get("page_size") or 10)
            page_size = max(1, min(25, page_size))
        except (TypeError, ValueError):
            page_size = 10
        return SearchFilters(
            query=query, role_clusters=roles or None, districts=districts or None,
            salary_min_vnd=salary, work_modes=modes or None,
            job_levels=levels or None, company_query=company_q,
            posted_within_days=days, portfolio_required=portfolio,
            source=source, page_size=page_size,
        ).validate()
