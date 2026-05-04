"""ARIA Engine - PatternPredictor Tests (Phase 5 축 5)

행동 패턴 예측 테스트
- record_query: 쿼리 이벤트 기록
- check_triggers: 패턴 매칭 + 선제적 제안
- analyze: LLM 기반 패턴 감지
- 패턴 병합/메모리 축적
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.events.types import Event, EventQuery, EventSeverity
from aria.learning.pattern_predictor import (
    BEHAVIOR_PATTERNS_DOMAIN,
    PATTERN_ANALYSIS_SYSTEM,
    PatternPredictor,
)
from aria.learning.types import (
    BehaviorPattern,
    LearningAnalysisRequest,
    LearningAnalysisResult,
    LearningEventType,
    PatternType,
)


# === Test Helpers ===


def _make_mock_llm(response_content: str = "{}"):
    llm = MagicMock()
    llm.config = MagicMock()
    llm.config.cheap_model = "claude-haiku-4-5-20251001"
    llm.complete = AsyncMock(return_value={"content": response_content})
    return llm


def _make_query_event(query: str, hour: int = 9, day: str = "monday", action: str = "") -> Event:
    return Event(
        event_id=f"evt-q-{hash(query) % 10000}",
        event_type=LearningEventType.PATTERN_DETECTED.value,
        source="aria",
        severity=EventSeverity.INFO,
        data={
            "record_type": "query",
            "query": query,
            "hour": hour,
            "day_of_week": day,
            "intent_action": action,
            "date": "2026-05-04",
        },
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _make_mock_event_store(query_events=None):
    store = MagicMock()
    mock_event = MagicMock()
    mock_event.event_id = "evt-pp-001"
    store.ingest = MagicMock(return_value=mock_event)

    def mock_query(q: EventQuery):
        if q.event_type == LearningEventType.PATTERN_DETECTED.value:
            return query_events or []
        return []

    store.query = MagicMock(side_effect=mock_query)
    return store


def _make_mock_index_manager():
    manager = MagicMock()
    manager.get_topic.side_effect = Exception("not found")
    manager.upsert_topic = MagicMock()
    return manager


def _make_test_pattern(
    pattern_type: PatternType = PatternType.TEMPORAL,
    pattern_id: str = "test-pattern",
    trigger: dict = None,
    confidence: float = 0.8,
    occurrence_count: int = 5,
) -> BehaviorPattern:
    return BehaviorPattern(
        pattern_type=pattern_type,
        pattern_id=pattern_id,
        description="테스트 패턴",
        trigger=trigger or {},
        frequency=1.0,
        occurrence_count=occurrence_count,
        suggested_action="테스트 제안",
        confidence=confidence,
        active=True,
    )


# ============================================================
# Init Tests
# ============================================================


class TestPatternPredictorInit:

    def test_default_init(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        assert pp.enabled is True
        assert pp._min_occurrences == 3
        assert pp._confidence_threshold == 0.6
        assert pp._analysis_window_days == 14
        assert pp.known_patterns == []

    def test_stats(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        stats = pp.stats
        assert stats["learner"] == "PatternPredictor"
        assert stats["known_patterns"] == 0
        assert stats["total_queries_recorded"] == 0
        assert stats["total_patterns_detected"] == 0


# ============================================================
# Record Query Tests
# ============================================================


class TestRecordQuery:

    def test_record_basic(self):
        store = _make_mock_event_store()
        pp = PatternPredictor(llm=_make_mock_llm(), event_store=store)

        event_id = pp.record_query(
            query="비용 현황 알려줘",
            intent_action="search_knowledge",
            tool_calls=["cost_check"],
        )

        assert event_id == "evt-pp-001"
        assert pp._total_queries_recorded == 1

        call_args = store.ingest.call_args[0][0]
        assert call_args.data["record_type"] == "query"
        assert call_args.data["query"] == "비용 현황 알려줘"
        assert "hour" in call_args.data
        assert "day_of_week" in call_args.data

    def test_record_disabled(self):
        store = _make_mock_event_store()
        pp = PatternPredictor(
            llm=_make_mock_llm(), event_store=store, enabled=False
        )

        event_id = pp.record_query("test")
        assert event_id is None
        store.ingest.assert_not_called()


# ============================================================
# Check Triggers Tests
# ============================================================


class TestCheckTriggers:

    def test_temporal_match(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        pp._known_patterns = [
            _make_test_pattern(
                pattern_type=PatternType.TEMPORAL,
                pattern_id="morning-cost",
                trigger={"day_of_week": "monday", "hour_start": 8, "hour_end": 10},
            )
        ]

        matched = pp.check_triggers(current_hour=9, current_day="monday")
        assert len(matched) == 1
        assert matched[0].pattern_id == "morning-cost"

    def test_temporal_no_match(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        pp._known_patterns = [
            _make_test_pattern(
                pattern_type=PatternType.TEMPORAL,
                trigger={"day_of_week": "monday", "hour_start": 8, "hour_end": 10},
            )
        ]

        matched = pp.check_triggers(current_hour=15, current_day="monday")
        assert matched == []

    def test_sequential_match(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        pp._known_patterns = [
            _make_test_pattern(
                pattern_type=PatternType.SEQUENTIAL,
                pattern_id="cost-then-memory",
                trigger={"previous_action": "cost_check"},
            )
        ]

        matched = pp.check_triggers(last_action="cost_check")
        assert len(matched) == 1

    def test_contextual_match(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        pp._known_patterns = [
            _make_test_pattern(
                pattern_type=PatternType.CONTEXTUAL,
                trigger={"day_of_week": "friday"},
            )
        ]

        matched = pp.check_triggers(current_day="friday")
        assert len(matched) == 1

    def test_reactive_match(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        pp._known_patterns = [
            _make_test_pattern(
                pattern_type=PatternType.REACTIVE,
                trigger={"trigger_event": "deploy_complete"},
            )
        ]

        matched = pp.check_triggers(last_action="deploy_complete")
        assert len(matched) == 1

    def test_skip_unreliable_patterns(self):
        """is_reliable=False인 패턴은 매칭 안 함"""
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        pp._known_patterns = [
            _make_test_pattern(
                pattern_type=PatternType.TEMPORAL,
                trigger={"hour_start": 0, "hour_end": 23},
                confidence=0.3,  # 낮은 confidence
                occurrence_count=1,  # 적은 발생
            )
        ]

        matched = pp.check_triggers(current_hour=9)
        assert matched == []

    def test_skip_inactive_patterns(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        pattern = _make_test_pattern(
            pattern_type=PatternType.TEMPORAL,
            trigger={"hour_start": 0, "hour_end": 23},
        )
        pattern.active = False
        pp._known_patterns = [pattern]

        matched = pp.check_triggers(current_hour=9)
        assert matched == []

    def test_empty_patterns(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        matched = pp.check_triggers()
        assert matched == []


# ============================================================
# Analyze Tests
# ============================================================


class TestPatternPredictorAnalyze:

    @pytest.mark.asyncio
    async def test_analyze_detects_pattern(self):
        events = [
            _make_query_event("비용 현황", 9, "monday"),
            _make_query_event("이번 달 비용", 9, "monday"),
            _make_query_event("비용 확인해줘", 10, "monday"),
        ]
        store = _make_mock_event_store(events)

        llm_response = json.dumps({
            "patterns": [
                {
                    "pattern_type": "temporal",
                    "pattern_id": "monday-cost-check",
                    "description": "매주 월요일 아침에 비용 확인",
                    "trigger": {"day_of_week": "monday", "hour_start": 9, "hour_end": 10},
                    "frequency": 1.0,
                    "suggested_action": "월요일 아침 비용 현황을 미리 준비하겠습니다",
                    "confidence": 0.85,
                }
            ]
        })
        llm = _make_mock_llm(llm_response)
        manager = _make_mock_index_manager()

        pp = PatternPredictor(
            llm=llm, event_store=store, index_manager=manager,
        )

        req = LearningAnalysisRequest(query="analysis")
        result = await pp.analyze(req)

        assert result.patterns_updated == 1
        assert result.events_stored >= 1
        assert BEHAVIOR_PATTERNS_DOMAIN in result.memory_topics_updated
        assert len(pp.known_patterns) == 1
        assert pp.known_patterns[0].pattern_id == "monday-cost-check"

    @pytest.mark.asyncio
    async def test_analyze_too_few_events(self):
        events = [_make_query_event("q1", 9)]  # 1건만
        store = _make_mock_event_store(events)
        llm = _make_mock_llm()

        pp = PatternPredictor(
            llm=llm, event_store=store, min_occurrences=3,
        )

        req = LearningAnalysisRequest(query="analysis")
        result = await pp.analyze(req)

        assert result == LearningAnalysisResult()
        llm.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyze_no_events(self):
        store = _make_mock_event_store()
        pp = PatternPredictor(llm=_make_mock_llm(), event_store=store)

        req = LearningAnalysisRequest(query="analysis")
        result = await pp.analyze(req)

        assert result == LearningAnalysisResult()


# ============================================================
# Pattern Merge Tests
# ============================================================


class TestPatternMerge:

    def test_merge_new_pattern(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )

        new_patterns = [_make_test_pattern(pattern_id="new-one")]
        pp._merge_patterns(new_patterns)

        assert len(pp._known_patterns) == 1
        assert pp._known_patterns[0].pattern_id == "new-one"

    def test_merge_existing_pattern_updates(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        pp._known_patterns = [
            _make_test_pattern(pattern_id="existing", confidence=0.6, occurrence_count=3)
        ]

        new_patterns = [
            _make_test_pattern(pattern_id="existing", confidence=0.8, occurrence_count=3)
        ]
        pp._merge_patterns(new_patterns)

        assert len(pp._known_patterns) == 1
        assert pp._known_patterns[0].confidence == 0.8  # 높은 값 유지
        assert pp._known_patterns[0].occurrence_count == 4  # old+1


# ============================================================
# Extract Patterns Tests
# ============================================================


class TestExtractPatterns:

    def test_extract_valid(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        raw = [
            {
                "pattern_type": "temporal",
                "pattern_id": "test",
                "description": "테스트",
                "confidence": 0.8,
                "suggested_action": "제안",
            }
        ]

        patterns = pp._extract_patterns(raw)
        assert len(patterns) == 1
        assert patterns[0].pattern_type == PatternType.TEMPORAL

    def test_extract_low_confidence_filtered(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        raw = [
            {"pattern_type": "temporal", "pattern_id": "weak", "description": "약한 패턴", "confidence": 0.1}
        ]

        patterns = pp._extract_patterns(raw)
        assert patterns == []

    def test_extract_invalid_type_filtered(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        raw = [
            {"pattern_type": "nonexistent", "pattern_id": "bad", "description": "잘못된 유형", "confidence": 0.8}
        ]

        patterns = pp._extract_patterns(raw)
        assert patterns == []

    def test_extract_max_limit(self):
        pp = PatternPredictor(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        raw = [
            {"pattern_type": "temporal", "pattern_id": f"p{i}", "description": f"패턴{i}", "confidence": 0.8}
            for i in range(10)
        ]

        patterns = pp._extract_patterns(raw)
        assert len(patterns) == 5  # 최대 5개


# ============================================================
# System Prompt Tests
# ============================================================


class TestPatternSystemPrompt:

    def test_prompt_covers_all_types(self):
        for pt in PatternType:
            assert pt.value in PATTERN_ANALYSIS_SYSTEM

    def test_prompt_has_min_threshold(self):
        assert "3회" in PATTERN_ANALYSIS_SYSTEM


# ============================================================
# Graceful Degradation Tests
# ============================================================


class TestPatternGraceful:

    @pytest.mark.asyncio
    async def test_safe_analyze_on_failure(self):
        events = [_make_query_event(f"q{i}", 9) for i in range(5)]
        store = _make_mock_event_store(events)
        llm = _make_mock_llm()
        llm.complete.side_effect = RuntimeError("API down")

        pp = PatternPredictor(llm=llm, event_store=store)

        req = LearningAnalysisRequest(query="analysis")
        result = await pp.safe_analyze(req)

        assert result == LearningAnalysisResult()
        assert pp.stats["total_errors"] == 1
