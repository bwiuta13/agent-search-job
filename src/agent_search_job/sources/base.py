"""Interface adapter nguồn việc làm.

Theo docs/research-website.md mục 5: mỗi nguồn triển khai discover/fetchDetail/
normalize/healthCheck. Mỗi adapter lỗi thì bỏ qua, không làm sập pipeline.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from ..domain.models import Company, NormalizedJob, utcnow


@dataclass
class SourceHealth:
    source: str
    ok: bool
    latency_ms: Optional[int] = None
    error: str = ""
    checked_at: object = field(default_factory=utcnow)
    records_last_run: int = 0


@dataclass
class DiscoverInput:
    query: str = "content marketing"
    location: str = "Ho Chi Minh"
    max_results: int = 50


class JobSourceAdapter(ABC):
    """Base class cho mọi adapter nguồn."""

    name: str = ""

    @abstractmethod
    async def discover(self, input: DiscoverInput) -> AsyncIterator[NormalizedJob]:
        """Tìm và trả danh sách job đã normalize (chưa qua normalize_job)."""
        ...  # pragma: no cover
        if False:
            yield

    async def health_check(self) -> SourceHealth:
        return SourceHealth(source=self.name, ok=True, checked_at=utcnow())

    # hook cho adapter có thêm company profile
    async def enrich_company(self, company: Company) -> Company:
        return company
