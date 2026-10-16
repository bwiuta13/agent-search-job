"""Remote MCP server - 5 tool `vn_jobs_*` theo docs/research-mcp.md.

Triển khai JSON-RPC 2.0 Streamable HTTP tại /mcp, chạy trong FastAPI app.
Mỗi tool có inputSchema + outputSchema (JSON Schema), trả structuredContent
kèm TextContent JSON để tương thích client cũ.
MVP auth: bearer token trùng users.token (invite-only); deploy thật nâng lên OAuth 2.1.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .contracts import (
    MAX_PAGE_SIZE, MAX_POSTED_WITHIN_DAYS, SearchFilters, ValidationError,
    VALID_ROLE_CLUSTERS, VALID_WORK_MODES, VALID_SOURCES,
)
from ..storage.store import JobStore

log = logging.getLogger("mcp")

PROTOCOL_VERSION = "2025-06-18"
JSONRPC = "2.0"

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "maxLength": 200,
                  "description": "Từ khóa tự do, vd 'content tiktok', 'video editor'"},
        "role_clusters": {"type": "array", "items": {"type": "string",
                                                     "enum": VALID_ROLE_CLUSTERS}},
        "districts": {"type": "array", "items": {"type": "string"},
                      "description": "Tên quận/huyện cũ hoặc phường/xã mới, TP.HCM"},
        "salary_min_vnd": {"type": "integer", "minimum": 0},
        "experience_max_years": {"type": "number", "minimum": 0},
        "work_modes": {"type": "array", "items": {"type": "string",
                                                  "enum": VALID_WORK_MODES}},
        "job_levels": {"type": "array", "items": {"type": "string",
                       "enum": ["intern", "fresher", "junior", "middle", "senior",
                                "lead", "manager"]},
                       "description": "Cấp bậc: intern/fresher/junior/middle/senior/lead/manager"},
        "company_query": {"type": "string", "maxLength": 200,
                          "description": "Chỉ tìm job của công ty có tên chứa chuỗi này, vd 'FPT'"},
        "posted_within_days": {"type": "integer", "minimum": 1,
                               "maximum": MAX_POSTED_WITHIN_DAYS, "default": 14},
        "portfolio_required": {"type": "boolean"},
        "source": {"type": "string", "enum": VALID_SOURCES, "default": "all"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE,
                      "default": 10},
        "cursor": {"type": "string",
                   "description": "Con trỏ phân trang từ next_cursor của lần trước"},
    },
    "additionalProperties": False,
}

SEARCH_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["items", "next_cursor", "total_estimate", "applied_filters",
                 "data_freshness"],
    "properties": {
        "items": {"type": "array", "items": {"type": "object"}},
        "next_cursor": {"type": ["string", "null"]},
        "total_estimate": {"type": "integer"},
        "applied_filters": {"type": "object"},
        "data_freshness": {"type": "object"},
    },
}

GET_JOB_INPUT_SCHEMA = {
    "type": "object",
    "required": ["job_id"],
    "properties": {"job_id": {"type": "integer", "minimum": 1}},
    "additionalProperties": False,
}

GET_JOB_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["job"],
    "properties": {"job": {"type": ["object", "null"]}},
}

GET_COMPANY_INPUT_SCHEMA = {
    "type": "object",
    "required": ["company_id"],
    "properties": {"company_id": {"type": "integer", "minimum": 1}},
    "additionalProperties": False,
}

GET_COMPANY_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["company", "open_jobs"],
    "properties": {
        "company": {"type": ["object", "null"]},
        "open_jobs": {"type": "array", "items": {"type": "object"}},
    },
}

FEEDBACK_INPUT_SCHEMA = {
    "type": "object",
    "required": ["company_id"],
    "properties": {"company_id": {"type": "integer", "minimum": 1}},
    "additionalProperties": False,
}

FEEDBACK_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["status", "feedback"],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "unavailable"]},
        "feedback": {"type": ["array", "null"],
                     "description": "Danh sách feedback theo nguồn, có rating/review_count/confidence"},
        "note": {"type": "string"},
    },
}

SMART_SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "required": ["text"],
    "properties": {
        "text": {"type": "string", "maxLength": 300,
                 "description": "Câu tìm việc tiếng Việt tự nhiên, vd 'tìm việc content tiktok lương trên 15 triệu ở quận 1'"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
        "cursor": {"type": "string"},
    },
    "additionalProperties": False,
}

SMART_SEARCH_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["items", "next_cursor", "total_estimate", "parsed_filters", "parser"],
    "properties": {
        "items": {"type": "array", "items": {"type": "object"}},
        "next_cursor": {"type": ["string", "null"]},
        "total_estimate": {"type": "integer"},
        "parsed_filters": {"type": "object",
                           "description": "Filter đã parse từ câu tự nhiên"},
        "parser": {"type": "object",
                   "description": "llm_used, note - parser nào đã xử lý"},
    },
}

FACETS_INPUT_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

FACETS_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["role_clusters", "districts", "work_modes", "sources"],
    "properties": {
        "role_clusters": {"type": "array"},
        "districts": {"type": "array"},
        "work_modes": {"type": "array"},
        "sources": {"type": "array"},
    },
}

TOOLS = [
    {
        "name": "vn_jobs_search_jobs",
        "description": (
            "Tìm việc làm Content Marketing / truyền thông đa phương tiện tại TP.HCM. "
            "Dữ liệu đã chuẩn hóa kèm lương VND, khu vực, hạn nộp và nguồn."
        ),
        "inputSchema": SEARCH_INPUT_SCHEMA,
        "outputSchema": SEARCH_OUTPUT_SCHEMA,
    },
    {
        "name": "vn_jobs_get_job",
        "description": "Lấy bản ghi đầy đủ của một việc làm theo job_id.",
        "inputSchema": GET_JOB_INPUT_SCHEMA,
        "outputSchema": GET_JOB_OUTPUT_SCHEMA,
    },
    {
        "name": "vn_jobs_get_company",
        "description": "Hồ sơ công ty đã chuẩn hóa kèm các việc đang tuyển.",
        "inputSchema": GET_COMPANY_INPUT_SCHEMA,
        "outputSchema": GET_COMPANY_OUTPUT_SCHEMA,
    },
    {
        "name": "vn_jobs_get_company_feedback",
        "description": (
            "Đánh giá công ty theo nguồn, kèm rating, số review, mức tin cậy và link nguồn. "
            "Không có nguồn đủ tin cậy thì trả status=unavailable, không suy đoán."
        ),
        "inputSchema": FEEDBACK_INPUT_SCHEMA,
        "outputSchema": FEEDBACK_OUTPUT_SCHEMA,
    },
    {
        "name": "vn_jobs_smart_search",
        "description": (
            "Tìm việc bằng câu tiếng Việt tự nhiên (LLM parse). "
            "Ví dụ: 'tìm việc content tiktok lương trên 15 triệu ở quận 1', "
            "'việc social media mới đăng tuần này có thể remote'."
        ),
        "inputSchema": SMART_SEARCH_INPUT_SCHEMA,
        "outputSchema": SMART_SEARCH_OUTPUT_SCHEMA,
    },
    {
        "name": "vn_jobs_get_market_facets",
        "description": "Các lựa chọn lọc hiện có (role cluster, quận, work mode, nguồn) để xây truy vấn hợp lệ.",
        "inputSchema": FACETS_INPUT_SCHEMA,
        "outputSchema": FACETS_OUTPUT_SCHEMA,
    },
]


class MCPServer:
    """MCP JSON-RPC handler. Gắn vào FastAPI qua route /mcp."""

    def __init__(self, store: JobStore):
        self.store = store
        self._handlers = {
            "initialize": self._initialize,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "ping": self._ping,
        }

    # ------------------------------------------------------------------
    # JSON-RPC plumbing
    # ------------------------------------------------------------------

    async def handle(self, body: dict) -> Optional[dict]:
        method = body.get("method", "")
        msg_id = body.get("id")
        params = body.get("params") or {}
        handler = self._handlers.get(method)
        if handler is None:
            return self._error(msg_id, -32601, f"Method not found: {method}")
        try:
            result = await handler(params)
            if msg_id is None:
                return None  # notification, không cần reply
            return {"jsonrpc": JSONRPC, "id": msg_id, "result": result}
        except ValidationError as e:
            return self._error(msg_id, -32602, f"Invalid params: {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("MCP internal error")
            return self._error(msg_id, -32603, f"Internal error: {type(e).__name__}")

    @staticmethod
    def _error(msg_id, code: int, message: str) -> dict:
        return {"jsonrpc": JSONRPC, "id": msg_id,
                "error": {"code": code, "message": message}}

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    async def _initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "vn-jobs-mcp", "version": "0.1.0"},
        }

    async def _ping(self, params: dict) -> dict:
        return {}

    async def _tools_list(self, params: dict) -> dict:
        return {"tools": TOOLS}

    async def _tools_call(self, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments") or {}
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            return self._tool_error(name, f"Unknown tool: {name}")
        try:
            structured = await self._dispatch(name, args)
            return {
                "content": [{"type": "text",
                             "text": json.dumps(structured, ensure_ascii=False)}],
                "structuredContent": structured,
            }
        except ValidationError as e:
            return self._tool_error(name, str(e))
        except Exception as e:  # noqa: BLE001
            return self._tool_error(name, f"{type(e).__name__}: {e}")

    @staticmethod
    def _tool_error(name: str, message: str) -> dict:
        return {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        }

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, name: str, args: dict) -> dict:
        if name == "vn_jobs_search_jobs":
            return self._search_jobs(args)
        if name == "vn_jobs_get_job":
            return self._get_job(args)
        if name == "vn_jobs_get_company":
            return self._get_company(args)
        if name == "vn_jobs_get_company_feedback":
            return self._get_feedback(args)
        if name == "vn_jobs_smart_search":
            return await self._smart_search(args)
        if name == "vn_jobs_get_market_facets":
            return self._get_facets(args)
        raise ValidationError(f"Unknown tool: {name}")

    def _search_jobs(self, args: dict) -> dict:
        f = SearchFilters(
            query=args.get("query"),
            role_clusters=args.get("role_clusters"),
            districts=args.get("districts"),
            salary_min_vnd=args.get("salary_min_vnd"),
            experience_max_years=args.get("experience_max_years"),
            work_modes=args.get("work_modes"),
            job_levels=args.get("job_levels"),
            company_query=args.get("company_query"),
            posted_within_days=args.get("posted_within_days", 14),
            portfolio_required=args.get("portfolio_required"),
            source=args.get("source", "all"),
            page_size=args.get("page_size", 10),
            cursor=args.get("cursor"),
        ).validate()
        res = self.store.search_jobs(
            query=f.query, role_clusters=f.role_clusters, districts=f.districts,
            salary_min_vnd=f.salary_min_vnd,
            experience_max_years=f.experience_max_years,
            work_modes=f.work_modes, job_levels=f.job_levels,
            company_query=f.company_query,
            posted_within_days=f.posted_within_days,
            portfolio_required=f.portfolio_required, source=f.source,
            page_size=f.page_size, cursor=f.cursor,
        )
        last = self.store.last_crawl_run() or {}
        return {
            "items": res["items"],
            "next_cursor": res["next_cursor"],
            "total_estimate": res["total_estimate"],
            "applied_filters": {
                "query": f.query, "role_clusters": f.role_clusters,
                "districts": f.districts, "salary_min_vnd": f.salary_min_vnd,
                "experience_max_years": f.experience_max_years,
                "work_modes": f.work_modes,
                "job_levels": f.job_levels, "company_query": f.company_query,
                "posted_within_days": f.posted_within_days,
                "portfolio_required": f.portfolio_required, "source": f.source,
                "page_size": f.page_size,
            },
            "data_freshness": {
                "last_crawl_finished_at": last.get("finished_at"),
                "note": "Dữ liệu thu thập định kỳ; kiểm tra source_url trước khi ứng tuyển.",
            },
        }

    def _get_job(self, args: dict) -> dict:
        job_id = args.get("job_id")
        if not isinstance(job_id, int) or job_id < 1:
            raise ValidationError("job_id phải là số nguyên dương")
        job = self.store.get_job(job_id)
        return {"job": job}

    def _get_company(self, args: dict) -> dict:
        company_id = args.get("company_id")
        if not isinstance(company_id, int) or company_id < 1:
            raise ValidationError("company_id phải là số nguyên dương")
        company = self.store.get_company(company_id)
        return {"company": company, "open_jobs": self.store.company_open_jobs(company_id)}

    def _get_feedback(self, args: dict) -> dict:
        company_id = args.get("company_id")
        if not isinstance(company_id, int) or company_id < 1:
            raise ValidationError("company_id phải là số nguyên dương")
        feedback = self.store.get_feedback(company_id)
        if not feedback:
            company = self.store.get_company(company_id) or {}
            return {
                "status": "unavailable",
                "feedback": None,
                "note": (
                    f"Chưa có nguồn đánh giá đủ tin cậy cho "
                    f"{company.get('name', 'công ty này')}. "
                    "Bạn có thể tự kiểm tra tại Indeed/Glassdoor/ITviec."
                ),
            }
        return {"status": "ok", "feedback": feedback}

    async def _smart_search(self, args: dict) -> dict:
        text = str(args.get("text", "")).strip()
        if not text:
            raise ValidationError("text không được để trống")
        if len(text) > 300:
            raise ValidationError("text tối đa 300 ký tự")
        try:
            page_size = int(args.get("page_size") or 10)
            page_size = max(1, min(25, page_size))
        except (TypeError, ValueError):
            page_size = 10
        from ..pipeline.nl_query import NLQueryParser
        parsed = await NLQueryParser().parse(text)
        f = parsed.filters
        f.page_size = page_size
        f.cursor = args.get("cursor")
        res = self.store.search_jobs(
            query=f.query, role_clusters=f.role_clusters, districts=f.districts,
            salary_min_vnd=f.salary_min_vnd,
            experience_max_years=f.experience_max_years,
            work_modes=f.work_modes, job_levels=f.job_levels,
            company_query=f.company_query,
            posted_within_days=f.posted_within_days,
            portfolio_required=f.portfolio_required, source=f.source,
            page_size=f.page_size, cursor=f.cursor,
        )
        last = self.store.last_crawl_run() or {}
        return {
            "items": res["items"],
            "next_cursor": res["next_cursor"],
            "total_estimate": res["total_estimate"],
            "parsed_filters": {
                "query": f.query, "role_clusters": f.role_clusters,
                "districts": f.districts, "salary_min_vnd": f.salary_min_vnd,
                "work_modes": f.work_modes,
                "job_levels": f.job_levels, "company_query": f.company_query,
                "posted_within_days": f.posted_within_days,
                "portfolio_required": f.portfolio_required, "source": f.source,
            },
            "parser": {
                "llm_used": parsed.llm_used,
                "note": parsed.note,
                "raw_text": parsed.raw_text,
            },
            "data_freshness": {
                "last_crawl_finished_at": last.get("finished_at"),
            },
        }

    def _get_facets(self, args: dict) -> dict:
        return self.store.get_facets()
