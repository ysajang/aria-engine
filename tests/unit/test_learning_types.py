"""ARIA Engine - Phase 5 Step 0 Tests

학습 시스템 공통 인프라 테스트
- LearningConfig: 환경변수 바인딩 + 기본값 + 유효성 검증
- Learning Types: 스키마 유효성 + 직렬화 + 계산 속성
- BaseLearner: 공통 메서드 (safe_analyze / _call_cheap_llm / _parse_json / _store_event / _upsert_memory)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.core.config import LearningConfig
from aria.core.exceptions import LearningError, LearningAnalysisError
from aria.learning.types import (
    BehaviorPattern,
    CorrectionRecord,
    CorrectionType,
    DomainInsight,
    LearningAnalysisRequest,
    LearningAnalysisResult,
    LearningEventType,
    PatternType,
    Preference,
    PreferenceCategory,
    ToolMetrics,
)
from aria.learning.base import BaseLearner


# ============================================================
# LearningConfig Tests
# ============================================================


class TestLearningConfig:
    """LearningConfig 환경변수 + 기본값 테스트"""

    def test_default_values(self):
        config = LearningConfig()
        assert config.enabled is True
        assert config.conversation_learning is True
        assert config.feedback_loop is True
        assert config.tool_optimizer is True
        assert config.prompt_improver is True
        assert config.pattern_predictor is True
        assert config.preference_confidence_threshold == 0.7
        assert config.max_preferences_per_query == 3
        assert config.correction_memory_domain == "correction-log"
        assert config.tool_metrics_window_days == 7
        assert config.min_calls_for_priority == 5
        assert config.low_confidence_threshold == 0.5
        assert config.min_pattern_occurrences == 3
        assert config.pattern_confidence_threshold == 0.6

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ARIA_LEARNING_ENABLED", "false")
        monkeypatch.setenv("ARIA_LEARNING_PREFERENCE_CONFIDENCE_THRESHOLD", "0.9")
        monkeypatch.setenv("ARIA_LEARNING_TOOL_METRICS_WINDOW_DAYS", "30")
        config = LearningConfig()
        assert config.enabled is False
        assert config.preference_confidence_threshold == 0.9
        assert config.tool_metrics_window_days == 30

    def test_individual_axis_disable(self, monkeypatch):
        monkeypatch.setenv("ARIA_LEARNING_CONVERSATION_LEARNING", "false")
        monkeypatch.setenv("ARIA_LEARNING_PATTERN_PREDICTOR", "false")
        config = LearningConfig()
        assert config.enabled is True
        assert config.conversation_learning is False
        assert config.feedback_loop is True
        assert config.pattern_predictor is False


# ============================================================
# LearningEventType Tests
# ============================================================


class TestLearningEventType:
    """학습 이벤트 유형 enum 테스트"""

    def test_all_event_types_are_valid_event_type_strings(self):
        """모든 이벤트 유형이 EventStore event_type 규칙(소문자+언더스코어) 준수"""
        import re
        pattern = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
        for evt in LearningEventType:
            assert pattern.match(evt.value), f"{evt.value} doesn't match event_type pattern"

    def test_axis_coverage(self):
        """5가지 축 모두 이벤트 유형이 존재"""
        values = {e.value for e in LearningEventType}
        # 축 1
        assert "preference_detected" in values
        # 축 2
        assert "correction_detected" in values
        # 축 3
        assert "tool_success" in values
        assert "tool_failure" in values
        # 축 4
        assert "low_confidence_query" in values
        # 축 5
        assert "pattern_detected" in values

    def test_string_enum(self):
        assert LearningEventType.PREFERENCE_DETECTED == "preference_detected"
        assert str(LearningEventType.TOOL_SUCCESS) == "LearningEventType.TOOL_SUCCESS"


# ============================================================
# Preference Tests (축 1)
# ============================================================


class TestPreference:
    """선호도 감지 스키마 테스트"""

    def test_valid_preference(self):
        pref = Preference(
            category=PreferenceCategory.COMMUNICATION_STYLE,
            key="response_length",
            value="concise",
            confidence=0.85,
            evidence="사용자가 간결하게 요청",
            source_query="짧게 답해줘",
        )
        assert pref.category == PreferenceCategory.COMMUNICATION_STYLE
        assert pref.key == "response_length"
        assert pref.confidence == 0.85
        assert pref.detected_at is not None

    def test_key_normalization(self):
        pref = Preference(
            category=PreferenceCategory.TOOL_PREFERENCE,
            key="  Preferred Search Tool  ",
            value="naver",
            confidence=0.7,
        )
        assert pref.key == "preferred_search_tool"

    def test_confidence_bounds(self):
        with pytest.raises(Exception):
            Preference(
                category=PreferenceCategory.DOMAIN_INTEREST,
                key="test",
                value="test",
                confidence=1.5,
            )

    def test_empty_key_rejected(self):
        with pytest.raises(Exception):
            Preference(
                category=PreferenceCategory.DOMAIN_INTEREST,
                key="",
                value="test",
                confidence=0.5,
            )

    def test_serialization(self):
        pref = Preference(
            category=PreferenceCategory.LANGUAGE_PREFERENCE,
            key="primary_language",
            value="korean",
            confidence=0.95,
        )
        data = pref.model_dump()
        assert data["category"] == "language_preference"
        assert data["key"] == "primary_language"


# ============================================================
# CorrectionRecord Tests (축 2)
# ============================================================


class TestCorrectionRecord:
    """교정 기록 스키마 테스트"""

    def test_valid_correction(self):
        record = CorrectionRecord(
            correction_type=CorrectionType.FACTUAL_ERROR,
            user_feedback="그건 틀렸어 / 정답은 B야",
            corrected_behavior="A가 아닌 B로 응답해야 함",
            domain="general-knowledge",
            severity="major",
        )
        assert record.correction_type == CorrectionType.FACTUAL_ERROR
        assert record.severity == "major"

    def test_severity_validation(self):
        with pytest.raises(Exception):
            CorrectionRecord(
                correction_type=CorrectionType.STYLE_CORRECTION,
                user_feedback="test",
                corrected_behavior="test",
                severity="unknown",
            )

    def test_severity_normalization(self):
        record = CorrectionRecord(
            correction_type=CorrectionType.STYLE_CORRECTION,
            user_feedback="test",
            corrected_behavior="test",
            severity="  CRITICAL  ",
        )
        assert record.severity == "critical"

    def test_all_correction_types(self):
        """모든 교정 유형이 정의되어 있는지"""
        types = set(CorrectionType)
        assert len(types) >= 7
        assert CorrectionType.FACTUAL_ERROR in types
        assert CorrectionType.TOOL_MISUSE in types
        assert CorrectionType.EXCESSIVE_RESPONSE in types


# ============================================================
# ToolMetrics Tests (축 3)
# ============================================================


class TestToolMetrics:
    """도구 사용 통계 스키마 + 계산 속성 테스트"""

    def test_empty_metrics(self):
        m = ToolMetrics(tool_name="test_tool")
        assert m.total_calls == 0
        assert m.success_rate == 0.0
        assert m.priority_score == 0.5

    def test_success_rate_calculation(self):
        m = ToolMetrics(tool_name="naver_search", success_count=8, failure_count=2)
        assert m.total_calls == 10
        assert m.success_rate == 0.8

    def test_priority_calculation_high_performer(self):
        m = ToolMetrics(
            tool_name="kakao_search",
            success_count=95,
            failure_count=5,
            avg_latency_ms=500,
            last_used_at=datetime.now(timezone.utc),
        )
        score = m.calculate_priority()
        assert score > 0.7  # 높은 성공률 + 최근 사용 + 빠른 응답

    def test_priority_calculation_poor_performer(self):
        m = ToolMetrics(
            tool_name="slow_tool",
            success_count=3,
            failure_count=7,
            avg_latency_ms=15000,  # 15초 (매우 느림)
            last_used_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        score = m.calculate_priority()
        assert score < 0.3  # 낮은 성공률 + 오래된 사용 + 느린 응답

    def test_priority_unused_tool(self):
        m = ToolMetrics(tool_name="unused")
        score = m.calculate_priority()
        assert score == 0.5  # 미사용 = 중립

    def test_priority_score_bounds(self):
        m = ToolMetrics(
            tool_name="test",
            success_count=100,
            failure_count=0,
            avg_latency_ms=0,
            last_used_at=datetime.now(timezone.utc),
        )
        score = m.calculate_priority()
        assert 0.0 <= score <= 1.0


# ============================================================
# DomainInsight Tests (축 4)
# ============================================================


class TestDomainInsight:
    """저신뢰 도메인 분석 스키마 테스트"""

    def test_valid_insight(self):
        insight = DomainInsight(
            domain="legal",
            avg_confidence=0.35,
            query_count=12,
            sample_queries=["개인정보보호법", "GDPR 위반 사례"],
            knowledge_gaps=["한국 개인정보보호법 최신 개정안"],
            improvement_suggestions=["법률 전문 지식베이스 추가"],
        )
        assert insight.domain == "legal"
        assert insight.query_count == 12

    def test_min_query_count(self):
        with pytest.raises(Exception):
            DomainInsight(domain="test", avg_confidence=0.5, query_count=0)


# ============================================================
# BehaviorPattern Tests (축 5)
# ============================================================


class TestBehaviorPattern:
    """행동 패턴 스키마 테스트"""

    def test_valid_pattern(self):
        pattern = BehaviorPattern(
            pattern_type=PatternType.TEMPORAL,
            pattern_id="monday-briefing",
            description="매주 월요일 아침에 브리핑 요청",
            trigger={"day_of_week": "monday", "time_range": "08:00-10:00"},
            frequency=1.0,
            occurrence_count=5,
            suggested_action="월요일 브리핑을 미리 준비하겠습니다",
            confidence=0.8,
        )
        assert pattern.is_reliable is True

    def test_unreliable_pattern(self):
        pattern = BehaviorPattern(
            pattern_type=PatternType.SEQUENTIAL,
            pattern_id="rare-action",
            description="가끔 발생하는 패턴",
            occurrence_count=1,
            confidence=0.4,
        )
        assert pattern.is_reliable is False  # 횟수 부족 + 낮은 confidence

    def test_pattern_id_normalization(self):
        pattern = BehaviorPattern(
            pattern_type=PatternType.CONTEXTUAL,
            pattern_id="  Monday Briefing  ",
            description="test",
        )
        assert pattern.pattern_id == "monday-briefing"

    def test_all_pattern_types(self):
        types = set(PatternType)
        assert PatternType.TEMPORAL in types
        assert PatternType.SEQUENTIAL in types
        assert PatternType.CONTEXTUAL in types
        assert PatternType.REACTIVE in types


# ============================================================
# LearningAnalysisRequest/Result Tests
# ============================================================


class TestLearningAnalysisModels:
    """분석 요청/결과 모델 테스트"""

    def test_request_minimal(self):
        req = LearningAnalysisRequest(query="테스트 쿼리")
        assert req.confidence == 0.0
        assert req.scope == "global"
        assert req.tool_calls == []

    def test_request_full(self):
        req = LearningAnalysisRequest(
            query="남양주 약국 찾아줘",
            response="약국 10곳을 찾았습니다",
            confidence=0.75,
            tool_calls=[{"name": "kakao_keyword_search", "success": True}],
            scope="global",
            metadata={"iterations": 1},
        )
        assert len(req.tool_calls) == 1
        assert req.metadata["iterations"] == 1

    def test_result_empty(self):
        result = LearningAnalysisResult()
        assert result.preferences == []
        assert result.corrections == []
        assert result.events_stored == 0

    def test_result_with_data(self):
        result = LearningAnalysisResult(
            preferences=[
                Preference(
                    category=PreferenceCategory.TOOL_PREFERENCE,
                    key="search_tool",
                    value="kakao",
                    confidence=0.8,
                )
            ],
            events_stored=3,
            memory_topics_updated=["user-preferences"],
        )
        assert len(result.preferences) == 1
        assert result.events_stored == 3


# ============================================================
# LearningError Tests
# ============================================================


class TestLearningExceptions:
    """학습 시스템 예외 테스트"""

    def test_learning_error(self):
        err = LearningError("분석 실패", learner="ConversationLearner")
        assert err.code == "LEARNING_ERROR"
        assert err.details["learner"] == "ConversationLearner"
        assert "분석 실패" in str(err)

    def test_learning_analysis_error(self):
        err = LearningAnalysisError("JSON 파싱 실패", learner="FeedbackLoop")
        assert err.code == "LEARNING_ANALYSIS_ERROR"
        assert err.details["learner"] == "FeedbackLoop"


# ============================================================
# BaseLearner Tests
# ============================================================


class ConcreteLearner(BaseLearner):
    """테스트용 구체 클래스"""

    def __init__(self, *args, analyze_result=None, analyze_error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._analyze_result = analyze_result or LearningAnalysisResult()
        self._analyze_error = analyze_error

    async def analyze(self, request):
        if self._analyze_error:
            raise self._analyze_error
        return self._analyze_result


def _make_mock_llm():
    """mock LLM 프로바이더 생성"""
    llm = MagicMock()
    llm.config = MagicMock()
    llm.config.cheap_model = "claude-haiku-4-5-20251001"
    llm.complete = AsyncMock(return_value={"content": "{}"})
    return llm


def _make_mock_event_store():
    """mock EventStore 생성"""
    store = MagicMock()
    store.ingest = MagicMock()
    # ingest가 event_id를 가진 Event 반환
    mock_event = MagicMock()
    mock_event.event_id = "test-event-id-123"
    store.ingest.return_value = mock_event
    return store


def _make_mock_index_manager():
    """mock IndexManager 생성"""
    manager = MagicMock()
    manager.get_topic = MagicMock(side_effect=Exception("not found"))
    manager.upsert_topic = MagicMock()
    return manager


class TestBaseLearnerInit:
    """BaseLearner 초기화 테스트"""

    def test_init_defaults(self):
        learner = ConcreteLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        assert learner.enabled is True
        assert learner.stats["total_analyses"] == 0
        assert learner.stats["total_errors"] == 0

    def test_init_disabled(self):
        learner = ConcreteLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            enabled=False,
        )
        assert learner.enabled is False

    def test_stats(self):
        learner = ConcreteLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        stats = learner.stats
        assert stats["learner"] == "ConcreteLearner"
        assert stats["enabled"] is True


class TestBaseLearnerSafeAnalyze:
    """safe_analyze (graceful degradation) 테스트"""

    @pytest.mark.asyncio
    async def test_safe_analyze_success(self):
        expected = LearningAnalysisResult(events_stored=5)
        learner = ConcreteLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            analyze_result=expected,
        )
        req = LearningAnalysisRequest(query="test")
        result = await learner.safe_analyze(req)
        assert result.events_stored == 5
        assert learner.stats["total_analyses"] == 1
        assert learner.stats["total_errors"] == 0

    @pytest.mark.asyncio
    async def test_safe_analyze_disabled(self):
        learner = ConcreteLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            enabled=False,
        )
        req = LearningAnalysisRequest(query="test")
        result = await learner.safe_analyze(req)
        assert result == LearningAnalysisResult()
        assert learner.stats["total_analyses"] == 0

    @pytest.mark.asyncio
    async def test_safe_analyze_graceful_on_error(self):
        """분석 실패해도 빈 결과 반환 — 메인 파이프라인 방해 안 함"""
        learner = ConcreteLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            analyze_error=RuntimeError("LLM 호출 실패"),
        )
        req = LearningAnalysisRequest(query="test")
        result = await learner.safe_analyze(req)
        assert result == LearningAnalysisResult()
        assert learner.stats["total_errors"] == 1


class TestBaseLearnerCallCheapLLM:
    """_call_cheap_llm 테스트"""

    @pytest.mark.asyncio
    async def test_call_cheap_llm_uses_cheap_model(self):
        llm = _make_mock_llm()
        llm.complete = AsyncMock(return_value={"content": "analysis result"})
        learner = ConcreteLearner(llm=llm, event_store=_make_mock_event_store())

        result = await learner._call_cheap_llm("system", "user")
        assert result == "analysis result"
        llm.complete.assert_called_once_with(
            system_prompt="system",
            user_prompt="user",
            model_override="claude-haiku-4-5-20251001",
            max_tokens=1024,
            cache_system_prompt=False,
        )


class TestBaseLearnerParseJson:
    """_parse_json_response 테스트"""

    def test_parse_clean_json(self):
        learner = ConcreteLearner(
            llm=_make_mock_llm(), event_store=_make_mock_event_store()
        )
        result = learner._parse_json_response('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parse_json_with_code_block(self):
        learner = ConcreteLearner(
            llm=_make_mock_llm(), event_store=_make_mock_event_store()
        )
        text = '```json\n{"key": "value"}\n```'
        result = learner._parse_json_response(text)
        assert result == {"key": "value"}

    def test_parse_json_with_generic_code_block(self):
        learner = ConcreteLearner(
            llm=_make_mock_llm(), event_store=_make_mock_event_store()
        )
        text = '```\n{"key": "value"}\n```'
        result = learner._parse_json_response(text)
        assert result == {"key": "value"}

    def test_parse_invalid_json(self):
        learner = ConcreteLearner(
            llm=_make_mock_llm(), event_store=_make_mock_event_store()
        )
        result = learner._parse_json_response("not json at all")
        assert result == {}

    def test_parse_empty_string(self):
        learner = ConcreteLearner(
            llm=_make_mock_llm(), event_store=_make_mock_event_store()
        )
        result = learner._parse_json_response("")
        assert result == {}

    def test_parse_array_returns_empty(self):
        """JSON 배열은 dict가 아니므로 빈 dict 반환"""
        learner = ConcreteLearner(
            llm=_make_mock_llm(), event_store=_make_mock_event_store()
        )
        result = learner._parse_json_response("[1, 2, 3]")
        assert result == {}

    def test_parse_oversized_response(self):
        """10KB 초과 응답 거부"""
        learner = ConcreteLearner(
            llm=_make_mock_llm(), event_store=_make_mock_event_store()
        )
        huge = json.dumps({"data": "x" * 20000})
        result = learner._parse_json_response(huge)
        assert result == {}


class TestBaseLearnerStoreEvent:
    """_store_learning_event 테스트"""

    def test_store_event_success(self):
        store = _make_mock_event_store()
        learner = ConcreteLearner(llm=_make_mock_llm(), event_store=store)

        event_id = learner._store_learning_event(
            LearningEventType.PREFERENCE_DETECTED,
            {"key": "response_length", "value": "concise"},
        )
        assert event_id == "test-event-id-123"
        assert learner._total_events_stored == 1
        store.ingest.assert_called_once()

        # 인입된 EventInput 검증
        call_args = store.ingest.call_args[0][0]
        assert call_args.event_type == "preference_detected"
        assert call_args.source == "aria"

    def test_store_event_failure_graceful(self):
        store = _make_mock_event_store()
        store.ingest.side_effect = RuntimeError("disk full")
        learner = ConcreteLearner(llm=_make_mock_llm(), event_store=store)

        event_id = learner._store_learning_event(
            LearningEventType.TOOL_FAILURE,
            {"tool": "test"},
        )
        assert event_id is None
        assert learner._total_events_stored == 0


class TestBaseLearnerUpsertMemory:
    """_upsert_memory_topic 테스트"""

    def test_upsert_new_topic(self):
        manager = _make_mock_index_manager()
        learner = ConcreteLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            index_manager=manager,
        )

        result = learner._upsert_memory_topic(
            scope="global",
            domain="user-preferences",
            summary="사용자 선호도",
            content="# 선호도\n\n- 간결한 응답 선호",
        )
        assert result is True
        manager.upsert_topic.assert_called_once()

        # 신규 생성이므로 expected_version=None
        call_kwargs = manager.upsert_topic.call_args[1]
        assert call_kwargs["expected_version"] is None

    def test_upsert_existing_topic_merges_content(self):
        manager = _make_mock_index_manager()
        # 기존 토픽 존재하는 케이스
        existing_topic = MagicMock()
        existing_topic.version = 3
        existing_topic.content = "# 기존 내용\n\n- 이전 데이터"
        manager.get_topic.side_effect = None
        manager.get_topic.return_value = existing_topic

        learner = ConcreteLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            index_manager=manager,
        )

        result = learner._upsert_memory_topic(
            scope="global",
            domain="user-preferences",
            summary="사용자 선호도 (업데이트)",
            content="- 새로운 선호: 코드 블록 형식",
        )
        assert result is True

        # read-before-write: expected_version=3
        call_kwargs = manager.upsert_topic.call_args[1]
        assert call_kwargs["expected_version"] == 3
        # 기존 내용 + 새 내용 병합
        assert "기존 내용" in call_kwargs["content"]
        assert "새로운 선호" in call_kwargs["content"]

    def test_upsert_no_index_manager(self):
        """index_manager 미설정 시 스킵"""
        learner = ConcreteLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            index_manager=None,
        )
        result = learner._upsert_memory_topic(
            scope="global", domain="test", summary="test", content="test"
        )
        assert result is False

    def test_upsert_failure_graceful(self):
        """upsert 실패해도 False 반환 (예외 전파 안 함)"""
        manager = _make_mock_index_manager()
        manager.upsert_topic.side_effect = RuntimeError("storage error")
        learner = ConcreteLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            index_manager=manager,
        )
        result = learner._upsert_memory_topic(
            scope="global", domain="test", summary="test", content="test"
        )
        assert result is False


class TestBaseLearnerEnableToggle:
    """enabled 토글 테스트"""

    def test_toggle_enabled(self):
        learner = ConcreteLearner(
            llm=_make_mock_llm(), event_store=_make_mock_event_store()
        )
        assert learner.enabled is True
        learner.enabled = False
        assert learner.enabled is False
        learner.enabled = True
        assert learner.enabled is True
