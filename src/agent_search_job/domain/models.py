"""Domain models và taxonomy cho Job Search Agent.

Theo docs/research-website.md mục 6 (taxonomy role cluster) và mục 8 (data model),
và docs/research-mcp.md mục 3 (thiết kế tool, filter enum).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class RoleCluster(str, Enum):
    CONTENT_MARKETING = "content_marketing"
    COPYWRITER_SEO = "copywriter_seo"
    SOCIAL_MEDIA = "social_media"
    COMMUNICATIONS_PR = "communications_pr"
    VIDEO_PRODUCTION = "video_production"
    MULTIMEDIA_DESIGN = "multimedia_design"
    CREATIVE_PRODUCTION = "creative_production"


class WorkMode(str, Enum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"


class SourceName(str, Enum):
    TOPCV = "topcv"
    VIETNAMWORKS = "vietnamworks"
    ITVIEC = "itviec"
    CAREERVIET = "careerviet"
    LINKEDIN = "linkedin"
    INDEED = "indeed"
    TOPDEV = "topdev"


ROLE_CLUSTER_LABELS_VI: dict[str, str] = {
    RoleCluster.CONTENT_MARKETING.value: "Content Marketing",
    RoleCluster.COPYWRITER_SEO.value: "Copywriter / SEO Content",
    RoleCluster.SOCIAL_MEDIA.value: "Social Media / Community",
    RoleCluster.COMMUNICATIONS_PR.value: "Truyền thông / PR",
    RoleCluster.VIDEO_PRODUCTION.value: "Video / Biên tập video",
    RoleCluster.MULTIMEDIA_DESIGN.value: "Thiết kế đa phương tiện",
    RoleCluster.CREATIVE_PRODUCTION.value: "Sản xuất sáng tạo / Content Creator",
}

# Từ khóa (vi + en, lowercase) ánh xạ sang role cluster dùng cho rule classifier.
ROLE_CLUSTER_KEYWORDS: dict[str, list[str]] = {
    RoleCluster.CONTENT_MARKETING.value: [
        "content marketing", "content executive", "content specialist",
        "chuyên viên content", "nội dung marketing", "content lead",
        "content manager", "content strategist", "growth content",
    ],
    RoleCluster.COPYWRITER_SEO.value: [
        "copywriter", "copy writer", "seo content", "content writer",
        "content seo", "seo specialist", "seo executive", "viết content",
        "chuyên viên seo", "nội dung seo",
    ],
    RoleCluster.SOCIAL_MEDIA.value: [
        "social media", "community manager", "community", "tiktok",
        "fanpage", "facebook ads", "social executive", "social specialist",
        "seeding", "kols", "influencer",
    ],
    RoleCluster.COMMUNICATIONS_PR.value: [
        "truyền thông", "pr ", "press", "communications", "communication",
        "pr specialist", "public relations", "hoạt động báo chí",
        "marcom", "marketing communications", "công chúng",
    ],
    RoleCluster.VIDEO_PRODUCTION.value: [
        "video editor", "video content", "videographer", "editor video",
        "dựng video", "biên tập video", "video producer", "short video",
        "post production", "motion",
    ],
    RoleCluster.MULTIMEDIA_DESIGN.value: [
        "multimedia", "designer", "thiết kế", "graphic design",
        "đồ họa", "motion graphic", "visual", "canva", "photoshop",
        "illustrator", "ui content",
    ],
    RoleCluster.CREATIVE_PRODUCTION.value: [
        "creative", "content creator", "script writer", "biên kịch",
        "content producer", "sáng tạo nội dung", "creative producer",
        "storyboard",
    ],
}

PLATFORMS = ["tiktok", "facebook", "instagram", "youtube", "website", "email", "linkedin"]
CREATIVE_TOOLS = [
    "capcut", "canva", "premiere", "premiere pro", "after effects",
    "photoshop", "illustrator", "figma", "da vinci", "final cut",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Provenance:
    """Gốc dữ liệu theo docs/research-mcp.md mục 4 - mọi dữ liệu phải có provenance."""
    source: str
    source_url: str
    source_job_id: str = ""
    first_seen_at: datetime = field(default_factory=utcnow)
    last_verified_at: datetime = field(default_factory=utcnow)


@dataclass
class Company:
    company_id: int = 0
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    industry: str = ""
    size: str = ""
    website: str = ""
    address: str = ""
    profile: str = ""  # mô tả do công ty/nguồn cung cấp
    profile_source_url: str = ""
    tax_code: str = ""


@dataclass
class CompanyFeedback:
    """Feedback công ty có nguồn, timestamp, confidence - không suy đoán."""
    company_id: int
    source: str
    source_url: str
    rating: Optional[float] = None
    review_count: Optional[int] = None
    summary: str = ""
    fetched_at: datetime = field(default_factory=utcnow)
    confidence: str = "low"  # low | medium | high

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "source_url": self.source_url,
            "rating": self.rating,
            "review_count": self.review_count,
            "summary": self.summary,
            "fetched_at": self.fetched_at.isoformat(),
            "confidence": self.confidence,
        }


@dataclass
class NormalizedJob:
    """Bản ghi canonical sau normalize - đủ field cho search + MCP output."""
    job_id: int = 0
    title: str = ""
    company_name: str = ""
    company_id: int = 0
    role_cluster: str = RoleCluster.CONTENT_MARKETING.value
    role_cluster_confidence: float = 0.0
    salary_min_vnd: Optional[int] = None
    salary_max_vnd: Optional[int] = None
    salary_text: str = ""
    experience_years_min: Optional[float] = None
    experience_years_max: Optional[float] = None
    work_mode: str = WorkMode.ONSITE.value
    district: str = ""          # quận/huyện cũ hoặc phường/xã mới
    city: str = "Hồ Chí Minh"
    is_hcm: bool = True
    address: str = ""
    platforms: list[str] = field(default_factory=list)
    creative_tools: list[str] = field(default_factory=list)
    portfolio_required: Optional[bool] = None
    deadline: Optional[datetime] = None
    description: str = ""
    requirements: str = ""
    benefits: list[str] = field(default_factory=list)
    apply_url: str = ""
    languages: list[str] = field(default_factory=list)
    is_active: bool = True
    # provenance
    source: str = ""
    source_url: str = ""
    source_job_id: str = ""
    first_seen_at: datetime = field(default_factory=utcnow)
    last_verified_at: datetime = field(default_factory=utcnow)

    def to_search_item(self) -> dict:
        """Dạng ngắn cho kết quả search (web + MCP)."""
        return {
            "job_id": self.job_id,
            "title": self.title,
            "company": self.company_name,
            "role_cluster": self.role_cluster,
            "role_cluster_label": ROLE_CLUSTER_LABELS_VI.get(self.role_cluster, self.role_cluster),
            "salary_min_vnd": self.salary_min_vnd,
            "salary_max_vnd": self.salary_max_vnd,
            "salary_text": self.salary_text,
            "district": self.district,
            "city": self.city,
            "work_mode": self.work_mode,
            "experience_years_min": self.experience_years_min,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "is_active": self.is_active,
            "platforms": self.platforms,
            "creative_tools": self.creative_tools,
            "portfolio_required": self.portfolio_required,
            "source": self.source,
            "source_url": self.source_url,
            "last_verified_at": self.last_verified_at.isoformat(),
        }

    def to_detail(self) -> dict:
        """Dạng đầy đủ cho trang chi tiết + tool get_job."""
        d = self.to_search_item()
        d.update({
            "job_id": self.job_id,
            "address": self.address,
            "experience_years_max": self.experience_years_max,
            "languages": self.languages,
            "description": self.description,
            "requirements": self.requirements,
            "benefits": self.benefits,
            "apply_url": self.apply_url or self.source_url,
            "company_id": self.company_id,
            "first_seen_at": self.first_seen_at.isoformat(),
            "provenance": {
                "source": self.source,
                "source_url": self.source_url,
                "source_job_id": self.source_job_id,
                "first_seen_at": self.first_seen_at.isoformat(),
                "last_verified_at": self.last_verified_at.isoformat(),
                "expires_at": self.deadline.isoformat() if self.deadline else None,
                "is_active": self.is_active,
            },
        })
        return d
