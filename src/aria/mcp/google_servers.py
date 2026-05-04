"""ARIA Engine - Google MCP Server Configurations

Google Workspace 공식 MCP 서버 연결 설정 + 초기화 헬퍼
- Gmail: https://gmailmcp.googleapis.com/mcp/v1 (10 tools)
- Calendar: https://calendarmcp.googleapis.com/mcp/v1 (8 tools)
- Drive: https://drivemcp.googleapis.com/mcp/v1 (7 tools)

사용법:
    configs = get_google_mcp_configs(enabled_services={"gmail", "calendar", "drive"})
    clients = await connect_google_mcp_servers(configs, token_provider)
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

import structlog

from aria.mcp.client import MCPClient, MCPConnectionError, TokenProvider
from aria.mcp.types import MCPAuthType, MCPServerConfig, MCPTransport

logger = structlog.get_logger()


# === Google MCP Server URLs ===

GOOGLE_MCP_SERVERS: dict[str, str] = {
    "gmail": "https://gmailmcp.googleapis.com/mcp/v1",
    "calendar": "https://calendarmcp.googleapis.com/mcp/v1",
    "drive": "https://drivemcp.googleapis.com/mcp/v1",
}


# === 기존 REST 도구와 중복되는 MCP 도구 매핑 (MCP 우선 시 REST 도구 비활성화 대상) ===
REST_TO_MCP_OVERLAP: dict[str, list[str]] = {
    "gmail": [
        "gmail_search",     # REST → mcp_gmail_search_threads
        "gmail_read",       # REST → mcp_gmail_get_thread
        "gmail_send",       # REST → (없음 — Gmail MCP는 send 미제공)
        "gmail_draft",      # REST → mcp_gmail_create_draft
    ],
    "calendar": [
        "gcal_list_events",   # REST → mcp_calendar_list_events
        "gcal_create_event",  # REST → mcp_calendar_create_event
        "gcal_update_event",  # REST → mcp_calendar_update_event
    ],
}


def get_google_mcp_configs(
    enabled_services: set[str] | None = None,
) -> list[MCPServerConfig]:
    """Google MCP 서버 설정 목록 생성

    Args:
        enabled_services: 활성화할 서비스 세트 (None이면 전체)
            가능한 값: {"gmail", "calendar", "drive"}

    Returns:
        MCPServerConfig 리스트
    """
    services = enabled_services or set(GOOGLE_MCP_SERVERS.keys())
    configs: list[MCPServerConfig] = []

    for service_name, url in GOOGLE_MCP_SERVERS.items():
        if service_name not in services:
            continue

        configs.append(MCPServerConfig(
            name=service_name,
            url=url,
            transport=MCPTransport.HTTP,
            auth_type=MCPAuthType.BEARER,
            enabled=True,
            timeout=30.0,
            priority=20,  # REST 도구(기본 10)보다 높은 우선순위
            tool_prefix=f"mcp_{service_name}_",
        ))

    return configs


async def connect_google_mcp_servers(
    configs: list[MCPServerConfig],
    token_provider: TokenProvider,
) -> list[MCPClient]:
    """Google MCP 서버들에 순차 연결

    개별 서버 연결 실패 시 해당 서버만 스킵 (다른 서버에 영향 없음)

    Args:
        configs: MCPServerConfig 리스트
        token_provider: OAuth2 access_token 제공 비동기 함수

    Returns:
        연결 성공한 MCPClient 리스트
    """
    connected_clients: list[MCPClient] = []

    for config in configs:
        if not config.enabled:
            continue

        client = MCPClient(
            config=config,
            token_provider=token_provider,
        )

        try:
            init_result = await client.connect()
            connected_clients.append(client)
            logger.info(
                "google_mcp_server_connected",
                service=config.name,
                server_name=init_result.serverInfo.name,
                tools=len(client.discovered_tools),
            )
        except MCPConnectionError as e:
            logger.error(
                "google_mcp_server_connection_failed",
                service=config.name,
                error=str(e)[:200],
            )
        except Exception as e:
            logger.error(
                "google_mcp_server_unexpected_error",
                service=config.name,
                error=str(e)[:200],
            )

    return connected_clients


def get_rest_tools_to_disable(
    connected_services: set[str],
) -> list[str]:
    """MCP 연결 성공한 서비스의 기존 REST 도구 이름 목록 반환

    MCP 우선 정책: 동일 서비스의 REST 도구는 비활성화 or 미등록 대상

    Args:
        connected_services: MCP 연결 성공한 서비스 이름 세트

    Returns:
        비활성화할 REST 도구 이름 리스트
    """
    disable_list: list[str] = []

    for service_name in connected_services:
        if service_name in REST_TO_MCP_OVERLAP:
            disable_list.extend(REST_TO_MCP_OVERLAP[service_name])

    return disable_list
