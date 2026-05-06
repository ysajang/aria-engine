"""ARIA Engine - MCP Server (ARIA를 MCP 서버로 노출)

외부 AI 도구(Claude Desktop/Cursor/ChatGPT)가 ARIA에 접속하여
메모리 읽기/쓰기 + 컨텍스트 교환을 할 수 있도록 MCP 프로토콜 서버 구현

전송 방식: HTTP Streamable Transport (POST /mcp)
인증: Bearer 토큰 (ARIA_API_KEY 재사용)
프로토콜: JSON-RPC 2.0 (MCP 2025-03-26 spec)

노출 도구:
    - aria_memory_read: 메모리 토픽 읽기
    - aria_memory_list: 메모리 인덱스 조회
    - aria_context_push: 외부 대화 로그 인입
    - aria_context_pull: 컨텍스트 마크다운 반환
    - aria_knowledge_search: 벡터+BM25 하이브리드 검색

설계 원칙:
    - 기존 FastAPI 앱에 라우터로 마운트 (별도 서버 불필요)
    - ARIA API 인증 재사용 (X-API-Key 또는 Bearer)
    - JSON-RPC 2.0 strict compliance
    - 읽기 전용 도구만 기본 노출 (쓰기는 선택적)
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger()

# MCP 프로토콜 상수
MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_SERVER_NAME = "aria-engine"
MCP_SERVER_VERSION = "0.3.0"

# 노출할 도구 정의
MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "aria_memory_read",
        "description": "ARIA 메모리에서 특정 도메인의 토픽 내용을 읽습니다",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "메모리 스코프 (global/testorum/talksim/autotube)",
                    "default": "global",
                },
                "domain": {
                    "type": "string",
                    "description": "토픽 도메인명 (예: user-profile, coding-conventions)",
                },
            },
            "required": ["domain"],
        },
    },
    {
        "name": "aria_memory_list",
        "description": "ARIA 메모리 인덱스를 조회합니다 (모든 도메인 목록 + 요약)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "메모리 스코프",
                    "default": "global",
                },
            },
        },
    },
    {
        "name": "aria_context_push",
        "description": "현재 대화 내용을 ARIA 메모리에 저장합니다 (자동 분석 + upsert)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "도구 식별자 (cursor/claude-desktop/chatgpt)",
                },
                "scope": {
                    "type": "string",
                    "description": "저장 대상 스코프",
                    "default": "global",
                },
                "messages": {
                    "type": "array",
                    "description": "대화 메시지 배열 [{role, content}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["role", "content"],
                    },
                },
                "session_id": {
                    "type": "string",
                    "description": "세션 ID (중복 방지용)",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "분류 태그",
                },
            },
            "required": ["source", "messages"],
        },
    },
    {
        "name": "aria_context_pull",
        "description": "ARIA 메모리에서 컨텍스트를 가져옵니다 (시스템 프롬프트 주입용 마크다운)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "메모리 스코프",
                    "default": "global",
                },
                "domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "특정 도메인만 로딩 (비어있으면 전체)",
                },
                "token_budget": {
                    "type": "integer",
                    "description": "토큰 예산 (기본 4000)",
                    "default": 4000,
                },
            },
        },
    },
    {
        "name": "aria_knowledge_search",
        "description": "ARIA 지식 베이스에서 시맨틱 검색을 수행합니다",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색 쿼리",
                },
                "collection": {
                    "type": "string",
                    "description": "검색 대상 컬렉션",
                    "default": "default",
                },
                "top_k": {
                    "type": "integer",
                    "description": "반환 결과 수 (기본 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
]


def create_mcp_router() -> APIRouter:
    """MCP 서버 라우터 생성

    FastAPI 앱에 include_router()로 마운트

    Returns:
        APIRouter (/mcp 엔드포인트)
    """
    router = APIRouter(tags=["MCP Server"])

    @router.post("/mcp")
    async def handle_mcp(request: Request) -> JSONResponse:
        """MCP JSON-RPC 2.0 핸들러

        단일 엔드포인트에서 모든 MCP 메서드 처리:
        - initialize
        - initialized (notification)
        - tools/list
        - tools/call
        """
        try:
            body = await request.json()
        except Exception:
            return _jsonrpc_error(None, -32700, "Parse error")

        # JSON-RPC 2.0 기본 검증
        if not isinstance(body, dict):
            return _jsonrpc_error(None, -32600, "Invalid Request")

        method = body.get("method")
        params = body.get("params", {})
        req_id = body.get("id")
        jsonrpc = body.get("jsonrpc")

        if jsonrpc != "2.0":
            return _jsonrpc_error(req_id, -32600, "Invalid Request: jsonrpc must be '2.0'")

        # Notification (id 없음) → 응답 불필요
        if req_id is None:
            if method == "notifications/initialized":
                logger.info("mcp_server_client_initialized")
                return JSONResponse(content="", status_code=204)
            # 알 수 없는 notification → 무시
            return JSONResponse(content="", status_code=204)

        # Method dispatch
        if method == "initialize":
            return _handle_initialize(req_id, params)
        elif method == "tools/list":
            return _handle_tools_list(req_id)
        elif method == "tools/call":
            return await _handle_tools_call(req_id, params, request)
        else:
            return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    return router


def _jsonrpc_response(req_id: Any, result: Any) -> JSONResponse:
    """JSON-RPC 2.0 성공 응답"""
    return JSONResponse(content={
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result,
    })


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    """JSON-RPC 2.0 에러 응답"""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse(
        content={"jsonrpc": "2.0", "id": req_id, "error": error},
        status_code=200,  # JSON-RPC는 항상 200
    )


def _handle_initialize(req_id: Any, params: dict) -> JSONResponse:
    """MCP initialize 핸들러"""
    client_info = params.get("clientInfo", {})
    logger.info(
        "mcp_server_initialize",
        client_name=client_info.get("name", "unknown"),
        client_version=client_info.get("version", "unknown"),
    )

    return _jsonrpc_response(req_id, {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": {
            "name": MCP_SERVER_NAME,
            "version": MCP_SERVER_VERSION,
        },
    })


def _handle_tools_list(req_id: Any) -> JSONResponse:
    """MCP tools/list 핸들러"""
    return _jsonrpc_response(req_id, {
        "tools": MCP_TOOLS,
    })


async def _handle_tools_call(
    req_id: Any,
    params: dict,
    request: Request,
) -> JSONResponse:
    """MCP tools/call 핸들러

    app.state에서 필요한 의존성 가져옴:
    - index_manager
    - memory_loader
    - context_bridge
    - hybrid_retriever (knowledge_search용)
    """
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    if not tool_name:
        return _jsonrpc_error(req_id, -32602, "Missing tool name")

    app = request.app

    try:
        if tool_name == "aria_memory_read":
            result = await _tool_memory_read(app, arguments)
        elif tool_name == "aria_memory_list":
            result = await _tool_memory_list(app, arguments)
        elif tool_name == "aria_context_push":
            result = await _tool_context_push(app, arguments)
        elif tool_name == "aria_context_pull":
            result = await _tool_context_pull(app, arguments)
        elif tool_name == "aria_knowledge_search":
            result = await _tool_knowledge_search(app, arguments)
        else:
            return _jsonrpc_error(
                req_id, -32602,
                f"Unknown tool: {tool_name}",
            )
    except Exception as e:
        logger.error(
            "mcp_server_tool_error",
            tool=tool_name,
            error=str(e),
        )
        return _jsonrpc_response(req_id, {
            "content": [{"type": "text", "text": f"Error: {e}"}],
            "isError": True,
        })

    return _jsonrpc_response(req_id, {
        "content": [{"type": "text", "text": result}],
    })


# === Tool Implementations ===

async def _tool_memory_read(app: Any, args: dict) -> str:
    """aria_memory_read 구현"""
    index_manager = getattr(app.state, "index_manager", None)
    if index_manager is None:
        return "Error: Memory system not initialized"

    scope = args.get("scope", "global")
    domain = args.get("domain", "")

    if not domain:
        return "Error: domain is required"

    try:
        topic = index_manager.get_topic(scope, domain)
        return f"# {domain} (scope: {scope})\n\n{topic.content}"
    except Exception as e:
        return f"Topic not found: {domain} in scope {scope} ({e})"


async def _tool_memory_list(app: Any, args: dict) -> str:
    """aria_memory_list 구현"""
    index_manager = getattr(app.state, "index_manager", None)
    if index_manager is None:
        return "Error: Memory system not initialized"

    scope = args.get("scope", "global")

    try:
        index = index_manager.get_index(scope)
        if not index.entries:
            return f"No memory entries in scope: {scope}"

        lines = [f"# Memory Index (scope: {scope})\n"]
        for domain, entry in sorted(index.entries.items()):
            lines.append(f"- **{domain}**: {entry.summary} (v{entry.version})")

        return "\n".join(lines)
    except Exception as e:
        return f"Error reading index for scope {scope}: {e}"


async def _tool_context_push(app: Any, args: dict) -> str:
    """aria_context_push 구현"""
    context_bridge = getattr(app.state, "context_bridge", None)
    if context_bridge is None:
        return "Error: Context bridge not initialized"

    from aria.context.types import ContextPushRequest, ConversationMessage

    try:
        messages = [
            ConversationMessage(
                role=m.get("role", "user"),
                content=m.get("content", ""),
            )
            for m in args.get("messages", [])
            if m.get("content")
        ]

        if not messages:
            return "Error: No valid messages provided"

        request = ContextPushRequest(
            source=args.get("source", "custom"),
            scope=args.get("scope", "global"),
            messages=messages,
            session_id=args.get("session_id"),
            tags=args.get("tags", []),
        )

        response = await context_bridge.push(request)

        result_lines = [
            f"Context pushed successfully",
            f"- Status: {response.status}",
            f"- Insights extracted: {response.insights_extracted}",
            f"- Memory updated: {response.memory_updated}",
        ]
        if response.memory_domain:
            result_lines.append(f"- Memory domain: {response.memory_domain}")

        return "\n".join(result_lines)

    except Exception as e:
        return f"Error pushing context: {e}"


async def _tool_context_pull(app: Any, args: dict) -> str:
    """aria_context_pull 구현"""
    context_bridge = getattr(app.state, "context_bridge", None)
    if context_bridge is None:
        return "Error: Context bridge not initialized"

    from aria.context.types import ContextPullRequest

    try:
        request = ContextPullRequest(
            scope=args.get("scope", "global"),
            domains=args.get("domains"),
            token_budget=args.get("token_budget", 4000),
        )

        response = await context_bridge.pull(request)
        return response.content or "(No context available for this scope)"

    except Exception as e:
        return f"Error pulling context: {e}"


async def _tool_knowledge_search(app: Any, args: dict) -> str:
    """aria_knowledge_search 구현"""
    hybrid_retriever = getattr(app.state, "hybrid_retriever", None)
    if hybrid_retriever is None:
        return "Error: Knowledge search not initialized"

    query = args.get("query", "")
    if not query:
        return "Error: query is required"

    collection = args.get("collection", "default")
    top_k = min(args.get("top_k", 5), 10)

    try:
        results = hybrid_retriever.search(
            query=query,
            collection=collection,
            top_k=top_k,
        )

        if not results:
            return f"No results found for: {query}"

        lines = [f"# Search Results for: {query}\n"]
        for i, result in enumerate(results, 1):
            score = getattr(result, "score", 0)
            text = getattr(result, "text", str(result))
            lines.append(f"## Result {i} (score: {score:.3f})")
            lines.append(text[:500])
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return f"Error searching: {e}"
