"""ARIA Engine - Conversation Learner (Phase 5 축 1)

대화에서 승재의 선호/패턴을 자동 감지하여 메모리 토픽에 upsert

동작 플로우:
    1. /v1/query 응답 완료 후 비동기 호출
    2. cheap 모델(Haiku)로 쿼리+응답 분석
    3. Preference 객체 추출 (category / key / value / confidence)
    4. confidence >= threshold → 메모리 토픽 자동 upsert
    5. EventStore에 preference_detected 이벤트 저장

비용: 분석 1회 ~$0.0003 (Haiku)
안전장치:
    - 분석 실패 → 메인 파이프라인 영향 없음 (safe_analyze)
    - 쿼리당 최대 감지 수 제한 (max_preferences_per_query)
    - 중복 감지 방지 (동일 key 이미 메모리에 존재 시 confidence 비교)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from aria.events.event_store import EventStore
from aria.events.types import EventSeverity
from aria.learning.base import BaseLearner
from aria.learning.types import (
    LearningAnalysisRequest,
    LearningAnalysisResult,
    LearningEventType,
    Preference,
    PreferenceCategory,
)
from aria.memory.index_manager import IndexManager
from aria.providers.llm_provider import LLMProvider

logger = structlog.get_logger()

# 메모리 토픽 도메인 (선호도 저장용)
PREFERENCE_MEMORY_DOMAIN = "user-preferences"

# 선호도 분석 시스템 프롬프트
CONVERSATION_ANALYSIS_SYSTEM = """당신은 대화 분석 전문가입니다.

사용자와 AI 어시스턴트의 대화를 분석하여 사용자의 선호도/습관/패턴을 감지하세요.

다음 카테고리에서 선호도를 추출합니다:
- communication_style: 응답 스타일 선호 (간결/상세/격식체/비격식체 등)
- tool_preference: 특정 도구/서비스 선호 (검색 엔진/지도 등)
- domain_interest: 관심 도메인/분야
- schedule_pattern: 일정/시간 관련 패턴
- workflow_habit: 작업 습관/프로세스
- response_format: 응답 형식 선호 (코드 블록/표/목록 등)
- language_preference: 언어/용어 선호

JSON 형태로만 응답하세요 (다른 텍스트 없이):
{
    "preferences": [
        {
            "category": "카테고리명",
            "key": "선호_항목_식별자",
            "value": "선호_값",
            "confidence": 0.0~1.0,
            "evidence": "감지 근거 (대화에서 발췌 요약)"
        }
    ]
}

규칙:
1. 명확한 근거가 있는 선호만 추출 (추측 금지)
2. confidence는 보수적으로 — 명시적 요청은 0.9+ / 암묵적 패턴은 0.5~0.7
3. 일회성 요청은 선호가 아님 (반복적 패턴만 추출)
4. 선호가 감지되지 않으면 {"preferences": []} 반환
5. 최대 3개까지만 추출
6. key는 영문 소문자+언더스코어 (예: response_length / preferred_map_service)"""


class ConversationLearner(BaseLearner):
    """대화 학습기 — 선호/패턴 자동 감지 + 메모리 upsert

    Args:
        llm: LLM 프로바이더 (cheap 모델 사용)
        event_store: 이벤트 저장소
        index_manager: 메모리 인덱스 매니저
        enabled: 활성화 여부
        confidence_threshold: 메모리 upsert 최소 신뢰도
        max_preferences_per_query: 쿼리당 최대 감지 수
    """

    def __init__(
        self,
        llm: LLMProvider,
        event_store: EventStore,
        index_manager: IndexManager | None = None,
        enabled: bool = True,
        confidence_threshold: float = 0.7,
        max_preferences_per_query: int = 3,
    ) -> None:
        super().__init__(
            llm=llm,
            event_store=event_store,
            index_manager=index_manager,
            enabled=enabled,
        )
        self._confidence_threshold = confidence_threshold
        self._max_preferences_per_query = max_preferences_per_query
        self._total_preferences_detected: int = 0
        self._total_memory_updates: int = 0

    @property
    def stats(self) -> dict[str, Any]:
        base = super().stats
        base.update({
            "confidence_threshold": self._confidence_threshold,
            "total_preferences_detected": self._total_preferences_detected,
            "total_memory_updates": self._total_memory_updates,
        })
        return base

    async def analyze(
        self,
        request: LearningAnalysisRequest,
    ) -> LearningAnalysisResult:
        """대화 분석 → 선호도 추출 → 메모리 upsert + 이벤트 저장

        Args:
            request: 분석 요청 (query + response + metadata)

        Returns:
            분석 결과 (감지된 선호도 목록 + 저장 이벤트 수 + 업데이트된 토픽)
        """
        # 1. 분석 대상이 너무 짧으면 스킵
        if len(request.query.strip()) < 5:
            return LearningAnalysisResult()

        # 2. cheap 모델로 대화 분석
        user_prompt = self._build_analysis_prompt(request)
        raw_response = await self._call_cheap_llm(
            CONVERSATION_ANALYSIS_SYSTEM,
            user_prompt,
        )

        # 3. JSON 파싱 → Preference 객체 변환
        parsed = self._parse_json_response(raw_response)
        preferences = self._extract_preferences(parsed, request.query)

        if not preferences:
            return LearningAnalysisResult()

        self._total_preferences_detected += len(preferences)

        # 4. 이벤트 저장 + 메모리 upsert
        events_stored = 0
        memory_topics_updated: list[str] = []

        for pref in preferences:
            # 이벤트 저장 (모든 감지 결과)
            event_id = self._store_learning_event(
                LearningEventType.PREFERENCE_DETECTED,
                {
                    "category": pref.category.value,
                    "key": pref.key,
                    "value": pref.value,
                    "confidence": pref.confidence,
                    "evidence": pref.evidence,
                    "source_query": pref.source_query,
                },
            )
            if event_id:
                events_stored += 1

            # 신뢰도 임계값 이상이면 메모리 upsert
            if pref.confidence >= self._confidence_threshold:
                updated = self._update_preference_memory(
                    scope=request.scope,
                    preference=pref,
                )
                if updated and PREFERENCE_MEMORY_DOMAIN not in memory_topics_updated:
                    memory_topics_updated.append(PREFERENCE_MEMORY_DOMAIN)
                    self._total_memory_updates += 1

        return LearningAnalysisResult(
            preferences=preferences,
            events_stored=events_stored,
            memory_topics_updated=memory_topics_updated,
        )

    def _build_analysis_prompt(self, request: LearningAnalysisRequest) -> str:
        """분석 프롬프트 생성

        쿼리 + 응답 + 도구 사용 정보를 포함
        """
        parts = [
            f"사용자 쿼리: {request.query[:2000]}",
        ]

        if request.response:
            # 응답은 500자로 제한 (비용 절약)
            parts.append(f"AI 응답 (요약): {request.response[:500]}")

        if request.tool_calls:
            tool_names = [tc.get("name", "unknown") for tc in request.tool_calls[:5]]
            parts.append(f"사용된 도구: {', '.join(tool_names)}")

        if request.metadata:
            # 유용한 메타데이터만 포함
            if "iterations" in request.metadata:
                parts.append(f"추론 반복: {request.metadata['iterations']}회")
            if "confidence" in request.metadata:
                parts.append(f"응답 신뢰도: {request.metadata['confidence']}")

        return "\n".join(parts)

    def _extract_preferences(
        self,
        parsed: dict[str, Any],
        source_query: str,
    ) -> list[Preference]:
        """파싱된 JSON에서 Preference 객체 변환

        유효성 검증 + 개수 제한 + 정렬 (confidence 내림차순)
        """
        raw_prefs = parsed.get("preferences", [])
        if not isinstance(raw_prefs, list):
            return []

        preferences: list[Preference] = []

        for raw in raw_prefs[:self._max_preferences_per_query]:
            try:
                # 카테고리 변환
                category_str = raw.get("category", "")
                try:
                    category = PreferenceCategory(category_str)
                except ValueError:
                    logger.debug(
                        "learning_invalid_category",
                        category=category_str,
                    )
                    continue

                pref = Preference(
                    category=category,
                    key=str(raw.get("key", "")),
                    value=str(raw.get("value", "")),
                    confidence=float(raw.get("confidence", 0.0)),
                    evidence=str(raw.get("evidence", ""))[:500],
                    source_query=source_query[:500],
                )
                preferences.append(pref)

            except (ValueError, TypeError, KeyError) as e:
                logger.debug(
                    "learning_preference_parse_error",
                    raw=str(raw)[:200],
                    error=str(e),
                )
                continue

        # confidence 내림차순 정렬
        preferences.sort(key=lambda p: p.confidence, reverse=True)
        return preferences

    def _update_preference_memory(
        self,
        scope: str,
        preference: Preference,
    ) -> bool:
        """감지된 선호도를 메모리 토픽에 upsert

        user-preferences 도메인에 마크다운 형식으로 추가
        기존 동일 key가 있으면 덮어쓰기 (최신 값 우선)

        Args:
            scope: 메모리 스코프
            preference: 감지된 선호도

        Returns:
            upsert 성공 여부
        """
        # 선호도를 마크다운 항목으로 변환
        timestamp = preference.detected_at.strftime("%Y-%m-%d %H:%M")
        entry = (
            f"- **{preference.category.value}** | "
            f"`{preference.key}` = `{preference.value}` "
            f"(confidence: {preference.confidence:.2f} / {timestamp})"
        )

        # 메모리에 추가
        return self._upsert_memory_topic(
            scope=scope,
            domain=PREFERENCE_MEMORY_DOMAIN,
            summary="대화에서 자동 감지된 사용자 선호도/습관/패턴",
            content=f"## 자동 감지된 선호도\n\n{entry}",
        )
