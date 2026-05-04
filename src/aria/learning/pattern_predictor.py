"""ARIA Engine - Pattern Predictor (Phase 5 축 5)

승재의 반복 행동 패턴 감지 → 선제적 제안

동작 플로우:
    1. /v1/query 호출 시 record_query() — 시간/요일/컨텍스트 메타데이터 저장 (비용 $0)
    2. analyze() — 주기적 분석 (매일 1회)
       → EventStore에서 쿼리 이벤트 집계
       → cheap 모델로 반복 패턴 감지
       → BehaviorPattern 생성 → 메모리 저장
    3. check_triggers() — 현재 시점에서 활성 패턴 매칭
       → 매칭되는 패턴이 있으면 선제적 제안 반환

예시 패턴:
    - TEMPORAL: "매주 월요일 아침에 브리핑 요청" → 월요일 아침에 브리핑 준비
    - SEQUENTIAL: "비용 확인 후 항상 메모리 확인" → 비용 확인 시 메모리도 함께 제공
    - CONTEXTUAL: "에러 발생 시 항상 로그 확인" → 에러 감지 시 로그 분석 자동 제안
    - REACTIVE: "배포 후 항상 헬스체크" → 배포 완료 시 헬스체크 제안

비용: record_query $0 / analyze() ~$0.0003 / check_triggers() $0
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

import structlog

from aria.events.event_store import EventStore
from aria.events.types import EventQuery, EventSeverity
from aria.learning.base import BaseLearner
from aria.learning.types import (
    BehaviorPattern,
    LearningAnalysisRequest,
    LearningAnalysisResult,
    LearningEventType,
    PatternType,
)
from aria.memory.index_manager import IndexManager
from aria.providers.llm_provider import LLMProvider

logger = structlog.get_logger()

# 메모리 토픽 도메인
BEHAVIOR_PATTERNS_DOMAIN = "behavior-patterns"

# 쿼리 기록 이벤트 타입 (EventStore용)
QUERY_RECORD_EVENT_TYPE = "query_recorded"

# 패턴 분석 시스템 프롬프트
PATTERN_ANALYSIS_SYSTEM = """당신은 사용자 행동 패턴 분석 전문가입니다.

사용자의 쿼리 이력을 분석하여 반복적인 행동 패턴을 식별하세요.

4가지 패턴 유형:
- temporal: 특정 시간/요일에 반복되는 행동 (매일 아침 비용 확인 등)
- sequential: A 행동 후 항상 B가 따라오는 패턴 (비용 확인 → 메모리 확인)
- contextual: 특정 상황에서 반복되는 행동 (월요일에 브리핑 요청)
- reactive: 특정 이벤트 발생 시 반복되는 반응 (에러 후 로그 확인)

JSON 형태로만 응답하세요:
{
    "patterns": [
        {
            "pattern_type": "temporal" | "sequential" | "contextual" | "reactive",
            "pattern_id": "고유식별자 (영문소문자-하이픈)",
            "description": "패턴 설명 (한국어 / 사람이 읽을 수 있는 형태)",
            "trigger": {"설명키": "값"},
            "frequency": 0.0,
            "suggested_action": "선제적 제안 내용 (한국어)",
            "confidence": 0.0~1.0
        }
    ]
}

규칙:
1. 최소 3회 이상 반복된 패턴만 보고
2. confidence 보수적으로 — 명확한 반복은 0.8+ / 약한 패턴은 0.4~0.6
3. suggested_action은 자연스러운 한국어로 (로봇같지 않게)
4. 패턴이 없으면 {"patterns": []}
5. 최대 5개까지만"""


class PatternPredictor(BaseLearner):
    """행동 패턴 예측 — 반복 패턴 감지 + 선제적 제안

    Args:
        llm: LLM 프로바이더 (analyze 시에만 사용)
        event_store: 이벤트 저장소
        index_manager: 메모리 인덱스 매니저
        enabled: 활성화 여부
        min_occurrences: 패턴 인정 최소 발생 횟수
        confidence_threshold: 패턴 활성화 신뢰도 임계값
        analysis_window_days: 분석 기간 (일)
    """

    def __init__(
        self,
        llm: LLMProvider,
        event_store: EventStore,
        index_manager: IndexManager | None = None,
        enabled: bool = True,
        min_occurrences: int = 3,
        confidence_threshold: float = 0.6,
        analysis_window_days: int = 14,
    ) -> None:
        super().__init__(
            llm=llm,
            event_store=event_store,
            index_manager=index_manager,
            enabled=enabled,
        )
        self._min_occurrences = min_occurrences
        self._confidence_threshold = confidence_threshold
        self._analysis_window_days = analysis_window_days
        self._known_patterns: list[BehaviorPattern] = []
        self._total_queries_recorded: int = 0
        self._total_patterns_detected: int = 0
        self._total_suggestions_sent: int = 0

    @property
    def stats(self) -> dict[str, Any]:
        base = super().stats
        base.update({
            "min_occurrences": self._min_occurrences,
            "confidence_threshold": self._confidence_threshold,
            "analysis_window_days": self._analysis_window_days,
            "known_patterns": len(self._known_patterns),
            "total_queries_recorded": self._total_queries_recorded,
            "total_patterns_detected": self._total_patterns_detected,
            "total_suggestions_sent": self._total_suggestions_sent,
        })
        return base

    @property
    def known_patterns(self) -> list[BehaviorPattern]:
        return list(self._known_patterns)

    # === 이벤트 기록 (LLM 호출 없음) ===

    def record_query(
        self,
        query: str,
        intent_action: str = "",
        tool_calls: list[str] | None = None,
    ) -> str | None:
        """쿼리를 시간/컨텍스트 메타데이터와 함께 기록

        /v1/query 호출 시마다 실행 — LLM 호출 없이 이벤트만 저장

        Args:
            query: 사용자 쿼리
            intent_action: 의도분석 결과 (search_knowledge/respond/pc_command 등)
            tool_calls: 사용된 도구 이름 목록

        Returns:
            event_id 또는 None
        """
        if not self._enabled:
            return None

        now = datetime.now(timezone.utc)

        event_id = self._store_learning_event(
            LearningEventType.PATTERN_DETECTED,  # 재사용 (query_recorded는 별도 정의 불필요)
            {
                "record_type": "query",
                "query": query[:500],
                "intent_action": intent_action,
                "tool_calls": (tool_calls or [])[:10],
                "hour": now.hour,
                "day_of_week": now.strftime("%A").lower(),
                "date": now.strftime("%Y-%m-%d"),
            },
        )

        if event_id:
            self._total_queries_recorded += 1

        return event_id

    # === 패턴 매칭 (LLM 호출 없음) ===

    def check_triggers(
        self,
        current_hour: int | None = None,
        current_day: str | None = None,
        last_action: str = "",
    ) -> list[BehaviorPattern]:
        """현재 시점에서 활성 패턴 매칭 → 선제적 제안 목록 반환

        LLM 호출 없이 메모리 내 패턴만 확인 → 비용 $0

        Args:
            current_hour: 현재 시각 (0~23 / None이면 자동)
            current_day: 현재 요일 (monday~sunday / None이면 자동)
            last_action: 마지막 수행 행동 (sequential 패턴 매칭용)

        Returns:
            매칭된 BehaviorPattern 목록 (is_reliable 패턴만)
        """
        if not self._enabled or not self._known_patterns:
            return []

        now = datetime.now(timezone.utc)
        hour = current_hour if current_hour is not None else now.hour
        day = current_day or now.strftime("%A").lower()

        matched: list[BehaviorPattern] = []

        for pattern in self._known_patterns:
            if not pattern.active or not pattern.is_reliable:
                continue

            trigger = pattern.trigger

            if pattern.pattern_type == PatternType.TEMPORAL:
                # 시간/요일 매칭
                t_day = trigger.get("day_of_week", "")
                t_hour_start = trigger.get("hour_start", 0)
                t_hour_end = trigger.get("hour_end", 23)

                if t_day and t_day != day:
                    continue
                if not (t_hour_start <= hour <= t_hour_end):
                    continue
                matched.append(pattern)

            elif pattern.pattern_type == PatternType.SEQUENTIAL:
                # 이전 행동 매칭
                t_prev_action = trigger.get("previous_action", "")
                if t_prev_action and t_prev_action == last_action:
                    matched.append(pattern)

            elif pattern.pattern_type == PatternType.CONTEXTUAL:
                # 요일 매칭 (시간 범위 무시)
                t_day = trigger.get("day_of_week", "")
                if t_day and t_day == day:
                    matched.append(pattern)

            elif pattern.pattern_type == PatternType.REACTIVE:
                # reactive는 외부 이벤트 기반 — 여기서는 last_action 매칭
                t_event = trigger.get("trigger_event", "")
                if t_event and t_event == last_action:
                    matched.append(pattern)

        return matched

    # === LLM 기반 분석 (주기적) ===

    async def analyze(
        self,
        request: LearningAnalysisRequest,
    ) -> LearningAnalysisResult:
        """쿼리 이력 분석 → 행동 패턴 감지

        주기적으로 호출 (매일 1회 또는 수동)

        Args:
            request: 분석 요청 (query 필드는 무시 — 이벤트 기반 분석)

        Returns:
            분석 결과 (업데이트된 패턴 수 + 이벤트 수 + 메모리 업데이트)
        """
        # 1. 쿼리 이벤트 조회
        since = (
            datetime.now(timezone.utc) - timedelta(days=self._analysis_window_days)
        ).isoformat()

        events = self._event_store.query(EventQuery(
            source="aria",
            event_type=LearningEventType.PATTERN_DETECTED.value,
            since=since,
            limit=500,
        ))

        # query record 이벤트만 필터
        query_events = [
            e for e in events
            if e.data.get("record_type") == "query"
        ]

        if len(query_events) < self._min_occurrences:
            return LearningAnalysisResult()

        # 2. cheap 모델로 패턴 분석
        user_prompt = self._build_pattern_prompt(query_events)
        raw_response = await self._call_cheap_llm(
            PATTERN_ANALYSIS_SYSTEM,
            user_prompt,
        )

        parsed = self._parse_json_response(raw_response)
        raw_patterns = parsed.get("patterns", [])
        if not isinstance(raw_patterns, list):
            return LearningAnalysisResult()

        # 3. BehaviorPattern 변환 + 필터
        new_patterns = self._extract_patterns(raw_patterns)

        events_stored = 0
        memory_topics_updated: list[str] = []

        for pattern in new_patterns:
            # 이벤트 저장
            event_id = self._store_learning_event(
                LearningEventType.PATTERN_DETECTED,
                {
                    "record_type": "pattern",
                    "pattern_id": pattern.pattern_id,
                    "pattern_type": pattern.pattern_type.value,
                    "description": pattern.description,
                    "confidence": pattern.confidence,
                    "suggested_action": pattern.suggested_action,
                },
            )
            if event_id:
                events_stored += 1

        # 4. 기존 패턴 업데이트
        self._merge_patterns(new_patterns)

        # 5. 메모리에 패턴 축적
        if new_patterns:
            updated = self._update_patterns_memory(scope=request.scope)
            if updated:
                memory_topics_updated.append(BEHAVIOR_PATTERNS_DOMAIN)

        return LearningAnalysisResult(
            patterns_updated=len(new_patterns),
            events_stored=events_stored,
            memory_topics_updated=memory_topics_updated,
        )

    def _build_pattern_prompt(self, events: list) -> str:
        """패턴 분석용 프롬프트 생성"""
        lines = [
            f"쿼리 이력 (최근 {self._analysis_window_days}일 / {len(events)}건):",
            "",
        ]

        # 시간대별 집계
        hour_counts: dict[int, int] = defaultdict(int)
        day_counts: dict[str, int] = defaultdict(int)
        action_counts: dict[str, int] = defaultdict(int)

        for event in events:
            hour_counts[event.data.get("hour", 0)] += 1
            day_counts[event.data.get("day_of_week", "unknown")] += 1
            action = event.data.get("intent_action", "")
            if action:
                action_counts[action] += 1

        lines.append("시간대별 쿼리 빈도:")
        for h in sorted(hour_counts):
            lines.append(f"  - {h}시: {hour_counts[h]}건")

        lines.append("\n요일별 쿼리 빈도:")
        for d, c in sorted(day_counts.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  - {d}: {c}건")

        if action_counts:
            lines.append("\n행동 유형별 빈도:")
            for a, c in sorted(action_counts.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  - {a}: {c}건")

        # 최근 쿼리 샘플 (최대 15개)
        lines.append("\n최근 쿼리 샘플:")
        for event in events[:15]:
            q = event.data.get("query", "")[:100]
            h = event.data.get("hour", "?")
            d = event.data.get("day_of_week", "?")
            lines.append(f"  - [{d} {h}시] {q}")

        return "\n".join(lines)

    def _extract_patterns(self, raw_patterns: list) -> list[BehaviorPattern]:
        """JSON → BehaviorPattern 변환 + 필터"""
        patterns: list[BehaviorPattern] = []

        for raw in raw_patterns[:5]:
            if not isinstance(raw, dict):
                continue

            try:
                pattern_type_str = raw.get("pattern_type", "")
                try:
                    pattern_type = PatternType(pattern_type_str)
                except ValueError:
                    continue

                confidence = float(raw.get("confidence", 0.0))
                if confidence < 0.3:  # 너무 낮은 confidence 제외
                    continue

                pattern = BehaviorPattern(
                    pattern_type=pattern_type,
                    pattern_id=str(raw.get("pattern_id", f"auto-{len(patterns)}"))[:100],
                    description=str(raw.get("description", ""))[:500],
                    trigger=raw.get("trigger", {}),
                    frequency=float(raw.get("frequency", 0.0)),
                    occurrence_count=max(self._min_occurrences, int(raw.get("occurrence_count", self._min_occurrences))),
                    suggested_action=str(raw.get("suggested_action", ""))[:500],
                    confidence=confidence,
                    active=True,
                )
                patterns.append(pattern)
                self._total_patterns_detected += 1

            except (ValueError, TypeError) as e:
                logger.debug("pattern_parse_error", error=str(e))
                continue

        return patterns

    def _merge_patterns(self, new_patterns: list[BehaviorPattern]) -> None:
        """새 패턴을 기존 패턴 목록에 병합

        동일 pattern_id → 업데이트 (confidence/occurrence_count 갱신)
        새 pattern_id → 추가
        """
        existing_ids = {p.pattern_id: i for i, p in enumerate(self._known_patterns)}

        for new_p in new_patterns:
            if new_p.pattern_id in existing_ids:
                idx = existing_ids[new_p.pattern_id]
                old = self._known_patterns[idx]
                # confidence 갱신 (더 높은 값 유지)
                new_p.confidence = max(new_p.confidence, old.confidence)
                new_p.occurrence_count = max(new_p.occurrence_count, old.occurrence_count + 1)
                self._known_patterns[idx] = new_p
            else:
                self._known_patterns.append(new_p)

    def _update_patterns_memory(self, scope: str) -> bool:
        """패턴을 메모리에 저장"""
        if not self._known_patterns:
            return False

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        lines = [f"## 감지된 행동 패턴 ({timestamp})\n"]

        reliable = [p for p in self._known_patterns if p.is_reliable]
        unreliable = [p for p in self._known_patterns if not p.is_reliable]

        if reliable:
            lines.append("**활성 패턴 (신뢰):**")
            for p in reliable:
                lines.append(
                    f"- [{p.pattern_type.value}] {p.description} "
                    f"(confidence {p.confidence:.2f} / {p.occurrence_count}회)"
                )
                if p.suggested_action:
                    lines.append(f"  → 제안: {p.suggested_action}")

        if unreliable:
            lines.append("\n**관찰 중 (미확정):**")
            for p in unreliable:
                lines.append(
                    f"- [{p.pattern_type.value}] {p.description} "
                    f"(confidence {p.confidence:.2f} / {p.occurrence_count}회)"
                )

        return self._upsert_memory_topic(
            scope=scope,
            domain=BEHAVIOR_PATTERNS_DOMAIN,
            summary="승재의 반복 행동 패턴 (자동 감지 / 선제적 제안용)",
            content="\n".join(lines),
        )
