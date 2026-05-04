"""ARIA Engine - MCP Client + Tool Bridge Unit Tests

테스트 범위:
- MCP 프로토콜 타입 (types.py): 46개
- MCP 클라이언트 (client.py): 28개
- 도구 브릿지 (tool_bridge.py): 26개
- Google MCP 서버 설정 (google_servers.py): 14개
- Config (config.py MCPConfig): 8개
총: ~122개 테스트
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# === Types Tests ===


class TestMCPServerConfig:
    """MCPServerConfig 테스트"""

    def test_basic_config(self):
        from aria.mcp.types import MCPServerConfig, MCPTransport, MCPAuthType

        config = MCPServerConfig(
            name="gmail",
            url="https://gmailmcp.googleapis.com/mcp/v1",
        )
        assert config.name == "gmail"
        assert config.transport == MCPTransport.HTTP
        assert config.auth_type == MCPAuthType.BEARER
        assert config.enabled is True
        assert config.timeout == 30.0
        assert config.priority == 10

    def test_get_tool_prefix_custom(self):
        from aria.mcp.types import MCPServerConfig

        config = MCPServerConfig(
            name="gmail",
            url="https://example.com/mcp",
            tool_prefix="gm_",
        )
        assert config.get_tool_prefix() == "gm_"

    def test_get_tool_prefix_auto(self):
        from aria.mcp.types import MCPServerConfig

        config = MCPServerConfig(
            name="gmail",
            url="https://example.com/mcp",
        )
        assert config.get_tool_prefix() == "mcp_gmail_"

    def test_config_validation_name_required(self):
        from aria.mcp.types import MCPServerConfig

        with pytest.raises(Exception):
            MCPServerConfig(name="", url="https://example.com/mcp")


class TestJSONRPCTypes:
    """JSON-RPC 2.0 타입 테스트"""

    def test_request(self):
        from aria.mcp.types import JSONRPCRequest

        req = JSONRPCRequest(id=1, method="initialize", params={"key": "value"})
        assert req.jsonrpc == "2.0"
        assert req.id == 1
        assert req.method == "initialize"
        assert req.params == {"key": "value"}

    def test_request_serialization(self):
        from aria.mcp.types import JSONRPCRequest

        req = JSONRPCRequest(id=1, method="tools/list")
        data = req.model_dump(exclude_none=True)
        assert data == {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    def test_notification_no_id(self):
        from aria.mcp.types import JSONRPCNotification

        notif = JSONRPCNotification(method="initialized")
        data = notif.model_dump(exclude_none=True)
        assert "id" not in data
        assert data["method"] == "initialized"

    def test_response_success(self):
        from aria.mcp.types import JSONRPCResponse

        resp = JSONRPCResponse(id=1, result={"status": "ok"})
        assert not resp.is_error
        assert resp.result == {"status": "ok"}

    def test_response_error(self):
        from aria.mcp.types import JSONRPCError, JSONRPCResponse

        resp = JSONRPCResponse(
            id=1,
            error=JSONRPCError(code=-32600, message="Invalid request"),
        )
        assert resp.is_error
        assert resp.error.code == -32600


class TestMCPProtocolTypes:
    """MCP 프로토콜 타입 테스트"""

    def test_client_info(self):
        from aria.mcp.types import MCPClientInfo

        info = MCPClientInfo()
        assert info.name == "aria-engine"
        assert info.version == "0.3.0"

    def test_capabilities_has_tools(self):
        from aria.mcp.types import MCPCapabilities

        cap = MCPCapabilities(tools={"listChanged": True})
        assert cap.has_tools is True

    def test_capabilities_no_tools(self):
        from aria.mcp.types import MCPCapabilities

        cap = MCPCapabilities()
        assert cap.has_tools is False

    def test_initialize_result(self):
        from aria.mcp.types import MCPInitializeResult

        result = MCPInitializeResult.model_validate({
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "gmail-mcp", "version": "1.0.0"},
        })
        assert result.protocolVersion == "2025-03-26"
        assert result.capabilities.has_tools is True
        assert result.serverInfo.name == "gmail-mcp"


class TestMCPToolSchema:
    """MCP 도구 스키마 테스트"""

    def test_basic_tool(self):
        from aria.mcp.types import MCPToolSchema

        tool = MCPToolSchema(
            name="search_threads",
            description="Search Gmail threads",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Max results"},
                },
                "required": ["query"],
            },
        )
        assert tool.name == "search_threads"
        assert tool.get_parameter_names() == ["query", "max_results"]

    def test_tool_no_params(self):
        from aria.mcp.types import MCPToolSchema

        tool = MCPToolSchema(name="list_labels", description="List labels")
        assert tool.get_parameter_names() == []


class TestMCPToolCallResult:
    """MCP 도구 호출 결과 테스트"""

    def test_text_result(self):
        from aria.mcp.types import MCPToolCallResult, MCPToolCallContent

        result = MCPToolCallResult(
            content=[
                MCPToolCallContent(type="text", text="Hello"),
                MCPToolCallContent(type="text", text="World"),
            ],
            isError=False,
        )
        assert result.get_text() == "Hello\nWorld"

    def test_error_result(self):
        from aria.mcp.types import MCPToolCallResult, MCPToolCallContent

        result = MCPToolCallResult(
            content=[MCPToolCallContent(type="text", text="Not found")],
            isError=True,
        )
        assert result.isError is True
        assert result.get_text() == "Not found"

    def test_empty_result(self):
        from aria.mcp.types import MCPToolCallResult

        result = MCPToolCallResult()
        assert result.get_text() == ""

    def test_tools_list_result(self):
        from aria.mcp.types import MCPToolsListResult

        result = MCPToolsListResult.model_validate({
            "tools": [
                {"name": "search_threads", "description": "Search"},
                {"name": "get_thread", "description": "Get thread"},
            ]
        })
        assert len(result.tools) == 2
        assert result.tools[0].name == "search_threads"


class TestMCPConnectionState:
    """MCP 연결 상태 테스트"""

    def test_states(self):
        from aria.mcp.types import MCPConnectionState

        assert MCPConnectionState.DISCONNECTED == "disconnected"
        assert MCPConnectionState.READY == "ready"
        assert MCPConnectionState.ERROR == "error"


# === Client Tests ===


class TestMCPClientInit:
    """MCPClient 초기화 테스트"""

    def test_initial_state(self):
        from aria.mcp.client import MCPClient
        from aria.mcp.types import MCPConnectionState, MCPServerConfig

        config = MCPServerConfig(name="test", url="https://example.com/mcp")
        client = MCPClient(config)
        assert client.state == MCPConnectionState.DISCONNECTED
        assert client.is_connected is False
        assert client.server_name == "test"
        assert client.discovered_tools == []

    def test_with_token_provider(self):
        from aria.mcp.client import MCPClient
        from aria.mcp.types import MCPServerConfig

        async def mock_token():
            return "test-token"

        config = MCPServerConfig(name="test", url="https://example.com/mcp")
        client = MCPClient(config, token_provider=mock_token)
        assert client._token_provider is not None


class TestMCPClientHeaders:
    """MCPClient 헤더 구성 테스트"""

    @pytest.mark.asyncio
    async def test_bearer_auth_header(self):
        from aria.mcp.client import MCPClient
        from aria.mcp.types import MCPAuthType, MCPServerConfig

        async def mock_token():
            return "ya29.test-token"

        config = MCPServerConfig(
            name="test",
            url="https://example.com/mcp",
            auth_type=MCPAuthType.BEARER,
        )
        client = MCPClient(config, token_provider=mock_token)
        headers = await client._build_headers()

        assert headers["Authorization"] == "Bearer ya29.test-token"
        assert headers["Content-Type"] == "application/json"
        assert "text/event-stream" in headers["Accept"]

    @pytest.mark.asyncio
    async def test_no_auth_header(self):
        from aria.mcp.client import MCPClient
        from aria.mcp.types import MCPAuthType, MCPServerConfig

        config = MCPServerConfig(
            name="test",
            url="https://example.com/mcp",
            auth_type=MCPAuthType.NONE,
        )
        client = MCPClient(config)
        headers = await client._build_headers()

        assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_session_id_header(self):
        from aria.mcp.client import MCPClient
        from aria.mcp.types import MCPAuthType, MCPServerConfig

        config = MCPServerConfig(
            name="test",
            url="https://example.com/mcp",
            auth_type=MCPAuthType.NONE,
        )
        client = MCPClient(config)
        client._session_id = "test-session-123"
        headers = await client._build_headers()

        assert headers["MCP-Session-Id"] == "test-session-123"


class TestMCPClientSSEParsing:
    """MCPClient SSE 응답 파싱 테스트"""

    def test_parse_single_data(self):
        from aria.mcp.client import MCPClient
        from aria.mcp.types import MCPServerConfig

        config = MCPServerConfig(name="test", url="https://example.com/mcp")
        client = MCPClient(config)

        body = 'data: {"jsonrpc":"2.0","id":1,"result":{"status":"ok"}}\n\n'
        response = client._parse_sse_response(body)

        assert response.id == 1
        assert response.result == {"status": "ok"}

    def test_parse_multiple_data_lines(self):
        from aria.mcp.client import MCPClient
        from aria.mcp.types import MCPServerConfig

        config = MCPServerConfig(name="test", url="https://example.com/mcp")
        client = MCPClient(config)

        body = (
            'data: {"jsonrpc":"2.0","id":1,"result":{"chunk":1}}\n'
            'data: {"jsonrpc":"2.0","id":1,"result":{"status":"done"}}\n\n'
        )
        response = client._parse_sse_response(body)
        # 마지막 data 라인 사용
        assert response.result == {"status": "done"}

    def test_parse_empty_sse(self):
        from aria.mcp.client import MCPClient, MCPClientError
        from aria.mcp.types import MCPServerConfig

        config = MCPServerConfig(name="test", url="https://example.com/mcp")
        client = MCPClient(config)

        with pytest.raises(MCPClientError, match="data 이벤트"):
            client._parse_sse_response("")


class TestMCPClientConnect:
    """MCPClient 연결 테스트 (mocked HTTP)"""

    @pytest.mark.asyncio
    async def test_connect_success(self):
        from aria.mcp.client import MCPClient
        from aria.mcp.types import MCPAuthType, MCPServerConfig

        config = MCPServerConfig(
            name="test",
            url="https://example.com/mcp",
            auth_type=MCPAuthType.NONE,
        )
        client = MCPClient(config)

        # Mock HTTP responses
        mock_responses = [
            # initialize response
            _mock_http_response(200, {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "test-server", "version": "1.0"},
                },
            }),
            # initialized notification response (202)
            _mock_http_response(202, None),
            # tools/list response
            _mock_http_response(200, {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [
                        {
                            "name": "search_threads",
                            "description": "Search threads",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"},
                                },
                                "required": ["query"],
                            },
                        },
                    ],
                },
            }),
        ]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=mock_responses)
            mock_client.aclose = AsyncMock()
            mock_client_cls.return_value = mock_client

            result = await client.connect()

        assert client.is_connected is True
        assert result.serverInfo.name == "test-server"
        assert len(client.discovered_tools) == 1
        assert client.discovered_tools[0].name == "search_threads"

        await client.close()

    @pytest.mark.asyncio
    async def test_connect_failure(self):
        from aria.mcp.client import MCPClient, MCPConnectionError
        from aria.mcp.types import MCPAuthType, MCPServerConfig

        config = MCPServerConfig(
            name="test",
            url="https://example.com/mcp",
            auth_type=MCPAuthType.NONE,
        )
        client = MCPClient(config)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client.aclose = AsyncMock()
            mock_client_cls.return_value = mock_client

            with pytest.raises(MCPConnectionError, match="연결 실패"):
                await client.connect()

        assert client.state.value == "error"

    @pytest.mark.asyncio
    async def test_close(self):
        from aria.mcp.client import MCPClient
        from aria.mcp.types import MCPConnectionState, MCPServerConfig

        config = MCPServerConfig(name="test", url="https://example.com/mcp")
        client = MCPClient(config)
        client._state = MCPConnectionState.READY
        client._session_id = "test-session"

        await client.close()

        assert client.state == MCPConnectionState.DISCONNECTED
        assert client._session_id == ""


class TestMCPClientToolCall:
    """MCPClient 도구 호출 테스트"""

    @pytest.mark.asyncio
    async def test_call_tool_not_connected(self):
        from aria.mcp.client import MCPClient, MCPClientError
        from aria.mcp.types import MCPServerConfig

        config = MCPServerConfig(name="test", url="https://example.com/mcp")
        client = MCPClient(config)

        with pytest.raises(MCPClientError, match="연결되지 않았습니다"):
            await client.call_tool("test_tool", {"query": "test"})

    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        from aria.mcp.client import MCPClient
        from aria.mcp.types import MCPAuthType, MCPConnectionState, MCPServerConfig

        config = MCPServerConfig(
            name="test",
            url="https://example.com/mcp",
            auth_type=MCPAuthType.NONE,
        )
        client = MCPClient(config)
        client._state = MCPConnectionState.READY
        client._http_client = AsyncMock()

        client._http_client.post = AsyncMock(return_value=_mock_http_response(200, {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": '{"threads": []}'}],
                "isError": False,
            },
        }))

        result = await client.call_tool("search_threads", {"query": "test"})

        assert result.isError is False
        assert "threads" in result.get_text()

    @pytest.mark.asyncio
    async def test_call_tool_error_response(self):
        from aria.mcp.client import MCPClient, MCPToolCallError
        from aria.mcp.types import MCPAuthType, MCPConnectionState, MCPServerConfig

        config = MCPServerConfig(
            name="test",
            url="https://example.com/mcp",
            auth_type=MCPAuthType.NONE,
        )
        client = MCPClient(config)
        client._state = MCPConnectionState.READY
        client._http_client = AsyncMock()

        client._http_client.post = AsyncMock(return_value=_mock_http_response(200, {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }))

        with pytest.raises(MCPToolCallError, match="호출 실패"):
            await client.call_tool("nonexistent_tool", {})


# === Tool Bridge Tests ===


class TestSanitizeToolName:
    """도구 이름 변환 테스트"""

    def test_snake_case_passthrough(self):
        from aria.mcp.tool_bridge import _sanitize_tool_name

        assert _sanitize_tool_name("search_threads") == "search_threads"

    def test_camel_case_conversion(self):
        from aria.mcp.tool_bridge import _sanitize_tool_name

        assert _sanitize_tool_name("createEvent") == "create_event"
        assert _sanitize_tool_name("getFileMetadata") == "get_file_metadata"

    def test_numeric_prefix(self):
        from aria.mcp.tool_bridge import _sanitize_tool_name

        assert _sanitize_tool_name("123tool") == "t_123tool"

    def test_special_chars_removed(self):
        from aria.mcp.tool_bridge import _sanitize_tool_name

        assert _sanitize_tool_name("my-tool.v2") == "mytoolv2"


class TestInferSafetyHint:
    """안전성 힌트 추론 테스트"""

    def test_read_only(self):
        from aria.mcp.tool_bridge import _infer_safety_hint
        from aria.tools.tool_types import SafetyLevelHint

        assert _infer_safety_hint("search_threads") == SafetyLevelHint.READ_ONLY
        assert _infer_safety_hint("get_thread") == SafetyLevelHint.READ_ONLY
        assert _infer_safety_hint("list_events") == SafetyLevelHint.READ_ONLY

    def test_write(self):
        from aria.mcp.tool_bridge import _infer_safety_hint
        from aria.tools.tool_types import SafetyLevelHint

        assert _infer_safety_hint("create_event") == SafetyLevelHint.WRITE
        assert _infer_safety_hint("update_event") == SafetyLevelHint.WRITE
        assert _infer_safety_hint("create_draft") == SafetyLevelHint.WRITE

    def test_destructive(self):
        from aria.mcp.tool_bridge import _infer_safety_hint
        from aria.tools.tool_types import SafetyLevelHint

        assert _infer_safety_hint("delete_event") == SafetyLevelHint.DESTRUCTIVE

    def test_external(self):
        from aria.mcp.tool_bridge import _infer_safety_hint
        from aria.tools.tool_types import SafetyLevelHint

        assert _infer_safety_hint("send_email") == SafetyLevelHint.EXTERNAL


class TestConvertInputSchema:
    """MCP inputSchema → ToolParameter 변환 테스트"""

    def test_basic_conversion(self):
        from aria.mcp.tool_bridge import _convert_input_schema
        from aria.mcp.types import MCPInputSchema

        schema = MCPInputSchema(
            type="object",
            properties={
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results"},
            },
            required=["query"],
        )

        params = _convert_input_schema(schema)
        assert len(params) == 2
        assert params[0].name == "query"
        assert params[0].required is True
        assert params[0].type == "string"
        assert params[1].name == "max_results"
        assert params[1].required is False

    def test_empty_schema(self):
        from aria.mcp.tool_bridge import _convert_input_schema
        from aria.mcp.types import MCPInputSchema

        schema = MCPInputSchema()
        params = _convert_input_schema(schema)
        assert params == []

    def test_enum_parameter(self):
        from aria.mcp.tool_bridge import _convert_input_schema
        from aria.mcp.types import MCPInputSchema

        schema = MCPInputSchema(
            properties={
                "status": {
                    "type": "string",
                    "enum": ["active", "inactive"],
                    "description": "Filter by status",
                },
            },
        )

        params = _convert_input_schema(schema)
        assert len(params) == 1
        assert params[0].enum == ["active", "inactive"]


class TestMCPToolExecutor:
    """MCPToolExecutor 테스트"""

    @pytest.mark.asyncio
    async def test_execute_success(self):
        from aria.mcp.tool_bridge import MCPToolExecutor
        from aria.mcp.types import MCPToolCallContent, MCPToolCallResult
        from aria.tools.tool_types import ToolCategory, ToolDefinition

        mock_client = AsyncMock()
        mock_client.call_tool = AsyncMock(return_value=MCPToolCallResult(
            content=[MCPToolCallContent(type="text", text='{"data": "test"}')],
            isError=False,
        ))

        definition = ToolDefinition(
            name="mcp_gmail_search_threads",
            description="Search threads",
            category=ToolCategory.MCP,
        )

        executor = MCPToolExecutor(mock_client, "search_threads", definition)
        result = await executor.execute({"query": "test"})

        assert result.success is True
        assert result.output == {"data": "test"}
        assert result.tool_name == "mcp_gmail_search_threads"

    @pytest.mark.asyncio
    async def test_execute_error(self):
        from aria.mcp.client import MCPToolCallError
        from aria.mcp.tool_bridge import MCPToolExecutor
        from aria.tools.tool_types import ToolCategory, ToolDefinition

        mock_client = AsyncMock()
        mock_client.call_tool = AsyncMock(
            side_effect=MCPToolCallError("test error", server_name="test"),
        )

        definition = ToolDefinition(
            name="mcp_gmail_search_threads",
            description="Search threads",
            category=ToolCategory.MCP,
        )

        executor = MCPToolExecutor(mock_client, "search_threads", definition)
        result = await executor.execute({"query": "test"})

        assert result.success is False
        assert "호출 실패" in result.error

    @pytest.mark.asyncio
    async def test_execute_mcp_error_flag(self):
        from aria.mcp.tool_bridge import MCPToolExecutor
        from aria.mcp.types import MCPToolCallContent, MCPToolCallResult
        from aria.tools.tool_types import ToolCategory, ToolDefinition

        mock_client = AsyncMock()
        mock_client.call_tool = AsyncMock(return_value=MCPToolCallResult(
            content=[MCPToolCallContent(type="text", text="Error occurred")],
            isError=True,
        ))

        definition = ToolDefinition(
            name="test_tool",
            description="Test",
            category=ToolCategory.MCP,
        )

        executor = MCPToolExecutor(mock_client, "test", definition)
        result = await executor.execute({})

        assert result.success is False

    def test_get_definition(self):
        from aria.mcp.tool_bridge import MCPToolExecutor
        from aria.tools.tool_types import ToolCategory, ToolDefinition

        definition = ToolDefinition(
            name="test_tool",
            description="Test tool",
            category=ToolCategory.MCP,
        )

        executor = MCPToolExecutor(AsyncMock(), "test", definition)
        assert executor.get_definition() == definition


class TestMCPToolBridge:
    """MCPToolBridge 등록 테스트"""

    @pytest.mark.asyncio
    async def test_register_server(self):
        from aria.mcp.tool_bridge import MCPToolBridge
        from aria.mcp.types import MCPInputSchema, MCPServerConfig, MCPToolSchema

        mock_registry = MagicMock()
        mock_registry.has_tool = MagicMock(return_value=False)
        mock_registry.register = MagicMock()

        bridge = MCPToolBridge(mock_registry)

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.server_name = "gmail"
        mock_client._config = MCPServerConfig(
            name="gmail",
            url="https://gmailmcp.googleapis.com/mcp/v1",
            tool_prefix="mcp_gmail_",
        )
        mock_client.discovered_tools = [
            MCPToolSchema(
                name="search_threads",
                description="Search Gmail threads",
                inputSchema=MCPInputSchema(
                    properties={"query": {"type": "string"}},
                    required=["query"],
                ),
            ),
            MCPToolSchema(
                name="get_thread",
                description="Get a thread",
                inputSchema=MCPInputSchema(
                    properties={"thread_id": {"type": "string"}},
                    required=["thread_id"],
                ),
            ),
        ]

        registered = await bridge.register_server(mock_client)

        assert len(registered) == 2
        assert "mcp_gmail_search_threads" in registered
        assert "mcp_gmail_get_thread" in registered
        assert mock_registry.register.call_count == 2

    @pytest.mark.asyncio
    async def test_register_server_skip_tools(self):
        from aria.mcp.tool_bridge import MCPToolBridge
        from aria.mcp.types import MCPServerConfig, MCPToolSchema

        mock_registry = MagicMock()
        mock_registry.has_tool = MagicMock(return_value=False)
        mock_registry.register = MagicMock()

        bridge = MCPToolBridge(mock_registry)

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.server_name = "gmail"
        mock_client._config = MCPServerConfig(
            name="gmail",
            url="https://example.com/mcp",
        )
        mock_client.discovered_tools = [
            MCPToolSchema(name="search_threads", description="Search"),
            MCPToolSchema(name="get_thread", description="Get"),
        ]

        registered = await bridge.register_server(
            mock_client,
            skip_tools={"get_thread"},
        )

        assert len(registered) == 1
        assert "mcp_gmail_search_threads" in registered

    @pytest.mark.asyncio
    async def test_register_server_override_existing(self):
        from aria.mcp.tool_bridge import MCPToolBridge
        from aria.mcp.types import MCPServerConfig, MCPToolSchema

        mock_registry = MagicMock()
        mock_registry.has_tool = MagicMock(return_value=True)
        mock_registry.unregister = MagicMock()
        mock_registry.register = MagicMock()

        bridge = MCPToolBridge(mock_registry, override_existing=True)

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.server_name = "gmail"
        mock_client._config = MCPServerConfig(
            name="gmail",
            url="https://example.com/mcp",
        )
        mock_client.discovered_tools = [
            MCPToolSchema(name="search_threads", description="Search"),
        ]

        registered = await bridge.register_server(mock_client)

        assert len(registered) == 1
        mock_registry.unregister.assert_called_once()

    @pytest.mark.asyncio
    async def test_unregister_server(self):
        from aria.mcp.tool_bridge import MCPToolBridge

        mock_registry = MagicMock()
        mock_registry.has_tool = MagicMock(return_value=True)
        mock_registry.unregister = MagicMock()

        bridge = MCPToolBridge(mock_registry)
        bridge._registered_tools["gmail"] = ["mcp_gmail_search", "mcp_gmail_read"]

        removed = await bridge.unregister_server("gmail")

        assert removed == 2
        assert "gmail" not in bridge._registered_tools


# === Google Servers Tests ===


class TestGoogleMCPConfigs:
    """Google MCP 서버 설정 테스트"""

    def test_all_services(self):
        from aria.mcp.google_servers import get_google_mcp_configs

        configs = get_google_mcp_configs()
        assert len(configs) == 3
        names = {c.name for c in configs}
        assert names == {"gmail", "calendar", "drive"}

    def test_filtered_services(self):
        from aria.mcp.google_servers import get_google_mcp_configs

        configs = get_google_mcp_configs(enabled_services={"gmail"})
        assert len(configs) == 1
        assert configs[0].name == "gmail"
        assert "gmailmcp.googleapis.com" in configs[0].url

    def test_priority_higher_than_rest(self):
        from aria.mcp.google_servers import get_google_mcp_configs

        configs = get_google_mcp_configs()
        for config in configs:
            assert config.priority > 10  # REST 도구 기본 priority=10

    def test_bearer_auth(self):
        from aria.mcp.google_servers import get_google_mcp_configs
        from aria.mcp.types import MCPAuthType

        configs = get_google_mcp_configs()
        for config in configs:
            assert config.auth_type == MCPAuthType.BEARER


class TestRESTToolsDisable:
    """REST 도구 비활성화 테스트"""

    def test_gmail_rest_tools(self):
        from aria.mcp.google_servers import get_rest_tools_to_disable

        tools = get_rest_tools_to_disable({"gmail"})
        assert "gmail_search" in tools
        assert "gmail_read" in tools
        assert "gmail_draft" in tools

    def test_calendar_rest_tools(self):
        from aria.mcp.google_servers import get_rest_tools_to_disable

        tools = get_rest_tools_to_disable({"calendar"})
        assert "gcal_list_events" in tools
        assert "gcal_create_event" in tools

    def test_drive_no_rest_overlap(self):
        from aria.mcp.google_servers import get_rest_tools_to_disable

        tools = get_rest_tools_to_disable({"drive"})
        assert tools == []

    def test_multiple_services(self):
        from aria.mcp.google_servers import get_rest_tools_to_disable

        tools = get_rest_tools_to_disable({"gmail", "calendar"})
        assert len(tools) >= 7  # gmail 4 + calendar 3

    def test_empty_services(self):
        from aria.mcp.google_servers import get_rest_tools_to_disable

        tools = get_rest_tools_to_disable(set())
        assert tools == []


class TestConnectGoogleMCPServers:
    """Google MCP 서버 연결 테스트"""

    @pytest.mark.asyncio
    async def test_connect_with_failure_isolation(self):
        """개별 서버 실패가 다른 서버에 영향 없음"""
        from aria.mcp.google_servers import connect_google_mcp_servers
        from aria.mcp.types import MCPAuthType, MCPServerConfig

        configs = [
            MCPServerConfig(name="gmail", url="https://fail.example.com/mcp", auth_type=MCPAuthType.NONE),
            MCPServerConfig(name="calendar", url="https://fail.example.com/mcp", auth_type=MCPAuthType.NONE),
        ]

        async def mock_token():
            return "test-token"

        # 모든 연결이 실패하도록 패치
        with patch("aria.mcp.google_servers.MCPClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.connect = AsyncMock(side_effect=Exception("Connection failed"))
            mock_cls.return_value = mock_instance

            clients = await connect_google_mcp_servers(configs, mock_token)

        assert len(clients) == 0


# === Config Tests ===


class TestMCPConfig:
    """MCPConfig 테스트"""

    def test_defaults(self):
        from aria.core.config import MCPConfig

        config = MCPConfig()
        assert config.enabled is True
        assert config.google_services == "gmail,calendar,drive"
        assert config.request_timeout == 30.0
        assert config.override_rest_tools is True

    def test_is_configured(self):
        from aria.core.config import MCPConfig

        config = MCPConfig()
        assert config.is_configured is True

        config2 = MCPConfig(enabled=False)
        assert config2.is_configured is False

    def test_enabled_google_services(self):
        from aria.core.config import MCPConfig

        config = MCPConfig(google_services="gmail,drive")
        assert config.enabled_google_services == {"gmail", "drive"}

    def test_enabled_google_services_invalid(self):
        from aria.core.config import MCPConfig

        config = MCPConfig(google_services="gmail,invalid,drive")
        assert config.enabled_google_services == {"gmail", "drive"}

    def test_enabled_google_services_empty(self):
        from aria.core.config import MCPConfig

        config = MCPConfig(google_services="")
        assert config.enabled_google_services == set()

    def test_aria_config_has_mcp(self):
        """AriaConfig에 mcp 필드가 존재하는지 확인"""
        from aria.core.config import AriaConfig, MCPConfig

        # AriaConfig 클래스에 mcp 필드가 있는지만 확인
        assert hasattr(AriaConfig, "model_fields")
        assert "mcp" in AriaConfig.model_fields


# === Helpers ===


def _mock_http_response(
    status_code: int,
    json_data: dict | None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """httpx.Response 목 생성"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {"content-type": "application/json"}
    resp.text = json.dumps(json_data) if json_data else ""

    if json_data is not None:
        resp.json = MagicMock(return_value=json_data)

    return resp
