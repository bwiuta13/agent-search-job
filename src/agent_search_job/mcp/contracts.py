"""Giới hạn và validation cho input của MCP tools + search API.

Theo docs/research-mcp.md mục 5-6: page_size tối đa 25, posted_within_days tối đa 60,
schema từ chối trường thừa, giới hạn độ dài chuỗi.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..domain.models import RoleCluster, WorkMode

MAX_PAGE_SIZE = 25
DEFAULT_PAGE_SIZE = 10
MAX_POSTED_WITHIN_DAYS = 60
DEFAULT_POSTED_WITHIN_DAYS = 14
MAX_QUERY_LENGTH = 200

VALID_ROLE_CLUSTERS = [c.value for c in RoleCluster]
VALID_WORK_MODES = [m.value for m in WorkMode]
VALID_SOURCES = ["topcv", "vietnamworks", "itviec", "careerviet", "linkedin", "indeed", "topdev", "all"]
VALID_DISTRICTS = [
    "Quận 1", "Quận 3", "Quận 4", "Quận 5", "Quận 6", "Quận 7", "Quận 8",
    "Quận 10", "Quận 11", "Quận 12", "Bình Thạnh", "Gò Vấp", "Phú Nhuận",
    "Tân Bình", "Tân Phú", "Thủ Đức", "Củ Chi", "Hóc Môn", "Bình Chánh",
    "Nhà Bè", "Cần Giờ",
]


class ValidationError(ValueError):
    """Input không hợp lệ - MCP tool phải trả actionable error."""


@dataclass
class SearchFilters:
    query: Optional[str] = None
    role_clusters: Optional[list[str]] = None
    districts: Optional[list[str]] = None
    salary_min_vnd: Optional[int] = None
    experience_max_years: Optional[float] = None
    work_modes: Optional[list[str]] = None
    posted_within_days: int = DEFAULT_POSTED_WITHIN_DAYS
    portfolio_required: Optional[bool] = None
    source: str = "all"
    page_size: int = DEFAULT_PAGE_SIZE
    cursor: Optional[str] = None

    def validate(self) -> "SearchFilters":
        if self.query is not None:
            q = self.query.strip()
            if not q:
                self.query = None
            elif len(q) > MAX_QUERY_LENGTH:
                raise ValidationError(
                    f"query quá dài ({len(q)} ký tự, tối đa {MAX_QUERY_LENGTH})"
                )
            else:
                self.query = q
        if self.role_clusters:
            bad = [r for r in self.role_clusters if r not in VALID_ROLE_CLUSTERS]
            if bad:
                raise ValidationError(
                    f"role_clusters không hợp lệ: {bad}. Hợp lệ: {VALID_ROLE_CLUSTERS}"
                )
        if self.work_modes:
            bad = [m for m in self.work_modes if m not in VALID_WORK_MODES]
            if bad:
                raise ValidationError(
                    f"work_modes không hợp lệ: {bad}. Hợp lệ: {VALID_WORK_MODES}"
                )
        if self.source not in VALID_SOURCES:
            raise ValidationError(
                f"source không hợp lệ: {self.source}. Hợp lệ: {VALID_SOURCES}"
            )
        if self.page_size < 1 or self.page_size > MAX_PAGE_SIZE:
            raise ValidationError(
                f"page_size phải từ 1 đến {MAX_PAGE_SIZE}"
            )
        if self.posted_within_days < 1 or self.posted_within_days > MAX_POSTED_WITHIN_DAYS:
            raise ValidationError(
                f"posted_within_days phải từ 1 đến {MAX_POSTED_WITHIN_DAYS}"
            )
        if self.salary_min_vnd is not None and self.salary_min_vnd < 0:
            raise ValidationError("salary_min_vnd phải >= 0")
        if self.experience_max_years is not None and self.experience_max_years < 0:
            raise ValidationError("experience_max_years phải >= 0")
        if self.districts:
            self.districts = [d.strip() for d in self.districts if d.strip()]
            if len(self.districts) > 10:
                raise ValidationError("districts tối đa 10 phần tử")
        return self
