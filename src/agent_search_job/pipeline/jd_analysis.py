"""LLM JD analysis: đọc mô tả JD dài -> tóm tắt có cấu trúc hiển thị trên web/MCP.

Chạy theo yêu cầu (lazy) khi người dùng mở trang chi tiết job:
- cache trong bảng llm_enrichment (kind='jd_analysis') nên lần sau không tốn token
- JD ngắn (<80 ký tự) hoặc thiếu -> trả None, UI hiện JD thô như cũ
- LLM lỗi/không cấu hình -> None, không chặn trang

Output schema (đã sanitize độ dài):
{"summary": str, "key_responsibilities": [str], "requirements": [str],
 "nice_to_have": [str], "salary_hint": str, "perks": [str], "red_flags": [str]}
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .llm_enrich import LLMConfig

SYSTEM_JD = """Bạn là chuyên gia tuyển dụng Content/Marketing tại Việt Nam, đọc tin tuyển dụng (JD) và trả về phân tích JSON.
Chỉ trả JSON đúng schema, không thêm chữ nào:
{"summary": "tóm tắt 2-3 câu vai trò này làm gì, cho ai",
 "key_responsibilities": ["3-5 trách nhiệm chính, mỗi mục một câu ngắn"],
 "requirements": ["yêu cầu bắt buộc (kinh nghiệm, kỹ năng, tool)"],
 "nice_to_have": ["yêu cầu ưu tiên nếu có"],
 "salary_hint": "lương nếu JD ghi rõ, không thì 'JD không ghi'",
 "perks": ["quyền lợi nổi bật"],
 "red_flags": ["điểm cần lưu ý (lương không rõ, OT nhiều, yêu cầu đa nhiệm quá mức...)"]}
Quy tắc:
- Viết tiếng Việt tự nhiên, ngắn gọn, giữ thuật ngữ gốc (TikTok, CapCut, KPI...)
- Không suy đoán thông tin JD không có; red_flags để [] nếu không có gì đáng ngờ
- Mỗi mảng tối đa 5 phần tử, mỗi phần tử dưới 120 ký tự
Suy nghĩ ngắn rồi trả JSON ở dòng cuối."""


@dataclass
class JDAnalysis:
    summary: str = ""
    key_responsibilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    salary_hint: str = ""
    perks: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "key_responsibilities": self.key_responsibilities,
            "requirements": self.requirements,
            "nice_to_have": self.nice_to_have,
            "salary_hint": self.salary_hint,
            "perks": self.perks,
            "red_flags": self.red_flags,
        }


def _clip_list(v, max_items: int = 5, max_len: int = 120) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip()[:max_len] for x in v if str(x).strip()][:max_items]


class JDAnalyzer:
    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig.from_env()

    async def analyze(self, job: dict) -> Optional[JDAnalysis]:
        jd = (job.get("description") or "").strip()
        req = (job.get("requirements") or "").strip()
        blob = f"Tiêu đề: {job.get('title', '')}\nCông ty: {job.get('company_name', '')}\n"
        if job.get("salary_text"):
            blob += f"Lương ghi trong tin: {job['salary_text']}\n"
        blob += f"\nMô tả công việc:\n{jd[:4000]}\n"
        if req:
            blob += f"\nYêu cầu:\n{req[:1500]}\n"
        if len(jd) < 80 and len(req) < 80:
            return None  # JD quá ngắn, không có gì để phân tích
        if not self.config.ok:
            return None
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
                            {"role": "system", "content": SYSTEM_JD},
                            {"role": "user", "content": blob},
                        ],
                        "max_tokens": 2500,
                        "temperature": 0.2,
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"].get("content") or ""
        except Exception:  # noqa: BLE001 - LLM lỗi -> None, trang vẫn render JD thô
            return None
        d = self._extract_json(content)
        if not d:
            return None
        return JDAnalysis(
            summary=str(d.get("summary") or "").strip()[:600],
            key_responsibilities=_clip_list(d.get("key_responsibilities")),
            requirements=_clip_list(d.get("requirements")),
            nice_to_have=_clip_list(d.get("nice_to_have")),
            salary_hint=str(d.get("salary_hint") or "").strip()[:120],
            perks=_clip_list(d.get("perks")),
            red_flags=_clip_list(d.get("red_flags"), max_items=3),
        )

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
