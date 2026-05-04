"""ARIA Engine - Base Learner ABC

모든 학습 축의 공통 베이스 클래스
- cheap 모델(Haiku) 기반 분석 호출 공통화
- 이벤트 저장 공통화
- 메모리 upsert 공통화
- 에러 핸들링 + graceful degradation

설계 원칙:
    1. 학습 실패가 메인 에이전트를 방해하면 안 됨 (graceful degradation)
    2. 모든 분석은 cheap 모델로 (비용 최소화 — 1회 ~$0.0003)
    3. 이벤트는 반드시 EventStore에 기록 (감사 추적)
    4. 메모리 upsert는 read-before-write 규율 준수
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any

import structlog

from aria.events.event_store import EventStore
from aria.events.types import EventInput, EventSeverity
from aria.learning.types import (
    LearningAnalysisRequest,
    LearningAnalysisResult,
    LearningEventType,
)
from aria.memory.index_manager import IndexManager
from aria.providers.llm_provider import LLMProvider

logger = structlog.get_logger()

# 분석 결과 JSON 파싱 시 최대 허용 크기 (10KB)
_MAX_ANALYSIS_RESPONSE_BYTES = 10 * 1024


class BaseLearner(ABC):
    """학습 축 공통 베이스 클래스

    Args:
        llm: LLM 프로바이더 (cheap 모델 사용)
        event_store: 이벤트 저장소
        index_manager: 메모리 인덱스 매니저 (토픽 자동 upsert)
        enabled: 학습 활성화 여부
    """

    def __init__(
        self,
        llm: LLMProvider,
        event_store: EventStore,
        index_manager: IndexManager | None = None,
        enabled: bool = True,
    ) -> None:
        self._llm = llm
        self._event_store = event_store
        self._index_manager = index_manager
        self._enabled = enabled
        self._total_analyses: int = 0
        self._total_errors: int = 0
        self._total_events_stored: int = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def stats(self) -> dict[str, Any]:
        """학습 통계"""
        return {
            "learner": self.__class__.__name__,
            "enabled": self._enabled,
            "total_analyses": self._total_analyses,
            "total_errors": self._total_errors,
            "total_events_stored": self._total_events_stored,
        }

    # === Abstract Method ===

    @abstractmethod
    async def analyze(
        self,
        request: LearningAnalysisRequest,
    ) -> LearningAnalysisResult:
        """학습 분석 실행 (각 축에서 구현)

        Args:
            request: 분석 요청 (쿼리 + 응답 + 메타데이터)

        Returns:
            분석 결과 (감지된 선호/교정/패턴 등)
        """
        ...

    # === Common Methods ===

    async def safe_analyze(
        self,
        request: LearningAnalysisRequest,
    ) -> LearningAnalysisResult:
        """안전한 분석 실행 — 실패해도 빈 결과 반환 (graceful degradation)

        메인 에이전트 파이프라인에서 호출할 때는 이 메서드 사용
        """
        if not self._enabled:
            return LearningAnalysisResult()

        start_time = time.monotonic()
        try:
            result = await self.analyze(request)
            self._total_analyses += 1

            elapsed_ms = (time.monotonic() - start_time) * 1000
            logger.info(
                "learning_analysis_complete",
                learner=self.__class__.__name__,
                elapsed_ms=round(elapsed_ms, 1),
                preferences=len(result.preferences),
                corrections=len(result.corrections),
                events_stored=result.events_stored,
            )
            return result

        except Exception as e:
            self._total_errors += 1
            elapsed_ms = (time.monotonic() - start_time) * 1000
            logger.warning(
                "learning_analysis_failed",
                learner=self.__class__.__name__,
                error=str(e),
                elapsed_ms=round(elapsed_ms, 1),
            )
            return LearningAnalysisResult()

    async def _call_cheap_llm(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """cheap 모델(Haiku)로 분석 호출

        Args:
            system_prompt: 시스템 프롬프트 (분석 지침)
            user_prompt: 사용자 프롬프트 (분석 대상 데이터)

        Returns:
            LLM 응답 텍스트

        Raises:
            Exception: LLM 호출 실패 시 (상위에서 catch)
        """
        response = await self._llm.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model_override=self._llm.config.cheap_model,
            max_tokens=1024,
            cache_system_prompt=False,  # cheap 모델은 캐시 비효율
        )
        return response.get("content", "")

    def _parse_json_response(self, text: str) -> dict[str, Any]:
        """LLM JSON 응답 파싱 (안전)

        JSON 블록 추출 + 크기 검증 + 파싱
        실패 시 빈 dict 반환 (graceful)

        Args:
            text: LLM 응답 텍스트

        Returns:
            파싱된 dict (실패 시 빈 dict)
        """
        if not text:
            return {}

        # JSON 블록 추출 (```json ... ``` 또는 { ... })
        cleaned = text.strip()
        if "```json" in cleaned:
            start = cleaned.index("```json") + 7
            end = cleaned.index("```", start) if "```" in cleaned[start:] else len(cleaned)
            cleaned = cleaned[start:end].strip()
        elif "```" in cleaned:
            start = cleaned.index("```") + 3
            end = cleaned.index("```", start) if "```" in cleaned[start:] else len(cleaned)
            cleaned = cleaned[start:end].strip()

        # 크기 검증
        if len(cleaned.encode("utf-8")) > _MAX_ANALYSIS_RESPONSE_BYTES:
            logger.warning(
                "learning_response_too_large",
                size=len(cleaned.encode("utf-8")),
                max_size=_MAX_ANALYSIS_RESPONSE_BYTES,
            )
            return {}

        try:
            result = json.loads(cleaned)
            if not isinstance(result, dict):
                return {}
            return result
        except (json.JSONDecodeError, ValueError):
            logger.debug(
                "learning_json_parse_failed",
                text_preview=cleaned[:200],
            )
            return {}

    def _store_learning_event(
        self,
        event_type: LearningEventType,
        data: dict[str, Any],
        severity: EventSeverity = EventSeverity.INFO,
    ) -> str | None:
        """학습 이벤트를 EventStore에 저장

        Args:
            event_type: 학습 이벤트 유형
            data: 이벤트 페이로드
            severity: 심각도

        Returns:
            event_id (저장 성공) 또는 None (실패)
        """
        try:
            event_input = EventInput(
                event_type=event_type.value,
                source="aria",
                severity=severity,
                data=data,
            )
            event = self._event_store.ingest(event_input)
            self._total_events_stored += 1
            return event.event_id
        except Exception as e:
            logger.warning(
                "learning_event_store_failed",
                event_type=event_type.value,
                error=str(e),
            )
            return None

    def _upsert_memory_topic(
        self,
        scope: str,
        domain: str,
        summary: str,
        content: str,
    ) -> bool:
        """메모리 토픽 자동 upsert (read-before-write 준수)

        기존 토픽이 있으면 내용 병합 → 업데이트
        없으면 신규 생성

        Args:
            scope: 메모리 스코프
            domain: 토픽 도메인
            summary: 인덱스 요약
            content: 토픽 본문

        Returns:
            성공 여부
        """
        if self._index_manager is None:
            logger.debug("learning_memory_upsert_skipped", reason="no_index_manager")
            return False

        try:
            # read-before-write: 기존 버전 확인
            expected_version: int | None = None
            try:
                existing = self._index_manager.get_topic(scope, domain)
                expected_version = existing.version
                # 기존 내용과 병합 (기존 내용 유지 + 새 내용 추가)
                if content not in existing.content:
                    content = existing.content.rstrip() + "\n\n" + content
            except Exception:
                # 토픽 미존재 → 신규 생성
                pass

            self._index_manager.upsert_topic(
                scope=scope,
                domain=domain,
                summary=summary,
                content=content,
                expected_version=expected_version,
            )

            logger.info(
                "learning_memory_upserted",
                scope=scope,
                domain=domain,
                is_update=expected_version is not None,
            )
            return True

        except Exception as e:
            logger.warning(
                "learning_memory_upsert_failed",
                scope=scope,
                domain=domain,
                error=str(e),
            )
            return False
