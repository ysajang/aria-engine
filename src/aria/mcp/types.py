"""ARIA Engine - MCP Protocol Types

JSON-RPC 2.0 메시지 타입 + MCP 도구 스키마 정의
- MCPServerConfig: 서버 연결 설정
- MCPToolSchema: MCP 서버가 반환하는 도구 스키마
- JSON-RPC request/response/notification 구조체

참조: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# === MCP Server Configuration ===

class MCPTransport(str, Enum):
    """MCP 전송 방식"""
    HTTP = "http"            # Streamable HTTP (POST JSON-RPC)
    STDIO = "stdio"          # stdin/stdout (로컬 프로세스)


class MCPAuthType(str, Enum):
    """MCP 인증 방식"""
    NONE = "none"
    BEARER = "bearer"        # OAuth2 Bearer token
    API_KEY = "api_key"      # API Key header


class MCPServerConfig(BaseModel):
    """MCP 서버 연결 설정

    Args:
        name: 서버 식별 이름 (예: "gmail" / "calendar" / "drive")
        url: MCP 서버 엔드포인트 URL (예: https://gmailmcp.googleapis.com/mcp/v1)
        transport: 전송 방식 (현재 HTTP만 지원)
        auth_type: 인증 방식
        enabled: 활성화 여부
        timeout: 요청 타임아웃 (초)
        priority: 동일 서비스 도구 충돌 시 우선순위 (높을수록 우선)
        tool_prefix: 자동 발견된 도구 이름에 붙일 접두사 (예: "mcp_gmail_")
    """
    name: str = Field(..., min_length=1, max_length=50)
    url: str = Field(..., min_length=1)
    transport: MCPTransport = Field(default=MCPTransport.HTTP)
    auth_type: MCPAuthType = Field(default=MCPAuthType.BEARER)
    enabled: bool = Field(default=True)
    timeout: float = Field(default=30.0, ge=5.0, le=120.0)
    priority: int = Field(default=10, ge=0, le=100)
    tool_prefix: str = Field(default="", description="도구 이름 접두사 (빈 문자열이면 서버명 기반 자동 생성)")

    def get_tool_prefix(self) -> str:
        """실제 사용할 도구 접두사 반환"""
        if self.tool_prefix:
            return self.tool_prefix
        return f"mcp_{self.name}_"


# === JSON-RPC 2.0 Types ===

class JSONRPCRequest(BaseModel):
    """JSON-RPC 2.0 요청 메시지"""
    jsonrpc: str = "2.0"
    id: int | str
    method: str
    params: dict[str, Any] | None = None


class JSONRPCNotification(BaseModel):
    """JSON-RPC 2.0 알림 메시지 (응답 불필요)"""
    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any] | None = None


class JSONRPCError(BaseModel):
    """JSON-RPC 2.0 에러 객체"""
    code: int
    message: str
    data: Any = None


class JSONRPCResponse(BaseModel):
    """JSON-RPC 2.0 응답 메시지"""
    jsonrpc: str = "2.0"
    id: int | str | None = None
    result: Any = None
    error: JSONRPCError | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


# === MCP Protocol Types ===

class MCPClientInfo(BaseModel):
    """MCP 클라이언트 정보 (initialize 요청 시 전송)"""
    name: str = "aria-engine"
    version: str = "0.3.0"


class MCPServerInfo(BaseModel):
    """MCP 서버 정보 (initialize 응답에서 수신)"""
    name: str = ""
    version: str = ""


class MCPCapabilities(BaseModel):
    """MCP 서버 capabilities (initialize 응답에서 수신)"""
    tools: dict[str, Any] | None = None
    resources: dict[str, Any] | None = None
    prompts: dict[str, Any] | None = None
    logging: dict[str, Any] | None = None

    @property
    def has_tools(self) -> bool:
        return self.tools is not None


class MCPInitializeResult(BaseModel):
    """initialize 메서드 응답 결과"""
    protocolVersion: str = ""
    capabilities: MCPCapabilities = Field(default_factory=MCPCapabilities)
    serverInfo: MCPServerInfo = Field(default_factory=MCPServerInfo)


# === MCP Tool Schema ===

class MCPInputSchema(BaseModel):
    """MCP 도구 입력 스키마 (JSON Schema 서브셋)"""
    type: str = "object"
    properties: dict[str, Any] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    additionalProperties: bool | None = None
    description: str = ""


class MCPToolSchema(BaseModel):
    """MCP 서버가 반환하는 도구 정의

    tools/list 응답의 각 도구 항목
    """
    name: str
    description: str = ""
    inputSchema: MCPInputSchema = Field(default_factory=MCPInputSchema)

    def get_parameter_names(self) -> list[str]:
        """파라미터 이름 목록"""
        return list(self.inputSchema.properties.keys())


class MCPToolsListResult(BaseModel):
    """tools/list 메서드 응답 결과"""
    tools: list[MCPToolSchema] = Field(default_factory=list)


class MCPToolCallContent(BaseModel):
    """tools/call 응답의 content 항목"""
    type: str = "text"
    text: str = ""


class MCPToolCallResult(BaseModel):
    """tools/call 메서드 응답 결과"""
    content: list[MCPToolCallContent] = Field(default_factory=list)
    isError: bool = False

    def get_text(self) -> str:
        """모든 text content를 합쳐서 반환"""
        parts = [c.text for c in self.content if c.type == "text" and c.text]
        return "\n".join(parts)


# === MCP Connection State ===

class MCPConnectionState(str, Enum):
    """MCP 연결 상태"""
    DISCONNECTED = "disconnected"
    INITIALIZING = "initializing"
    READY = "ready"
    ERROR = "error"
