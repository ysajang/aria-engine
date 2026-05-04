"""ARIA Engine - Generic MCP Client

범용 MCP 클라이언트 (HTTP Streamable Transport)
- JSON-RPC 2.0 over HTTP POST
- OAuth2 Bearer token 자동 주입
- 세션 관리 (MCP-Session-Id)
- 도구 자동 발견 (tools/list)
- 도구 호출 (tools/call)
- 재시도 + 에러 핸들링

사용법:
    client = MCPClient(config, token_provider=token_mgr.get_access_token)
    await client.connect()
    tools = await client.list_tools()
    result = await client.call_tool("search_threads", {"query": "from:ariel"})
    await client.close()
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Awaitable

import httpx
import structlog

from aria.mcp.types import (
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    MCPClientInfo,
    MCPConnectionState,
    MCPInitializeResult,
    MCPServerConfig,
    MCPToolCallResult,
    MCPToolSchema,
    MCPToolsListResult,
    MCPAuthType,
)

logger = structlog.get_logger()

# MCP 프로토콜 버전 (2025-03-26이 현재 안정 버전)
MCP_PROTOCOL_VERSION = "2025-03-26"

# JSON-RPC 에러 코드
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INTERNAL_ERROR = -32603


class MCPClientError(Exception):
    """MCP 클라이언트 에러"""

    def __init__(self, message: str, code: int = 0, server_name: str = "") -> None:
        self.code = code
        self.server_name = server_name
        super().__init__(message)


class MCPConnectionError(MCPClientError):
    """MCP 서버 연결 실패"""
    pass


class MCPToolCallError(MCPClientError):
    """MCP 도구 호출 실패"""
    pass


# Token provider 타입: 비동기 함수 → access_token 문자열 반환
TokenProvider = Callable[[], Awaitable[str]]


class MCPClient:
    """범용 MCP 클라이언트

    HTTP Streamable Transport 기반 MCP 서버 연결
    - initialize → initialized → tools/list → tools/call
    - Bearer token 자동 주입 (OAuth2)
    - 세션 ID 관리 (MCP-Session-Id)

    Args:
        config: MCP 서버 연결 설정
        token_provider: access_token 반환 비동기 함수 (Bearer 인증 시 필수)
        api_key: API Key 인증 시 키 값
    """

    def __init__(
        self,
        config: MCPServerConfig,
        token_provider: TokenProvider | None = None,
        api_key: str = "",
    ) -> None:
        self._config = config
        self._token_provider = token_provider
        self._api_key = api_key

        # 상태
        self._state = MCPConnectionState.DISCONNECTED
        self._session_id: str = ""
        self._request_id: int = 0
        self._server_info: MCPInitializeResult | None = None
        self._discovered_tools: list[MCPToolSchema] = []

        # HTTP 클라이언트 (lazy init)
        self._http_client: httpx.AsyncClient | None = None

    # === Properties ===

    @property
    def state(self) -> MCPConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == MCPConnectionState.READY

    @property
    def server_name(self) -> str:
        return self._config.name

    @property
    def server_info(self) -> MCPInitializeResult | None:
        return self._server_info

    @property
    def discovered_tools(self) -> list[MCPToolSchema]:
        return self._discovered_tools

    # === Lifecycle ===

    async def connect(self) -> MCPInitializeResult:
        """MCP 서버에 연결 (initialize + initialized + tools/list)

        Returns:
            MCPInitializeResult: 서버 정보 + capabilities

        Raises:
            MCPConnectionError: 연결 실패
        """
        if self._state == MCPConnectionState.READY:
            return self._server_info  # type: ignore

        self._state = MCPConnectionState.INITIALIZING
        self._http_client = httpx.AsyncClient(timeout=self._config.timeout)

        try:
            # Step 1: initialize
            init_result = await self._initialize()
            self._server_info = init_result

            # Step 2: initialized (notification — 응답 불필요)
            await self._send_notification("initialized")

            # Step 3: tools/list (서버가 tools capability를 지원하면)
            if init_result.capabilities.has_tools:
                self._discovered_tools = await self._list_tools()
            else:
                self._discovered_tools = []

            self._state = MCPConnectionState.READY
            logger.info(
                "mcp_client_connected",
                server=self._config.name,
                server_version=init_result.serverInfo.version,
                protocol_version=init_result.protocolVersion,
                tools_discovered=len(self._discovered_tools),
                session_id=self._session_id[:12] if self._session_id else "none",
            )
            return init_result

        except Exception as e:
            self._state = MCPConnectionState.ERROR
            await self._close_http_client()
            raise MCPConnectionError(
                f"MCP 서버 '{self._config.name}' 연결 실패: {e}",
                server_name=self._config.name,
            ) from e

    async def close(self) -> None:
        """MCP 연결 종료"""
        await self._close_http_client()
        self._state = MCPConnectionState.DISCONNECTED
        self._session_id = ""
        self._server_info = None
        self._discovered_tools = []
        logger.info("mcp_client_disconnected", server=self._config.name)

    # === Tool Operations ===

    async def list_tools(self) -> list[MCPToolSchema]:
        """서버의 도구 목록 조회 (캐시된 결과 또는 재조회)

        Returns:
            list[MCPToolSchema]: 사용 가능한 도구 목록
        """
        if not self.is_connected:
            raise MCPClientError(
                f"서버 '{self._config.name}'에 연결되지 않았습니다",
                server_name=self._config.name,
            )
        return self._discovered_tools

    async def refresh_tools(self) -> list[MCPToolSchema]:
        """도구 목록 강제 재조회

        서버가 tools/listChanged 알림을 보낸 경우 등에 사용
        """
        if not self.is_connected:
            raise MCPClientError(
                f"서버 '{self._config.name}'에 연결되지 않았습니다",
                server_name=self._config.name,
            )
        self._discovered_tools = await self._list_tools()
        return self._discovered_tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> MCPToolCallResult:
        """MCP 도구 호출

        Args:
            name: 도구 이름 (MCP 서버에 등록된 원본 이름)
            arguments: 도구 파라미터

        Returns:
            MCPToolCallResult: 호출 결과

        Raises:
            MCPToolCallError: 도구 호출 실패
        """
        if not self.is_connected:
            raise MCPClientError(
                f"서버 '{self._config.name}'에 연결되지 않았습니다",
                server_name=self._config.name,
            )

        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments

        try:
            response = await self._send_request("tools/call", params)

            if response.is_error:
                error = response.error
                assert error is not None
                raise MCPToolCallError(
                    f"도구 '{name}' 호출 실패: {error.message}",
                    code=error.code,
                    server_name=self._config.name,
                )

            result = MCPToolCallResult.model_validate(response.result or {})

            logger.debug(
                "mcp_tool_called",
                server=self._config.name,
                tool=name,
                is_error=result.isError,
            )
            return result

        except MCPToolCallError:
            raise
        except Exception as e:
            raise MCPToolCallError(
                f"도구 '{name}' 호출 중 예외: {e}",
                server_name=self._config.name,
            ) from e

    # === Private: Protocol Methods ===

    async def _initialize(self) -> MCPInitializeResult:
        """MCP initialize 핸드셰이크"""
        params = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": MCPClientInfo().model_dump(),
        }

        response = await self._send_request("initialize", params)

        if response.is_error:
            error = response.error
            assert error is not None
            raise MCPConnectionError(
                f"initialize 실패: {error.message} (code={error.code})",
                code=error.code,
                server_name=self._config.name,
            )

        return MCPInitializeResult.model_validate(response.result or {})

    async def _list_tools(self) -> list[MCPToolSchema]:
        """MCP tools/list 호출"""
        response = await self._send_request("tools/list", {})

        if response.is_error:
            error = response.error
            assert error is not None
            logger.warning(
                "mcp_tools_list_failed",
                server=self._config.name,
                error=error.message,
            )
            return []

        result = MCPToolsListResult.model_validate(response.result or {})
        return result.tools

    # === Private: Transport ===

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> JSONRPCResponse:
        """JSON-RPC 2.0 요청 전송 (HTTP POST)

        Args:
            method: JSON-RPC 메서드 (예: "initialize" / "tools/list" / "tools/call")
            params: 요청 파라미터

        Returns:
            JSONRPCResponse: 서버 응답

        Raises:
            MCPClientError: HTTP 에러 또는 파싱 실패
        """
        self._request_id += 1
        request = JSONRPCRequest(
            id=self._request_id,
            method=method,
            params=params,
        )

        headers = await self._build_headers()
        payload = request.model_dump(exclude_none=True)

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                assert self._http_client is not None
                resp = await self._http_client.post(
                    self._config.url,
                    json=payload,
                    headers=headers,
                )

                # 401 → 토큰 무효화 후 재시도 (Bearer 인증 시)
                if resp.status_code == 401 and attempt < max_retries:
                    if self._config.auth_type == MCPAuthType.BEARER:
                        logger.warning(
                            "mcp_auth_401_retry",
                            server=self._config.name,
                            attempt=attempt + 1,
                        )
                        # 토큰 재발급을 위해 헤더 재생성
                        headers = await self._build_headers(force_refresh=True)
                        continue

                # 세션 ID 저장
                session_id = resp.headers.get("mcp-session-id", "")
                if session_id:
                    self._session_id = session_id

                # HTTP 에러 체크
                if resp.status_code >= 400:
                    raise MCPClientError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}",
                        code=resp.status_code,
                        server_name=self._config.name,
                    )

                # Content-Type 확인
                content_type = resp.headers.get("content-type", "")

                if "text/event-stream" in content_type:
                    # SSE 응답 → 마지막 data 이벤트에서 JSON-RPC 응답 추출
                    return self._parse_sse_response(resp.text)

                # JSON 응답
                return JSONRPCResponse.model_validate(resp.json())

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                if attempt < max_retries:
                    wait_time = 2 ** attempt
                    logger.warning(
                        "mcp_request_retry",
                        server=self._config.name,
                        method=method,
                        attempt=attempt + 1,
                        wait=wait_time,
                        error=str(e)[:100],
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise MCPClientError(
                    f"MCP 서버 '{self._config.name}' 통신 실패: {e}",
                    server_name=self._config.name,
                ) from e
            except MCPClientError:
                raise
            except Exception as e:
                raise MCPClientError(
                    f"MCP 요청 처리 실패: {e}",
                    server_name=self._config.name,
                ) from e

        # 이론적으로 도달 불가 (for 루프 내에서 항상 return 또는 raise)
        raise MCPClientError(
            f"MCP 요청 재시도 모두 실패",
            server_name=self._config.name,
        )

    async def _send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """JSON-RPC 2.0 알림 전송 (응답 불필요)

        HTTP 202 Accepted 또는 200 OK 예상
        """
        notification = JSONRPCNotification(
            method=method,
            params=params,
        )

        headers = await self._build_headers()
        payload = notification.model_dump(exclude_none=True)

        try:
            assert self._http_client is not None
            resp = await self._http_client.post(
                self._config.url,
                json=payload,
                headers=headers,
            )

            # 세션 ID 저장
            session_id = resp.headers.get("mcp-session-id", "")
            if session_id:
                self._session_id = session_id

            # 202 Accepted 또는 200 OK 허용
            if resp.status_code not in (200, 202, 204):
                logger.warning(
                    "mcp_notification_unexpected_status",
                    server=self._config.name,
                    method=method,
                    status=resp.status_code,
                )

        except Exception as e:
            # Notification 실패는 치명적이지 않음 → 경고만 로깅
            logger.warning(
                "mcp_notification_failed",
                server=self._config.name,
                method=method,
                error=str(e)[:100],
            )

    # === Private: Headers ===

    async def _build_headers(self, force_refresh: bool = False) -> dict[str, str]:
        """HTTP 요청 헤더 구성

        - Content-Type: application/json
        - Accept: application/json, text/event-stream
        - Authorization: Bearer <token> (Bearer 인증 시)
        - MCP-Session-Id: <session_id> (세션 유지)
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        # 인증 헤더
        if self._config.auth_type == MCPAuthType.BEARER and self._token_provider:
            token = await self._token_provider()
            headers["Authorization"] = f"Bearer {token}"
        elif self._config.auth_type == MCPAuthType.API_KEY and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # 세션 ID
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id

        return headers

    # === Private: SSE Parsing ===

    def _parse_sse_response(self, body: str) -> JSONRPCResponse:
        """SSE 응답에서 마지막 JSON-RPC 응답 추출

        SSE 형식:
            data: {"jsonrpc":"2.0","id":1,"result":{...}}
            data: {"jsonrpc":"2.0","id":1,"result":{...}}

        마지막 data 라인의 JSON을 파싱
        """
        import json

        last_data = ""
        for line in body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("data: "):
                last_data = stripped[6:]
            elif stripped.startswith("data:"):
                last_data = stripped[5:].strip()

        if not last_data:
            raise MCPClientError(
                "SSE 응답에서 data 이벤트를 찾을 수 없습니다",
                server_name=self._config.name,
            )

        try:
            parsed = json.loads(last_data)
            return JSONRPCResponse.model_validate(parsed)
        except (json.JSONDecodeError, Exception) as e:
            raise MCPClientError(
                f"SSE JSON 파싱 실패: {e}",
                server_name=self._config.name,
            ) from e

    # === Private: Cleanup ===

    async def _close_http_client(self) -> None:
        """HTTP 클라이언트 정리"""
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
