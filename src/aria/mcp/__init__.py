"""ARIA Engine - MCP (Model Context Protocol) Client Package

범용 MCP 클라이언트 + Google MCP 서버 연동
- MCPClient: HTTP Streamable 전송 기반 MCP 클라이언트
- MCPToolBridge: MCP 도구 → ToolRegistry 자동 등록 브릿지
- Google MCP Servers: Gmail / Calendar / Drive 공식 MCP 서버
"""

from aria.mcp.client import MCPClient, MCPClientError, MCPConnectionError, MCPToolCallError
from aria.mcp.types import MCPServerConfig, MCPToolSchema, MCPConnectionState
from aria.mcp.tool_bridge import MCPToolBridge, MCPToolExecutor

__all__ = [
    "MCPClient",
    "MCPClientError",
    "MCPConnectionError",
    "MCPToolCallError",
    "MCPServerConfig",
    "MCPToolSchema",
    "MCPConnectionState",
    "MCPToolBridge",
    "MCPToolExecutor",
]
