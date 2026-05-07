"""ARIA Engine - Supabase MCP Server Configuration

Supabase 공식 MCP 서버 연결 설정
- URL: https://mcp.supabase.com/mcp?project_ref=<ref>&read_only=true
- 인증: PAT (Personal Access Token) → Bearer header
- read_only=true 필수 (프로덕션 데이터 안전)

사용법:
    config = get_supabase_mcp_config(project_ref, access_token)
    client = await connect_supabase_mcp(config, access_token)
"""

from __future__ import annotations

from typing import Any

import structlog

from aria.mcp.client import MCPClient, MCPConnectionError
from aria.mcp.types import MCPAuthType, MCPServerConfig, MCPTransport

logger = structlog.get_logger()

SUPABASE_MCP_BASE_URL = "https://mcp.supabase.com/mcp"


def get_supabase_mcp_config(
    project_ref: str,
    project_name: str = "supabase",
    read_only: bool = True,
    features: str = "",
) -> MCPServerConfig:
    """단일 Supabase MCP 서버 설정 생성

    Args:
        project_ref: Supabase 프로젝트 ID
        project_name: 서버 이름 (복수 프로젝트 구분용)
        read_only: 읽기 전용 모드 (기본 True — 필수 권장)
        features: 활성화할 기능 그룹

    Returns:
        MCPServerConfig
    """
    params: list[str] = [f"project_ref={project_ref}"]
    if read_only:
        params.append("read_only=true")
    if features:
        params.append(f"features={features}")

    url = f"{SUPABASE_MCP_BASE_URL}?{'&'.join(params)}"

    return MCPServerConfig(
        name=f"supabase_{project_name}",
        url=url,
        transport=MCPTransport.HTTP,
        auth_type=MCPAuthType.API_KEY,
        enabled=True,
        timeout=30.0,
        priority=15,
        tool_prefix=f"mcp_sb_{project_name}_",
    )


def get_supabase_mcp_configs(
    projects: dict[str, str],
    read_only: bool = True,
) -> list[MCPServerConfig]:
    """복수 프로젝트 MCP 설정 리스트 생성

    Args:
        projects: {이름: project_ref} 매핑
        read_only: 읽기 전용 모드

    Returns:
        MCPServerConfig 리스트
    """
    return [
        get_supabase_mcp_config(
            project_ref=ref,
            project_name=name,
            read_only=read_only,
        )
        for name, ref in projects.items()
    ]


async def connect_supabase_mcp(
    config: MCPServerConfig,
    access_token: str,
) -> MCPClient | None:
    """단일 Supabase MCP 서버 연결

    Args:
        config: MCPServerConfig
        access_token: Supabase PAT (Personal Access Token)

    Returns:
        연결된 MCPClient (실패 시 None)
    """
    client = MCPClient(
        config=config,
        api_key=access_token,
    )

    try:
        init_result = await client.connect()
        logger.info(
            "supabase_mcp_connected",
            server_name=config.name,
            tools=len(client.discovered_tools),
            read_only="read_only=true" in config.url,
        )
        return client
    except MCPConnectionError as e:
        logger.error(
            "supabase_mcp_connection_failed",
            server=config.name,
            error=str(e)[:300],
        )
        return None
    except Exception as e:
        logger.error(
            "supabase_mcp_unexpected_error",
            server=config.name,
            error=str(e)[:300],
        )
        return None


async def connect_supabase_mcp_servers(
    configs: list[MCPServerConfig],
    access_token: str,
) -> list[MCPClient]:
    """복수 Supabase MCP 서버 순차 연결

    개별 실패 시 해당 서버만 스킵

    Args:
        configs: MCPServerConfig 리스트
        access_token: Supabase PAT (전체 공유)

    Returns:
        연결 성공한 MCPClient 리스트
    """
    connected: list[MCPClient] = []

    for config in configs:
        if not config.enabled:
            continue
        client = await connect_supabase_mcp(config, access_token)
        if client:
            connected.append(client)

    logger.info(
        "supabase_mcp_servers_done",
        connected=len(connected),
        total=len(configs),
    )
    return connected
