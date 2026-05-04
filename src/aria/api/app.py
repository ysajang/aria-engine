"""ARIA Engine - FastAPI Application

REST API 엔드포인트
- POST /v1/query → 에이전트에게 질문 (메모리 자동 주입)
- POST /v1/knowledge → 지식 추가
- POST /v1/knowledge/{collection}/search → 벡터 검색
- GET /v1/health → 헬스 체크
- GET /v1/cost → 비용 현황
- GET /v1/memory/{scope}/index → 메모리 인덱스 조회
- GET /v1/memory/{scope}/topics/{domain} → 토픽 조회
- PUT /v1/memory/{scope}/topics/{domain} → 토픽 upsert
- DELETE /v1/memory/{scope}/topics/{domain} → 토픽 삭제
- POST /v1/memory/{scope}/load → 메모리 로딩 (프롬프트 마크다운)
- POST /v1/events → 이벤트 수집 (배치)
- GET /v1/events → 이벤트 조회 (필터링)
- GET /v1/events/stats → 이벤트 통계
- 글로벌 에러 핸들러 → 구조화된 에러 응답
- Rate limiting → 요청 제한
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import structlog
from fastapi import FastAPI, HTTPException, Security, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from aria.core.config import get_config, AriaConfig
from aria.core.exceptions import (
    AriaError,
    KillSwitchError,
    LLMAllProvidersExhaustedError,
    LLMProviderError,
    NoAPIKeyError,
    CollectionNotFoundError,
    VectorStoreError,
    AgentError,
    MemoryError as AriaMemoryError,
    VersionConflictError,
    MemoryNotFoundError,
    MemoryStorageError,
    MemoryScopeError,
    ToolExecutionBlockedError,
)
from aria.providers.llm_provider import LLMProvider
from aria.rag.vector_store import VectorStore
from aria.rag.bm25_index import BM25Index
from aria.rag.hybrid_retriever import HybridRetriever
from aria.agents.react_agent import ReActAgent
from aria.memory.file_storage import FileStorageAdapter
from aria.memory.index_manager import IndexManager
from aria.memory.memory_loader import MemoryLoader
from aria.memory.types import (
    TopicUpsertRequest,
    TopicResponse,
    MemoryLoadRequest,
    MemoryLoadResponse,
    validate_domain,
    validate_scope,
)
from aria.tools.tool_registry import ToolRegistry, ToolNotFoundError
from aria.tools.builtin import MemoryReadTool, MemoryWriteTool, KnowledgeSearchTool
from aria.events.event_store import EventStore
from aria.events.types import EventIngestRequest, EventIngestResponse, EventQuery
from aria.alerts.alert_manager import AlertManager

logger = structlog.get_logger()

# === Global Instances ===
llm_provider: LLMProvider | None = None
vector_store: VectorStore | None = None
react_agent: ReActAgent | None = None
index_manager: IndexManager | None = None
memory_loader: MemoryLoader | None = None
tool_registry: ToolRegistry | None = None
event_store: EventStore | None = None
alert_manager: AlertManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """애플리케이션 시작/종료 시 초기화"""
    global llm_provider, vector_store, react_agent, rate_limiter
    global index_manager, memory_loader, tool_registry, event_store, alert_manager

    config = get_config()

    # BM25 인덱스 생성 → VectorStore에 주입 (문서 추가 시 자동 동기화)
    bm25_index = BM25Index()
    llm_provider = LLMProvider(config)
    vector_store = VectorStore(config, bm25_index=bm25_index)

    # Hybrid Retriever → ReAct 에이전트에 주입
    hybrid_retriever = HybridRetriever(vector_store, bm25_index)

    # Memory System 초기화
    storage = FileStorageAdapter(config.memory.base_path)
    index_manager = IndexManager(storage)
    memory_loader = MemoryLoader(index_manager, config.memory.token_budget)

    # Tool Registry 초기화 + Built-in Tools 등록
    from aria.tools.critic import CriticEvaluator
    from aria.tools.critic_types import CriticConfig

    critic = CriticEvaluator(llm_provider, CriticConfig())
    tool_registry = ToolRegistry(critic=critic)
    tool_registry.register_executor(MemoryReadTool(index_manager))
    tool_registry.register_executor(MemoryWriteTool(index_manager))
    tool_registry.register_executor(KnowledgeSearchTool(hybrid_retriever))

    # MCP Tools — Notion (토큰 설정 시에만 등록)
    if config.notion.is_configured:
        from aria.tools.mcp.notion_tools import (
            NotionClient,
            NotionSearchTool,
            NotionReadPageTool,
            NotionCreatePageTool,
        )
        notion_client = NotionClient(config.notion)
        tool_registry.register_executor(NotionSearchTool(notion_client))
        tool_registry.register_executor(NotionReadPageTool(notion_client))
        tool_registry.register_executor(NotionCreatePageTool(notion_client))
        logger.info("notion_tools_registered", tools=3)
    else:
        logger.info("notion_tools_skipped", reason="ARIA_NOTION_TOKEN not set")

    # MCP Tools — 카카오맵 (REST API 키 설정 시에만 등록)
    if config.kakao_map.is_configured:
        from aria.tools.mcp.kakao_map_tools import (
            KakaoMapClient,
            KakaoKeywordSearchTool,
            KakaoAddressSearchTool,
            KakaoCoord2AddressTool,
        )
        kakao_client = KakaoMapClient(config.kakao_map)
        tool_registry.register_executor(KakaoKeywordSearchTool(kakao_client))
        tool_registry.register_executor(KakaoAddressSearchTool(kakao_client))
        tool_registry.register_executor(KakaoCoord2AddressTool(kakao_client))
        logger.info("kakao_map_tools_registered", tools=3)
    else:
        logger.info("kakao_map_tools_skipped", reason="ARIA_KAKAO_REST_API_KEY not set")

    # MCP Tools — 네이버 검색 (Client ID + Secret 설정 시에만 등록)
    if config.naver_search.is_configured:
        from aria.tools.mcp.naver_search_tools import (
            NaverSearchClient,
            NaverBlogSearchTool,
            NaverNewsSearchTool,
            NaverCafeSearchTool,
            NaverShopSearchTool,
            NaverKinSearchTool,
            NaverLocalSearchTool,
        )
        naver_client = NaverSearchClient(config.naver_search)
        tool_registry.register_executor(NaverBlogSearchTool(naver_client))
        tool_registry.register_executor(NaverNewsSearchTool(naver_client))
        tool_registry.register_executor(NaverCafeSearchTool(naver_client))
        tool_registry.register_executor(NaverShopSearchTool(naver_client))
        tool_registry.register_executor(NaverKinSearchTool(naver_client))
        tool_registry.register_executor(NaverLocalSearchTool(naver_client))
        logger.info("naver_search_tools_registered", tools=6)
    else:
        logger.info("naver_search_tools_skipped", reason="ARIA_NAVER_CLIENT_ID/SECRET not set")

    # MCP Tools — TMAP 대중교통 (SK APP KEY 설정 시에만 등록)
    if config.tmap.is_configured:
        from aria.tools.mcp.tmap_tools import (
            TmapClient,
            TmapTransitRouteTool,
        )
        tmap_client = TmapClient(config.tmap)
        tool_registry.register_executor(TmapTransitRouteTool(tmap_client))
        logger.info("tmap_tools_registered", tools=1)
    else:
        logger.info("tmap_tools_skipped", reason="ARIA_TMAP_APP_KEY not set")

    # MCP Tools — DuckDuckGo 웹 검색 (API 키 불필요 / 기본 활성화)
    if config.ddg.is_configured:
        from aria.tools.mcp.ddg_tools import (
            DdgSearchClient,
            DdgWebSearchTool,
            DdgNewsSearchTool,
        )
        ddg_client = DdgSearchClient(config.ddg)
        tool_registry.register_executor(DdgWebSearchTool(ddg_client))
        tool_registry.register_executor(DdgNewsSearchTool(ddg_client))
        logger.info("ddg_tools_registered", tools=2)
    else:
        logger.info("ddg_tools_skipped", reason="ARIA_DDG_ENABLED=false")

    # MCP Tools — Google Maps Platform (API Key 기반 / 글로벌 장소 검색)
    if config.google_maps.is_configured:
        from aria.tools.mcp.google_maps_tools import (
            GoogleMapsClient,
            GooglePlacesSearchTool,
            GoogleGeocodeTool,
            GoogleDirectionsTool,
        )
        google_maps_client = GoogleMapsClient(config.google_maps)
        tool_registry.register_executor(GooglePlacesSearchTool(google_maps_client))
        tool_registry.register_executor(GoogleGeocodeTool(google_maps_client))
        tool_registry.register_executor(GoogleDirectionsTool(google_maps_client))
        logger.info("google_maps_tools_registered", tools=3)
    else:
        logger.info("google_maps_tools_skipped", reason="ARIA_GOOGLE_MAPS_API_KEY not set")

    # MCP Tools — Gmail + Google Calendar (OAuth2)
    if config.google_oauth.is_configured:
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.tools.mcp.gmail_tools import (
            GmailClient,
            GmailSearchTool,
            GmailReadTool,
            GmailSendTool,
            GmailDraftTool,
        )
        from aria.tools.mcp.gcal_tools import (
            GCalClient,
            GCalListEventsTool,
            GCalCreateEventTool,
            GCalUpdateEventTool,
        )
        google_token_mgr = GoogleTokenManager(config.google_oauth)
        gmail_client = GmailClient(google_token_mgr)
        gcal_client = GCalClient(google_token_mgr)

        tool_registry.register_executor(GmailSearchTool(gmail_client))
        tool_registry.register_executor(GmailReadTool(gmail_client))
        tool_registry.register_executor(GmailSendTool(gmail_client))
        tool_registry.register_executor(GmailDraftTool(gmail_client))
        tool_registry.register_executor(GCalListEventsTool(gcal_client))
        tool_registry.register_executor(GCalCreateEventTool(gcal_client))
        tool_registry.register_executor(GCalUpdateEventTool(gcal_client))
        logger.info("google_oauth_tools_registered", tools=7, services="gmail+calendar")
    else:
        logger.info("google_oauth_tools_skipped", reason="ARIA_GOOGLE_OAUTH_* not configured")

    # MCP Tools — 서버 자동 모니터링 (API 키 불필요 / 기본 활성화)
    if config.monitoring.is_configured:
        from aria.tools.mcp.server_monitor_tools import (
            ServerHealthcheckTool,
            ServerErrorLogTool,
            ServerTrafficTool,
            ServerSecurityScanTool,
        )
        tool_registry.register_executor(ServerHealthcheckTool())
        tool_registry.register_executor(ServerErrorLogTool())
        tool_registry.register_executor(ServerTrafficTool())
        tool_registry.register_executor(ServerSecurityScanTool())
        logger.info("monitoring_tools_registered", tools=4)
    else:
        logger.info("monitoring_tools_skipped", reason="ARIA_MONITOR_ENABLED=false")

    # MCP Client — Google Workspace MCP 서버 연동 (OAuth2 필수)
    mcp_clients: list = []
    if config.mcp.is_configured and config.google_oauth.is_configured:
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.mcp.client import MCPClient
        from aria.mcp.google_servers import (
            connect_google_mcp_servers,
            get_google_mcp_configs,
            get_rest_tools_to_disable,
        )
        from aria.mcp.tool_bridge import MCPToolBridge

        # GoogleTokenManager 재사용 (REST 도구 블록에서 이미 생성됨)
        # google_oauth.is_configured 조건이 동일하므로 google_token_mgr 변수 존재 보장

        mcp_configs = get_google_mcp_configs(
            enabled_services=config.mcp.enabled_google_services,
        )

        # 각 서버 연결 설정에 타임아웃 적용
        for mc in mcp_configs:
            mc.timeout = config.mcp.request_timeout

        mcp_clients = await connect_google_mcp_servers(
            mcp_configs,
            token_provider=google_token_mgr.get_access_token,
        )

        if mcp_clients:
            # MCP 도구 → ToolRegistry 자동 등록
            mcp_bridge = MCPToolBridge(
                tool_registry,
                override_existing=config.mcp.override_rest_tools,
            )

            total_mcp_tools = 0
            connected_services: set[str] = set()

            for client in mcp_clients:
                registered = await mcp_bridge.register_server(client)
                total_mcp_tools += len(registered)
                connected_services.add(client.server_name)

            # MCP 우선 정책: 기존 REST 도구 비활성화
            if config.mcp.override_rest_tools:
                rest_disable_list = get_rest_tools_to_disable(connected_services)
                disabled_count = 0
                for rest_tool_name in rest_disable_list:
                    if tool_registry.has_tool(rest_tool_name):
                        defn, _ = tool_registry.get(rest_tool_name)
                        defn.enabled = False
                        disabled_count += 1

                if disabled_count > 0:
                    logger.info(
                        "rest_tools_disabled_by_mcp",
                        disabled=disabled_count,
                        tools=rest_disable_list,
                    )

            logger.info(
                "mcp_integration_complete",
                servers_connected=len(mcp_clients),
                mcp_tools_registered=total_mcp_tools,
                services=list(connected_services),
            )

            # lifespan 종료 시 정리를 위해 app.state에 저장
            app_state_mcp_clients = mcp_clients
        else:
            logger.warning("mcp_no_servers_connected")
            app_state_mcp_clients = []
    elif config.mcp.is_configured and not config.google_oauth.is_configured:
        logger.info("mcp_skipped", reason="ARIA_GOOGLE_OAUTH_* not configured (OAuth2 필수)")
        app_state_mcp_clients = []
    else:
        logger.info("mcp_skipped", reason="ARIA_MCP_ENABLED=false or no services configured")
        app_state_mcp_clients = []

    react_agent = ReActAgent(
        llm_provider,
        vector_store,
        hybrid_retriever=hybrid_retriever,
        memory_loader=memory_loader,
        tool_registry=tool_registry,
    )

    # Event Store 초기화
    event_store = EventStore(
        base_path=config.event.base_path,
        max_buffer_size=config.event.max_buffer_size,
        retention_days=config.event.retention_days,
    )

    # Alert Manager 초기화 (텔레그램 설정이 있을 때만 활성화)
    alert_manager = AlertManager(
        bot_token=config.telegram.bot_token,
        chat_id=config.telegram.chat_id,
        enabled=config.alert.enabled,
        cost_warning_threshold=config.alert.cost_warning_threshold,
        cost_critical_threshold=config.alert.cost_critical_threshold,
        confidence_threshold=config.alert.confidence_threshold,
        consecutive_error_threshold=config.alert.consecutive_error_threshold,
    )

    rate_limiter = RateLimiter(
        max_requests=config.api.rate_limit_per_minute,
        window_seconds=60,
    )

    logger.info(
        "aria_engine_started",
        env=config.api.env.value,
        auth_disabled=config.api.auth_disabled,
        rate_limit=config.api.rate_limit_per_minute,
        hybrid_retrieval=True,
        memory_base_path=config.memory.base_path,
        memory_token_budget=config.memory.token_budget,
        tools_registered=tool_registry.tool_count,
        event_store_path=config.event.base_path,
        alerts_enabled=alert_manager.enabled,
        monitoring_enabled=config.monitoring.is_configured,
    )
    yield

    # MCP 클라이언트 정리
    for client in app_state_mcp_clients:
        try:
            await client.close()
        except Exception:
            pass

    logger.info("aria_engine_stopped")


app = FastAPI(
    title="ARIA Engine",
    description="Agentic Reasoning and Information Access Engine",
    version="0.2.0",
    lifespan=lifespan,
)


# === Rate Limiter (in-memory / 단일 인스턴스용) ===
class RateLimiter:
    """간단한 인메모리 rate limiter (sliding window)"""

    def __init__(self, max_requests: int = 60, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, client_id: str) -> bool:
        now = time.time()
        window_start = now - self.window_seconds

        # 만료된 요청 제거
        self._requests[client_id] = [
            t for t in self._requests[client_id] if t > window_start
        ]

        if len(self._requests[client_id]) >= self.max_requests:
            return False

        self._requests[client_id].append(now)
        return True


rate_limiter: RateLimiter | None = None  # lifespan에서 config 기반 초기화


# === CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],  # Next.js 개발 서버
    allow_credentials=True,
    allow_methods=["POST", "GET", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["X-API-Key", "Content-Type"],
)


# === Global Exception Handlers ===
@app.exception_handler(KillSwitchError)
async def killswitch_handler(request: Request, exc: KillSwitchError) -> JSONResponse:
    logger.error("killswitch_triggered_http", path=request.url.path, details=exc.details)
    # 능동 알림: KillSwitch 발동
    if alert_manager:
        asyncio.create_task(alert_manager.check_killswitch(
            daily_cost=exc.details.get("daily_cost_usd", 0),
            monthly_cost=exc.details.get("monthly_cost_usd", 0),
        ))
    return JSONResponse(
        status_code=429,
        content={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(LLMAllProvidersExhaustedError)
async def all_providers_exhausted_handler(request: Request, exc: LLMAllProvidersExhaustedError) -> JSONResponse:
    logger.error("all_providers_exhausted_http", path=request.url.path, attempts=exc.details.get("attempts"))
    return JSONResponse(
        status_code=502,
        content={
            "error": exc.code,
            "message": "모든 AI 모델에 연결할 수 없습니다. 잠시 후 다시 시도해주세요.",
            "details": {"attempts": exc.details.get("attempts", [])},
        },
    )


@app.exception_handler(CollectionNotFoundError)
async def collection_not_found_handler(request: Request, exc: CollectionNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(VectorStoreError)
async def vector_store_handler(request: Request, exc: VectorStoreError) -> JSONResponse:
    logger.error("vector_store_error_http", path=request.url.path, error=exc.message)
    return JSONResponse(
        status_code=500,
        content={
            "error": exc.code,
            "message": exc.message,
        },
    )


@app.exception_handler(AgentError)
async def agent_error_handler(request: Request, exc: AgentError) -> JSONResponse:
    logger.error("agent_error_http", path=request.url.path, error=exc.message)
    # 능동 알림: 연속 에러 체크
    if alert_manager:
        asyncio.create_task(alert_manager.check_error(
            error_type="AgentError",
            message=exc.message,
        ))
    return JSONResponse(
        status_code=500,
        content={
            "error": exc.code,
            "message": "에이전트 처리 중 오류가 발생했습니다.",
            "details": exc.details,
        },
    )


@app.exception_handler(NoAPIKeyError)
async def no_api_key_handler(request: Request, exc: NoAPIKeyError) -> JSONResponse:
    logger.error("no_api_key_http", path=request.url.path, details=exc.details)
    return JSONResponse(
        status_code=503,
        content={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


# === Memory Exception Handlers ===
@app.exception_handler(VersionConflictError)
async def version_conflict_handler(request: Request, exc: VersionConflictError) -> JSONResponse:
    logger.warning("version_conflict_http", path=request.url.path, details=exc.details)
    # 능동 알림: 메모리 충돌
    if alert_manager:
        asyncio.create_task(alert_manager.check_memory_conflict(
            scope=exc.details.get("scope", ""),
            domain=exc.details.get("domain", ""),
            expected_version=exc.details.get("expected_version", 0),
            actual_version=exc.details.get("actual_version", 0),
        ))
    return JSONResponse(
        status_code=409,
        content={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(MemoryNotFoundError)
async def memory_not_found_handler(request: Request, exc: MemoryNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(MemoryScopeError)
async def memory_scope_handler(request: Request, exc: MemoryScopeError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(MemoryStorageError)
async def memory_storage_handler(request: Request, exc: MemoryStorageError) -> JSONResponse:
    logger.error("memory_storage_error_http", path=request.url.path, error=exc.message)
    return JSONResponse(
        status_code=500,
        content={
            "error": exc.code,
            "message": exc.message,
        },
    )


# === Tool Safety Exception Handlers ===
@app.exception_handler(ToolExecutionBlockedError)
async def tool_blocked_handler(request: Request, exc: ToolExecutionBlockedError) -> JSONResponse:
    logger.warning(
        "tool_execution_blocked_http",
        path=request.url.path,
        tool_name=exc.details.get("tool_name"),
        reason=exc.details.get("reason"),
    )
    return JSONResponse(
        status_code=403,
        content={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(AriaError)
async def aria_error_handler(request: Request, exc: AriaError) -> JSONResponse:
    logger.error("aria_error_http", path=request.url.path, code=exc.code, error=exc.message)
    return JSONResponse(
        status_code=500,
        content={
            "error": exc.code,
            "message": exc.message,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """예상 못한 에러 → 500 + 내부 정보 노출 방지"""
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        error_type=type(exc).__name__,
        error=str(exc)[:500],
    )
    # 능동 알림: 서버 에러
    if alert_manager:
        asyncio.create_task(alert_manager.check_server_error(
            path=request.url.path,
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        ))
    return JSONResponse(
        status_code=500,
        content={
            "error": "INTERNAL_ERROR",
            "message": "내부 서버 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
        },
    )


# === API Key Authentication ===
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(request: Request, api_key: str | None = Security(api_key_header)) -> str:
    """API 키 검증 + Rate Limit 체크

    인증 우회 조건: ARIA_AUTH_DISABLED=true (development 환경에서만 허용)
    production/staging에서는 config validator가 auth_disabled=true를 차단함
    """
    config = get_config()

    if config.api.auth_disabled:
        # 명시적 스킵 — IP 기반 client_id (rate limit은 여전히 적용)
        client_ip = request.client.host if request.client else "unknown"
        client_id = f"anon:{client_ip}"
        logger.debug("auth_skipped", client_id=client_id, reason="auth_disabled")
    else:
        if not api_key:
            raise HTTPException(
                status_code=401,
                detail="API 키가 필요합니다. X-API-Key 헤더를 포함하세요.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        if api_key != config.api.api_key:
            logger.warning(
                "auth_failed",
                client_ip=request.client.host if request.client else "unknown",
                api_key_prefix=api_key[:8] if len(api_key) >= 8 else "***",
            )
            raise HTTPException(
                status_code=401,
                detail="유효하지 않은 API 키입니다.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        client_id = f"key:{api_key[:8]}"

    # Rate limit 체크 (인증 여부 무관하게 항상 적용)
    if rate_limiter and not rate_limiter.is_allowed(client_id):
        logger.warning("rate_limit_exceeded", client_id=client_id)
        raise HTTPException(
            status_code=429,
            detail="요청 제한을 초과했습니다. 잠시 후 다시 시도해주세요.",
            headers={"Retry-After": "60"},
        )

    return client_id


# === Request/Response Models ===
class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=10000, description="질문")
    collection: str = Field(default="default", description="검색 대상 컬렉션")
    model_tier: str = Field(default="default", description="모델 티어: default|heavy|cheap")
    context: list[dict[str, str]] = Field(default_factory=list, description="이전 대화 이력")
    scope: str = Field(default="global", description="메모리 스코프")
    memory_domains: list[str] | None = Field(default=None, description="명시적 메모리 토픽 지정 (None=전체)")


class QueryResponse(BaseModel):
    answer: str
    confidence: float
    iterations: int
    reasoning_steps: list[str]
    cost_summary: dict[str, Any]
    latency_ms: float
    memory_loaded: list[str] = Field(default_factory=list, description="로딩된 메모리 도메인")
    tool_calls_made: int = Field(default=0, description="도구 호출 횟수")


class DocumentInput(BaseModel):
    text: str = Field(..., min_length=1, max_length=50000, description="문서 텍스트")
    metadata: dict[str, Any] = Field(default_factory=dict, description="메타데이터")


class KnowledgeAddRequest(BaseModel):
    collection: str = Field(..., min_length=1, max_length=100, description="컬렉션 이름")
    documents: list[DocumentInput] = Field(..., min_length=1, max_length=100, description="추가할 문서 목록")


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000)
    top_k: int = Field(default=5, ge=1, le=50)
    score_threshold: float = Field(default=0.3, ge=0.0, le=1.0)


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# === Endpoints ===
@app.get("/v1/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "engine": "ARIA", "version": "0.2.0"}


@app.post(
    "/v1/query",
    response_model=QueryResponse,
    responses={
        429: {"model": ErrorResponse, "description": "KillSwitch 발동 또는 Rate limit"},
        502: {"model": ErrorResponse, "description": "모든 LLM 프로바이더 실패"},
    },
)
async def query_agent(
    request: QueryRequest,
    _client_id: str = Depends(verify_api_key),
) -> QueryResponse:
    """ARIA 에이전트에게 질문 (메모리 자동 주입)"""
    if react_agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    start_time = time.time()

    # KillSwitch / LLMAllProvidersExhausted / AgentError는
    # 글로벌 핸들러가 처리 → 여기서 별도 catch 불필요
    result = await react_agent.run(
        query=request.query,
        collection=request.collection,
        context=request.context if request.context else None,
        scope=request.scope,
        memory_domains=request.memory_domains,
    )

    latency_ms = (time.time() - start_time) * 1000

    # 능동 알림: 비용 체크 + confidence 체크 (비차단)
    if alert_manager and llm_provider:
        cost = llm_provider.get_cost_summary()
        asyncio.create_task(alert_manager.check_cost(
            daily=cost.get("daily_cost_usd", 0),
            monthly=cost.get("monthly_cost_usd", 0),
            daily_limit=cost.get("daily_limit_usd", 0),
            monthly_limit=cost.get("monthly_limit_usd", 0),
        ))
        asyncio.create_task(alert_manager.check_confidence(
            confidence=result["confidence"],
            query=request.query,
        ))
        # 성공 시 에러 카운터 리셋
        alert_manager.reset_error_counter()

    return QueryResponse(
        answer=result["answer"],
        confidence=result["confidence"],
        iterations=result["iterations"],
        reasoning_steps=result["reasoning_steps"],
        cost_summary=result["cost_summary"],
        latency_ms=round(latency_ms, 2),
        memory_loaded=result.get("memory_loaded", []),
        tool_calls_made=result.get("tool_calls_made", 0),
    )


@app.post("/v1/knowledge")
async def add_knowledge(
    request: KnowledgeAddRequest,
    _client_id: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """지식 베이스에 문서 추가"""
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store not initialized")

    documents = [{"text": doc.text, "metadata": doc.metadata} for doc in request.documents]
    count = vector_store.add_documents(request.collection, documents)

    return {"status": "ok", "collection": request.collection, "documents_added": count}


@app.post(
    "/v1/knowledge/{collection}/search",
    responses={404: {"model": ErrorResponse, "description": "컬렉션 미존재"}},
)
async def search_knowledge(
    collection: str,
    request: SearchRequest,
    _client_id: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """벡터 검색"""
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store not initialized")

    # CollectionNotFoundError / VectorStoreError는 글로벌 핸들러가 처리
    results = vector_store.search(
        collection_name=collection,
        query=request.query,
        top_k=request.top_k,
        score_threshold=request.score_threshold,
    )

    return {"collection": collection, "query": request.query, "results": results}


@app.get("/v1/cost")
async def get_cost(
    _client_id: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """현재 비용 현황"""
    if llm_provider is None:
        raise HTTPException(status_code=503, detail="Provider not initialized")
    return llm_provider.get_cost_summary()


@app.get("/v1/collections")
async def list_collections(
    _client_id: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """벡터DB 컬렉션 목록"""
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    collections = vector_store._client.get_collections().collections
    return {
        "collections": [
            {"name": c.name}
            for c in collections
        ]
    }


# === Memory Endpoints ===

def _require_memory() -> tuple[IndexManager, MemoryLoader]:
    """메모리 시스템 초기화 확인"""
    if index_manager is None or memory_loader is None:
        raise HTTPException(status_code=503, detail="Memory system not initialized")
    return index_manager, memory_loader


def _validate_scope_http(scope: str) -> None:
    """스코프 유효성 검증 (HTTP 에러로 변환)"""
    try:
        validate_scope(scope)
    except ValueError:
        raise MemoryScopeError(scope)


def _validate_domain_http(domain: str) -> None:
    """도메인 유효성 검증 (HTTP 에러로 변환)"""
    try:
        validate_domain(domain)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"유효하지 않은 도메인: {domain}")


@app.get("/v1/memory/{scope}/index")
async def get_memory_index(
    scope: str,
    _client_id: str = Depends(verify_api_key),
) -> JSONResponse:
    """메모리 인덱스 조회"""
    mgr, _ = _require_memory()
    _validate_scope_http(scope)
    index = mgr.get_index(scope)
    return JSONResponse(content=index.model_dump(mode="json"))


@app.get("/v1/memory/{scope}/topics/{domain}")
async def get_memory_topic(
    scope: str,
    domain: str,
    _client_id: str = Depends(verify_api_key),
) -> JSONResponse:
    """토픽 조회"""
    mgr, _ = _require_memory()
    _validate_scope_http(scope)
    _validate_domain_http(domain)
    topic = mgr.get_topic(scope, domain)
    entry = mgr.get_entry(scope, domain)
    resp = TopicResponse(
        domain=topic.domain,
        scope=topic.scope,
        summary=entry.summary if entry else "",
        content=topic.content,
        version=topic.version,
        updated_at=topic.updated_at,
        created_at=topic.created_at,
        token_estimate=entry.token_estimate if entry else None,
    )
    return JSONResponse(content=resp.model_dump(mode="json"))


@app.put("/v1/memory/{scope}/topics/{domain}")
async def upsert_memory_topic(
    scope: str,
    domain: str,
    request: TopicUpsertRequest,
    _client_id: str = Depends(verify_api_key),
) -> JSONResponse:
    """토픽 upsert (read-before-write 강제)"""
    mgr, _ = _require_memory()
    _validate_scope_http(scope)
    _validate_domain_http(domain)

    topic = mgr.upsert_topic(
        scope=scope,
        domain=domain,
        summary=request.summary,
        content=request.content,
        expected_version=request.expected_version,
    )
    entry = mgr.get_entry(scope, domain)
    resp = TopicResponse(
        domain=topic.domain,
        scope=topic.scope,
        summary=request.summary,
        content=topic.content,
        version=topic.version,
        updated_at=topic.updated_at,
        created_at=topic.created_at,
        token_estimate=entry.token_estimate if entry else None,
    )
    return JSONResponse(content=resp.model_dump(mode="json"))


@app.delete("/v1/memory/{scope}/topics/{domain}")
async def delete_memory_topic(
    scope: str,
    domain: str,
    _client_id: str = Depends(verify_api_key),
) -> JSONResponse:
    """토픽 + 인덱스 엔트리 삭제"""
    mgr, _ = _require_memory()
    _validate_scope_http(scope)
    _validate_domain_http(domain)
    mgr.delete_topic(scope, domain)
    return JSONResponse(content={"status": "deleted", "scope": scope, "domain": domain})


@app.post("/v1/memory/{scope}/load")
async def load_memory(
    scope: str,
    request: MemoryLoadRequest,
    _client_id: str = Depends(verify_api_key),
) -> JSONResponse:
    """메모리 로딩 → 프롬프트용 마크다운 반환"""
    _, loader = _require_memory()
    _validate_scope_http(scope)

    result = loader.load(
        scope=scope,
        domains=request.domains,
        token_budget=request.token_budget,
    )

    resp = MemoryLoadResponse(
        scope=result.scope,
        loaded_domains=result.loaded_domains,
        prompt_markdown=result.prompt_markdown,
        total_tokens=result.total_tokens,
        budget_used=round(result.budget_used, 4),
    )
    return JSONResponse(content=resp.model_dump(mode="json"))


# === HITL (Human-in-the-Loop) Pending Actions ===


@app.post(
    "/v1/tools/pending/{confirmation_id}/execute",
    summary="승인된 대기 도구 실행",
    dependencies=[Depends(verify_api_key)],
)
async def execute_pending_tool(confirmation_id: str) -> JSONResponse:
    """사용자가 승인한 대기 도구 액션을 실행"""
    if tool_registry is None:
        return JSONResponse(
            status_code=503,
            content={"error": "SERVICE_UNAVAILABLE", "message": "Tool Registry 미초기화"},
        )

    try:
        result = await tool_registry.execute_pending(confirmation_id)
        return JSONResponse(content={
            "tool_name": result.tool_name,
            "success": result.success,
            "output": result.output,
            "error": result.error,
            "latency_ms": result.latency_ms,
        })
    except ToolNotFoundError:
        return JSONResponse(
            status_code=404,
            content={
                "error": "PENDING_NOT_FOUND",
                "message": f"대기 액션이 만료되었거나 존재하지 않습니다: {confirmation_id}",
            },
        )


@app.delete(
    "/v1/tools/pending/{confirmation_id}",
    summary="대기 도구 거부 (삭제)",
    dependencies=[Depends(verify_api_key)],
)
async def deny_pending_tool(confirmation_id: str) -> JSONResponse:
    """사용자가 거부한 대기 도구 액션을 삭제"""
    if tool_registry is None:
        return JSONResponse(
            status_code=503,
            content={"error": "SERVICE_UNAVAILABLE", "message": "Tool Registry 미초기화"},
        )

    removed = tool_registry.deny_pending(confirmation_id)
    return JSONResponse(content={
        "confirmation_id": confirmation_id,
        "removed": removed,
    })


# === Event Collection Endpoints ===


def _require_event_store() -> EventStore:
    """이벤트 저장소 초기화 확인"""
    if event_store is None:
        raise HTTPException(status_code=503, detail="Event store not initialized")
    return event_store


async def _evaluate_monitoring_event(event: Any) -> None:
    """모니터링 이벤트 수신 시 AlertManager 자동 평가

    cron 스크립트가 POST /v1/events로 보낸 모니터링 결과를
    AlertManager가 평가하여 필요 시 텔레그램 알림 발송

    지원 event_type:
    - health_check → check_health()
    - error_log_analysis → check_error_spike()
    - traffic_analysis → check_traffic_anomaly()
    - security_scan → check_security_issue()
    """
    if alert_manager is None:
        return

    try:
        et = event.event_type
        data = event.data or {}

        if et == "health_check":
            await alert_manager.check_health(
                url=data.get("url", "unknown"),
                status=data.get("status", "unknown"),
                status_code=data.get("status_code", 0),
                response_time_ms=data.get("response_time_ms", 0),
                ssl_expiry_days=data.get("ssl_expiry_days"),
                error=data.get("error"),
            )

        elif et == "error_log_analysis":
            error_count = data.get("error_count", 0)
            if error_count >= 10:
                await alert_manager.check_error_spike(
                    log_path=data.get("log_path", "unknown"),
                    error_count=error_count,
                    top_errors=data.get("top_error_messages"),
                )

        elif et == "traffic_analysis":
            if data.get("anomaly_detected"):
                await alert_manager.check_traffic_anomaly(
                    log_path=data.get("log_path", "unknown"),
                    current_rpm=data.get("current_rpm", 0),
                    baseline_rpm=data.get("baseline_rpm", 0),
                    ratio=data.get("ratio", 0),
                    top_ips=data.get("top_ips"),
                )

        elif et == "security_scan":
            issues = data.get("issues", [])
            high_issues = [i for i in issues if i.get("severity") == "high"]
            if high_issues:
                await alert_manager.check_security_issue(
                    url=data.get("url", "unknown"),
                    headers_score=data.get("headers_score", 0),
                    headers_max_score=data.get("headers_max_score", 0),
                    issues=issues,
                )

    except Exception as e:
        # 알림 평가 실패는 이벤트 인입에 영향 없음
        logger.warning(
            "monitoring_event_eval_failed",
            event_type=getattr(event, "event_type", "unknown"),
            error=str(e)[:200],
        )


@app.post(
    "/v1/events",
    summary="이벤트 수집",
    response_model=EventIngestResponse,
    dependencies=[Depends(verify_api_key)],
)
async def ingest_events(request: EventIngestRequest) -> JSONResponse:
    """제품(Testorum/Talksim/AutoTube)에서 이벤트 인입

    배치 인입 지원 (최대 100개/요청)
    모니터링 이벤트(source=aria, event_type=health_check|error_log_analysis|traffic_analysis|security_scan)는
    AlertManager 자동 평가 → 텔레그램 알림 발송
    """
    store = _require_event_store()

    try:
        events = store.ingest_batch(request.events)
        event_ids = [e.event_id for e in events]

        logger.info(
            "events_ingested",
            count=len(events),
            sources=list({e.source for e in events}),
        )

        # 모니터링 이벤트 자동 알림 평가
        if alert_manager and alert_manager.enabled:
            for event in events:
                if event.source == "aria":
                    asyncio.create_task(
                        _evaluate_monitoring_event(event)
                    )

        return JSONResponse(content={
            "status": "ok",
            "ingested": len(events),
            "event_ids": event_ids,
        })
    except OSError as e:
        logger.error("event_ingest_failed", error=str(e))
        return JSONResponse(
            status_code=500,
            content={
                "error": "EVENT_STORAGE_ERROR",
                "message": f"이벤트 저장 실패: {e}",
            },
        )


@app.get(
    "/v1/events",
    summary="이벤트 조회",
    dependencies=[Depends(verify_api_key)],
)
async def query_events(
    source: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> JSONResponse:
    """이벤트 조회 (필터링 / 최신순 정렬)"""
    store = _require_event_store()

    # severity 문자열 → enum 변환
    severity_enum = None
    if severity:
        try:
            from aria.events.types import EventSeverity
            severity_enum = EventSeverity(severity.lower())
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "INVALID_SEVERITY",
                    "message": f"유효하지 않은 severity: '{severity}'. 허용: info, warning, error",
                },
            )

    # limit 범위 검증
    if limit < 1 or limit > 500:
        return JSONResponse(
            status_code=400,
            content={
                "error": "INVALID_LIMIT",
                "message": "limit은 1~500 사이여야 합니다",
            },
        )

    q = EventQuery(
        source=source,
        event_type=event_type,
        severity=severity_enum,
        since=since,
        until=until,
        limit=limit,
    )

    events = store.query(q)
    return JSONResponse(content={
        "events": [e.model_dump(mode="json") for e in events],
        "count": len(events),
        "query": q.model_dump(mode="json"),
    })


@app.get(
    "/v1/events/stats",
    summary="이벤트 통계",
    dependencies=[Depends(verify_api_key)],
)
async def event_stats() -> JSONResponse:
    """이벤트 저장소 통계"""
    store = _require_event_store()
    return JSONResponse(content=store.get_stats())


# === Alert Endpoints ===


@app.get(
    "/v1/alerts/stats",
    summary="알림 통계",
    dependencies=[Depends(verify_api_key)],
)
async def alert_stats() -> JSONResponse:
    """능동 알림 시스템 통계"""
    if alert_manager is None:
        return JSONResponse(
            status_code=503,
            content={"error": "SERVICE_UNAVAILABLE", "message": "Alert Manager 미초기화"},
        )
    return JSONResponse(content=alert_manager.get_stats())
