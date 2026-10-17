"""LLM enrichment theo docs/research-website.md mục 6:

"Rule-based extraction nên đi trước. LLM chỉ dùng cho field khó, chạy sau ingest,
cache kết quả và lưu model/prompt version."

LLM dùng cho 2 việc:
1. Extract field khó từ JD: platforms, content_types, creative_tools,
   on_camera_required, client_facing, agency_or_inhouse, kpi_types, languages.
2. Scoring relevance của job so với nhóm mục tiêu (Content & Multimedia TP.HCM).

Cấu hình qua env: AI_PROVIDER, AI_TEXT_MODEL, OPENAI_API_KEY,
OPENAI_BASE_URL, OPENAI_REQUEST_TIMEOUT_SECONDS.

Cache trong bảng llm_enrichment (model+prompt_version+job_id unique) để
không gọi lại job đã enrich. Lỗi LLM/không cấu hình -> trả None, không sập pipeline.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

PROMPT_VERSION = "v1"

SYSTEM_EXTRACT = """Bạn là trợ lý trích xuất thông tin từ tin tuyển dụng Content/Marketing tại Việt Nam.
Trích xuất các trường sau từ JD. Với mỗi trường: chỉ điền giá trị có text hỗ trợ rõ, không suy đoán.
Trả về JSON ĐÚNG theo schema, không thêm chữ nào khác:
{"platforms": [], "content_types": [], "creative_tools": [], "on_camera_required": null,
 "client_facing": null, "agency_or_inhouse": null, "kpi_types": [], "languages": []}
Giá trị hợp lệ:
- platforms: ["tiktok","facebook","instagram","youtube","website","email","linkedin"]
- content_types: ["short_video","long_form","seo_article","social_post","livestream","event","internal_communication"]
- creative_tools: ["capcut","canva","premiere","after_effects","photoshop","illustrator","figma","da_vinci","final_cut"]
- on_camera_required / client_facing / agency_or_inhouse: true/false/null (null nếu JD không nói)
- kpi_types: mảng ngắn các KPI JD nêu (vd ["views","engagement","leads"])
- languages: ["vi","en"] hoặc []
Suy nghĩ ngắn gọn rồi trả lời JSON ngay ở dòng cuối."""

SYSTEM_SCORE = """Bạn chấm điểm độ phù hợp của tin tuyển dụng với nhóm mục tiêu:
ứng viên Content / Social Media / Brand Marketing / Truyền thông tại TP.HCM
(content marketing, content creator, copywriter, social media, marketing executive, brand marketing, creative).
Trả JSON đúng: {"relevance": <0.0-1.0>, "reason": "<một câu ngắn tiếng Việt>"}
- 1.0: đúng nhóm role trên (content/social/marketing/comms/creative), làm tại TP.HCM
- 0.5: có liên quan marketing/content nhưng lệch nhóm chính hoặc thiếu địa điểm
- 0.0: hoàn toàn lệch (telesales, kế toán, IT thuần, thuần video editor/thiết kế đồ họa...)
Suy nghĩ ngắn gọn rồi trả lời JSON ngay ở dòng cuối."""


@dataclass
class LLMConfig:
    provider: str = "openai"
    model: str = "z-ai/glm-5.3"
    api_key: str = ""
    base_url: str = "https://tokenplan.api.greennode.ai/v1"
    timeout: int = 90

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            provider=os.environ.get("AI_PROVIDER", "openai"),
            model=os.environ.get("AI_TEXT_MODEL", "z-ai/glm-5.3"),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            base_url=os.environ.get("OPENAI_BASE_URL",
                                    "https://tokenplan.api.greennode.ai/v1"),
            timeout=int(os.environ.get("OPENAI_REQUEST_TIMEOUT_SECONDS", "90")),
        )

    @property
    def ok(self) -> bool:
        return bool(self.api_key)


@dataclass
class Extraction:
    platforms: list[str] = field(default_factory=list)
    content_types: list[str] = field(default_factory=list)
    creative_tools: list[str] = field(default_factory=list)
    on_camera_required: Optional[bool] = None
    client_facing: Optional[bool] = None
    agency_or_inhouse: Optional[str] = None  # "agency" | "inhouse" | None
    kpi_types: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)


@dataclass
class RelevanceScore:
    relevance: float = 0.0
    reason: str = ""


class LLMEnricher:
    """Gọi LLM theo cấu hình env. Mọi lỗi -> None (pipeline vẫn chạy)."""

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig.from_env()

    async def _chat(self, system: str, user: str, max_tokens: int = 2000) -> Optional[str]:
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
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "max_tokens": max_tokens,
                        "temperature": 0.1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            msg = data["choices"][0]["message"]
            # model có reasoning_content riêng; content mới là câu trả lời
            return msg.get("content") or None
        except Exception:  # noqa: BLE001 - LLM lỗi thì bỏ qua field khó
            return None

    @staticmethod
    def _parse_json(text: Optional[str]) -> Optional[dict]:
        if not text:
            return None
        # cắt khối JSON nếu model trả kèm chữ
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    async def extract(self, title: str, description: str,
                      requirements: str = "") -> Optional[Extraction]:
        jd = f"Tiêu đề: {title}\n\nMô tả: {description[:2500]}\n\nYêu cầu: {requirements[:1200]}"
        text = await self._chat(SYSTEM_EXTRACT, jd, max_tokens=2500)
        d = self._parse_json(text)
        if not d:
            return None
        # sanitize: chỉ giữ giá trị enum hợp lệ
        valid_p = {"tiktok", "facebook", "instagram", "youtube", "website",
                   "email", "linkedin"}
        valid_ct = {"short_video", "long_form", "seo_article", "social_post",
                    "livestream", "event", "internal_communication"}
        valid_t = {"capcut", "canva", "premiere", "after_effects", "photoshop",
                   "illustrator", "figma", "da_vinci", "final_cut"}
        ext = Extraction(
            platforms=[p for p in (d.get("platforms") or []) if p in valid_p],
            content_types=[c for c in (d.get("content_types") or []) if c in valid_ct],
            creative_tools=[t for t in (d.get("creative_tools") or []) if t in valid_t],
            on_camera_required=_to_bool(d.get("on_camera_required")),
            client_facing=_to_bool(d.get("client_facing")),
            agency_or_inhouse=(
                d.get("agency_or_inhouse")
                if d.get("agency_or_inhouse") in ("agency", "inhouse") else None
            ),
            kpi_types=[str(k)[:30] for k in (d.get("kpi_types") or [])[:6]],
            languages=[l for l in (d.get("languages") or []) if l in ("vi", "en")],
        )
        return ext

    async def score_relevance(self, job: dict) -> Optional[RelevanceScore]:
        user = (
            f"Tiêu đề: {job.get('title', '')}\n"
            f"Công ty: {job.get('company_name', '')}\n"
            f"Địa điểm: {job.get('district', '')} {job.get('city', '')}\n"
            f"Mô tả (rút gọn): {(job.get('description') or '')[:1200]}"
        )
        text = await self._chat(SYSTEM_SCORE, user, max_tokens=1500)
        d = self._parse_json(text)
        if not d:
            return None
        try:
            rel = max(0.0, min(1.0, float(d.get("relevance", 0))))
        except (TypeError, ValueError):
            return None
        return RelevanceScore(relevance=rel, reason=str(d.get("reason", ""))[:200])


def _to_bool(v) -> Optional[bool]:
    if v is True:
        return True
    if v is False:
        return False
    return None
