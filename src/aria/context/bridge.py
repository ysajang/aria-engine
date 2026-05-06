"""ARIA Engine - Context Bridge

외부 AI 도구 ↔ ARIA 메모리 간 컨텍스트 교환 핵심 로직

Push 플로우:
    1. 중복 세션 체크 (session_id)
    2. 이벤트 저장 (원본 보존)
    3. 규칙 기반 분석 (LLM 0)
    4. 인사이트 → 메모리 upsert (auto_analyze=True 시)
    5. 응답 반환

Pull 플로우:
    1. 스코프 검증
    2. MemoryLoader로 토큰 예산 내 로딩
    3. 포맷 변환 (markdown/json)
    4. 응답 반환

설계 원칙:
    - LLM 호출 0 (규칙 기반 분석만)
    - 이벤트 저장은 항상 성공 (분석 실패해도 원본 보존)
    - 비차단 (push 실패가 외부 도구에 영향 없음)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from aria.context.analyzer import analyze_conversation, build_memory_content
from aria.context.types import (
    AnalysisResult,
    ContextPullRequest,
    ContextPullResponse,
    ContextPushRequest,
    ContextPushResponse,
    SessionRecord,
)
from aria.events.event_store import EventStore
from aria.memory.index_manager import IndexManager
from aria.memory.memory_loader import MemoryLoader

logger = structlog.get_logger()

# 세션 중복 감지 TTL (인메모리)
_MAX_SESSION_CACHE = 1000


class ContextBridge:
    """외부 AI 컨텍스트 교환 브릿지

    Args:
        index_manager: 메모리 인덱스 매니저
        memory_loader: 메모리 로더 (pull용)
        event_store: 이벤트 저장소 (push 원본 보존)
    """

    def __init__(
        self,
        index_manager: IndexManager,
        memory_loader: MemoryLoader,
        event_store: EventStore | None = None,
    ) -> None:
        self._index_manager = index_manager
        self._memory_loader = memory_loader
        self._event_store = event_store
        self._session_cache: dict[str, SessionRecord] = {}

    async def push(self, request: ContextPushRequest) -> ContextPushResponse:
        """외부 대화 로그 인입 + 분석 + 메모리 upsert

        Args:
            request: Push 요청

        Returns:
            ContextPushResponse
        """
        # 1. 세션 중복 체크
        if request.session_id:
            if request.session_id in self._session_cache:
                existing = self._session_cache[request.session_id]
                logger.info(
                    "context_push_duplicate_session",
                    session_id=request.session_id,
                    original_received=existing.received_at,
                )
                return ContextPushResponse(
                    status="duplicate",
                    session_id=request.session_id,
                )

        # 2. 이벤트 저장 (원본 보존 — 분석 실패와 무관)
        event_id = await self._store_event(request)

        # 3. 규칙 기반 분석
        analysis: AnalysisResult | None = None
        memory_updated = False
        memory_domain: str | None = None

        if request.auto_analyze:
            try:
                analysis = analyze_conversation(request.messages)

                # 4. 인사이트가 있으면 메모리 upsert
                if analysis.has_insights:
                    memory_domain = await self._upsert_to_memory(
                        scope=request.scope,
                        analysis=analysis,
                        source=request.source,
                    )
                    memory_updated = memory_domain is not None

            except Exception as e:
                # 분석 실패해도 이벤트는 이미 저장됨 → 에러만 로깅
                logger.warning(
                    "context_push_analysis_failed",
                    error=str(e),
                    source=request.source,
                    scope=request.scope,
                )

        # 5. 세션 캐시 등록
        if request.session_id:
            self._register_session(request)

        return ContextPushResponse(
            status="accepted",
            event_id=event_id,
            session_id=request.session_id,
            insights_extracted=len(analysis.insights) if analysis else 0,
            memory_updated=memory_updated,
            memory_domain=memory_domain,
            analysis=analysis,
        )

    async def pull(self, request: ContextPullRequest) -> ContextPullResponse:
        """스코프별 컨텍스트 마크다운 반환

        Args:
            request: Pull 요청

        Returns:
            ContextPullResponse
        """
        try:
            result = self._memory_loader.load(
                scope=request.scope,
                domains=request.domains,
                token_budget=request.token_budget,
            )
        except Exception as e:
            logger.warning(
                "context_pull_load_failed",
                scope=request.scope,
                error=str(e),
            )
            return ContextPullResponse(
                scope=request.scope,
                content="",
                token_count=0,
                format=request.format,
            )

        content = result.prompt_markdown

        # JSON 포맷 요청 시 변환
        if request.format == "json":
            content = json.dumps(
                {
                    "scope": request.scope,
                    "domains": result.loaded_domains,
                    "content": result.prompt_markdown,
                    "token_count": result.total_tokens,
                },
                ensure_ascii=False,
                indent=2,
            )

        return ContextPullResponse(
            scope=request.scope,
            loaded_domains=result.loaded_domains,
            content=content,
            token_count=result.total_tokens,
            format=request.format,
        )

    async def _store_event(self, request: ContextPushRequest) -> str | None:
        """이벤트 저장소에 원본 대화 로그 저장"""
        if self._event_store is None:
            return None

        # 메시지를 요약하여 저장 (전체 원본은 너무 큼)
        message_summary = []
        for msg in request.messages[:50]:  # 최대 50개만 저장
            message_summary.append({
                "role": msg.role,
                "content": msg.content[:500],  # 메시지당 500자 제한
                "timestamp": msg.timestamp,
            })

        try:
            from aria.events.types import EventInput

            event_input = EventInput(
                source=request.scope if request.scope != "global" else "aria",
                event_type="context_push",
                severity="info",
                data={
                    "title": f"External context from {request.source}",
                    "external_source": request.source,
                    "session_id": request.session_id,
                    "message_count": len(request.messages),
                    "tags": request.tags,
                    "messages_preview": message_summary[:5],
                    "metadata": request.metadata,
                },
            )

            stored = self._event_store.ingest(event_input)
            return stored.event_id

        except Exception as e:
            logger.warning("context_push_event_store_failed", error=str(e))
            return None

    async def _upsert_to_memory(
        self,
        scope: str,
        analysis: AnalysisResult,
        source: str,
    ) -> str | None:
        """분석 결과를 메모리 토픽으로 upsert

        도메인: external-context-{topic}
        """
        domain = f"external-context-{analysis.primary_topic}"

        try:
            # 기존 토픽 확인 (read-before-write)
            existing_content: str | None = None
            expected_version: int | None = None

            try:
                existing = self._index_manager.get_topic(scope, domain)
                existing_content = existing.content
                expected_version = existing.version
            except Exception:
                # 토픽 없음 → 신규 생성
                pass

            # 분석 결과 → 마크다운 콘텐츠 생성
            new_content = build_memory_content(
                analysis=analysis,
                source=source,
                existing_content=existing_content,
            )

            if not new_content:
                return None

            # 요약 생성
            insight_count = len(analysis.insights)
            summary = (
                f"외부 컨텍스트 ({source}) — "
                f"{analysis.summary[:60]} "
                f"[{insight_count}개 인사이트]"
            )
            if len(summary) > 120:
                summary = summary[:117] + "..."

            # upsert
            self._index_manager.upsert_topic(
                scope=scope,
                domain=domain,
                summary=summary,
                content=new_content,
                expected_version=expected_version,
            )

            logger.info(
                "context_push_memory_upserted",
                scope=scope,
                domain=domain,
                source=source,
                insights=insight_count,
                version=expected_version,
            )

            return domain

        except Exception as e:
            logger.warning(
                "context_push_memory_upsert_failed",
                scope=scope,
                domain=domain,
                error=str(e),
            )
            return None

    def _register_session(self, request: ContextPushRequest) -> None:
        """세션 캐시 등록 (중복 방지)"""
        if not request.session_id:
            return

        # 캐시 크기 제한
        if len(self._session_cache) >= _MAX_SESSION_CACHE:
            # 가장 오래된 항목 제거 (FIFO)
            oldest_key = next(iter(self._session_cache))
            del self._session_cache[oldest_key]

        self._session_cache[request.session_id] = SessionRecord(
            session_id=request.session_id,
            source=request.source,
            scope=request.scope,
            message_count=len(request.messages),
        )

    @property
    def session_count(self) -> int:
        """현재 캐시된 세션 수"""
        return len(self._session_cache)
