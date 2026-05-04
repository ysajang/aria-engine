"""ARIA Engine - MCP Tool Bridge

MCP 서버에서 발견된 도구를 ARIA ToolRegistry에 자동 등록하는 브릿지
- MCPToolExecutor: MCP tools/call을 래핑하는 ToolExecutor 구현체
- MCPToolBridge: 도구 자동 발견 → ToolDefinition 변환 → ToolRegistry 등록

사용법:
    bridge = MCPToolBridge(tool_registry)
    registered = await bridge.register_server(mcp_client)
    # → MCP 도구들이 ToolRegistry에 자동 등록됨
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

from aria.mcp.client import MCPClient, MCPToolCallError
from aria.mcp.types import MCPInputSchema, MCPToolSchema
from aria.tools.tool_types import (
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolResult,
    SafetyLevelHint,
)

logger = structlog.get_logger()

# MCP 도구 이름에서 안전성 힌트를 추론하기 위한 키워드 매핑
_WRITE_KEYWORDS = {"create", "update", "send", "draft", "delete", "upload", "move", "copy", "label", "unlabel"}
_DESTRUCTIVE_KEYWORDS = {"delete", "remove", "purge"}
_EXTERNAL_KEYWORDS = {"send"}


def _infer_safety_hint(tool_name: str) -> SafetyLevelHint:
    """도구 이름에서 안전성 힌트 추론"""
    name_lower = tool_name.lower()

    for keyword in _DESTRUCTIVE_KEYWORDS:
        if keyword in name_lower:
            return SafetyLevelHint.DESTRUCTIVE

    for keyword in _EXTERNAL_KEYWORDS:
        if keyword in name_lower:
            return SafetyLevelHint.EXTERNAL

    for keyword in _WRITE_KEYWORDS:
        if keyword in name_lower:
            return SafetyLevelHint.WRITE

    return SafetyLevelHint.READ_ONLY


def _json_schema_type_to_simple(json_type: str | list | None) -> str:
    """JSON Schema 타입을 ARIA ToolParameter 타입으로 변환"""
    if json_type is None:
        return "string"
    if isinstance(json_type, list):
        # oneOf 스타일: ["string", "null"] → "string"
        for t in json_type:
            if t != "null":
                return str(t)
        return "string"
    return str(json_type)


def _convert_input_schema(
    input_schema: MCPInputSchema,
) -> list[ToolParameter]:
    """MCP inputSchema → ARIA ToolParameter 리스트 변환

    JSON Schema properties를 개별 ToolParameter로 분해
    """
    parameters: list[ToolParameter] = []
    required_set = set(input_schema.required)

    for prop_name, prop_def in input_schema.properties.items():
        if not isinstance(prop_def, dict):
            continue

        param = ToolParameter(
            name=prop_name,
            type=_json_schema_type_to_simple(prop_def.get("type")),
            description=prop_def.get("description", "")[:500],
            required=prop_name in required_set,
            enum=prop_def.get("enum"),
        )
        parameters.append(param)

    return parameters


def _sanitize_tool_name(name: str) -> str:
    """MCP 도구 이름을 ARIA 도구 이름 규칙(snake_case)에 맞게 변환

    ARIA 규칙: ^[a-z][a-z0-9_]*$
    MCP 이름 예시: "search_threads" / "get_thread" / "createEvent"
    """
    # camelCase → snake_case 변환
    result = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            result.append("_")
        result.append(ch.lower())
    sanitized = "".join(result)

    # 허용되지 않는 문자 제거 (영문소문자/숫자/언더스코어만)
    cleaned = "".join(c for c in sanitized if c.isalnum() or c == "_")

    # 첫 글자가 숫자면 접두어 추가
    if cleaned and cleaned[0].isdigit():
        cleaned = "t_" + cleaned

    return cleaned or "unknown_tool"


class MCPToolExecutor(ToolExecutor):
    """MCP 도구를 ARIA ToolExecutor로 래핑

    tools/call JSON-RPC 호출을 ToolResult로 변환

    Args:
        mcp_client: MCP 클라이언트 인스턴스
        mcp_tool_name: MCP 서버에 등록된 원본 도구 이름
        definition: ARIA용 ToolDefinition
    """

    def __init__(
        self,
        mcp_client: MCPClient,
        mcp_tool_name: str,
        definition: ToolDefinition,
    ) -> None:
        self._mcp_client = mcp_client
        self._mcp_tool_name = mcp_tool_name
        self._definition = definition

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        """MCP tools/call 호출 → ToolResult 변환"""
        start = time.time()

        try:
            result = await self._mcp_client.call_tool(
                self._mcp_tool_name,
                parameters,
            )

            latency_ms = (time.time() - start) * 1000

            if result.isError:
                return ToolResult(
                    tool_name=self._definition.name,
                    success=False,
                    error=result.get_text() or "MCP 도구 실행 에러",
                    latency_ms=latency_ms,
                )

            output_text = result.get_text()

            # JSON 파싱 시도 (구조화된 데이터면 dict로 변환)
            output: Any = output_text
            if output_text:
                try:
                    output = json.loads(output_text)
                except (json.JSONDecodeError, ValueError):
                    pass

            return ToolResult(
                tool_name=self._definition.name,
                success=True,
                output=output,
                latency_ms=latency_ms,
            )

        except MCPToolCallError as e:
            latency_ms = (time.time() - start) * 1000
            return ToolResult(
                tool_name=self._definition.name,
                success=False,
                error=f"MCP 도구 호출 실패: {e}",
                latency_ms=latency_ms,
            )
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            return ToolResult(
                tool_name=self._definition.name,
                success=False,
                error=f"MCP 도구 예외: {str(e)[:300]}",
                latency_ms=latency_ms,
            )

    def get_definition(self) -> ToolDefinition:
        return self._definition


class MCPToolBridge:
    """MCP 도구 자동 발견 → ToolRegistry 등록 브릿지

    MCP 서버에서 tools/list로 발견된 도구를 ARIA ToolRegistry에
    MCPToolExecutor 래퍼로 자동 등록

    Args:
        tool_registry: ARIA ToolRegistry 인스턴스
        override_existing: True면 기존 동일 이름 도구를 MCP 도구로 교체
    """

    def __init__(
        self,
        tool_registry: Any,  # ToolRegistry — 순환 import 방지
        override_existing: bool = True,
    ) -> None:
        self._registry = tool_registry
        self._override_existing = override_existing
        self._registered_tools: dict[str, list[str]] = {}  # server_name → [tool_names]

    async def register_server(
        self,
        mcp_client: MCPClient,
        *,
        skip_tools: set[str] | None = None,
    ) -> list[str]:
        """MCP 서버의 도구를 ToolRegistry에 등록

        Args:
            mcp_client: 연결된 MCPClient 인스턴스
            skip_tools: 등록하지 않을 MCP 도구 이름 세트

        Returns:
            등록된 ARIA 도구 이름 목록
        """
        if not mcp_client.is_connected:
            raise MCPClientError(
                f"서버 '{mcp_client.server_name}'이 연결되지 않았습니다"
            )

        server_config = mcp_client._config
        mcp_tools = mcp_client.discovered_tools
        prefix = server_config.get_tool_prefix()

        registered_names: list[str] = []
        skip = skip_tools or set()

        for mcp_tool in mcp_tools:
            if mcp_tool.name in skip:
                logger.debug(
                    "mcp_tool_skipped",
                    server=server_config.name,
                    tool=mcp_tool.name,
                    reason="skip_list",
                )
                continue

            # ARIA 도구 이름 생성 (접두사 + sanitized 이름)
            sanitized_name = _sanitize_tool_name(mcp_tool.name)
            aria_tool_name = f"{prefix}{sanitized_name}"

            # 이름 길이 제한 (ToolDefinition max_length=100)
            if len(aria_tool_name) > 100:
                aria_tool_name = aria_tool_name[:100]

            # 기존 도구 충돌 처리
            if self._registry.has_tool(aria_tool_name):
                if self._override_existing:
                    self._registry.unregister(aria_tool_name)
                    logger.info(
                        "mcp_tool_override",
                        server=server_config.name,
                        tool=aria_tool_name,
                    )
                else:
                    logger.debug(
                        "mcp_tool_skipped",
                        server=server_config.name,
                        tool=aria_tool_name,
                        reason="already_registered",
                    )
                    continue

            # MCP 스키마 → ToolDefinition 변환
            definition = ToolDefinition(
                name=aria_tool_name,
                description=mcp_tool.description[:1000] if mcp_tool.description else f"MCP tool: {mcp_tool.name}",
                parameters=_convert_input_schema(mcp_tool.inputSchema),
                category=ToolCategory.MCP,
                safety_hint=_infer_safety_hint(mcp_tool.name),
                version="1.0.0",
                enabled=True,
            )

            # MCPToolExecutor 생성 + 등록
            executor = MCPToolExecutor(
                mcp_client=mcp_client,
                mcp_tool_name=mcp_tool.name,
                definition=definition,
            )

            try:
                self._registry.register(definition, executor)
                registered_names.append(aria_tool_name)
            except Exception as e:
                logger.warning(
                    "mcp_tool_register_failed",
                    server=server_config.name,
                    tool=aria_tool_name,
                    error=str(e)[:100],
                )

        self._registered_tools[server_config.name] = registered_names

        logger.info(
            "mcp_server_tools_registered",
            server=server_config.name,
            total_discovered=len(mcp_tools),
            total_registered=len(registered_names),
            tools=registered_names,
        )

        return registered_names

    async def unregister_server(self, server_name: str) -> int:
        """특정 MCP 서버의 도구를 모두 해제

        Returns:
            해제된 도구 수
        """
        tool_names = self._registered_tools.pop(server_name, [])
        removed = 0

        for name in tool_names:
            if self._registry.has_tool(name):
                self._registry.unregister(name)
                removed += 1

        logger.info(
            "mcp_server_tools_unregistered",
            server=server_name,
            removed=removed,
        )
        return removed

    def get_registered_tools(self, server_name: str) -> list[str]:
        """특정 서버에서 등록된 도구 이름 목록"""
        return self._registered_tools.get(server_name, [])

    @property
    def all_registered_servers(self) -> dict[str, list[str]]:
        """전체 서버별 등록된 도구 목록"""
        return dict(self._registered_tools)


# MCPClientError import 위치 (순환 방지)
from aria.mcp.client import MCPClientError  # noqa: E402
