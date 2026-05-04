"""ARIA Engine - ConversationLearner Tests (Phase 5 축 1)

대화 학습기 테스트
- 분석 프롬프트 생성
- 선호도 추출 (JSON 파싱 + 유효성 검증)
- 메모리 자동 upsert (confidence 임계값 기반)
- 이벤트 저장
- graceful degradation
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from aria.learning.conversation_learner import (
    ConversationLearner,
    CONVERSATION_ANALYSIS_SYSTEM,
    PREFERENCE_MEMORY_DOMAIN,
)
from aria.learning.types import (
    LearningAnalysisRequest,
    LearningAnalysisResult,
    LearningEventType,
    Preference,
    PreferenceCategory,
)


# === Test Helpers ===


def _make_mock_llm(response_content: str = "{}"):
    llm = MagicMock()
    llm.config = MagicMock()
    llm.config.cheap_model = "claude-haiku-4-5-20251001"
    llm.complete = AsyncMock(return_value={"content": response_content})
    return llm


def _make_mock_event_store():
    store = MagicMock()
    mock_event = MagicMock()
    mock_event.event_id = "evt-123"
    store.ingest = MagicMock(return_value=mock_event)
    return store


def _make_mock_index_manager(existing_topic=None):
    manager = MagicMock()
    if existing_topic:
        manager.get_topic.return_value = existing_topic
    else:
        manager.get_topic.side_effect = Exception("not found")
    manager.upsert_topic = MagicMock()
    return manager


def _make_llm_response(preferences: list[dict]) -> str:
    return json.dumps({"preferences": preferences})


# ============================================================
# Init + Config Tests
# ============================================================


class TestConversationLearnerInit:
    """초기화 + 설정 테스트"""

    def test_default_init(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        assert learner.enabled is True
        assert learner._confidence_threshold == 0.7
        assert learner._max_preferences_per_query == 3

    def test_custom_threshold(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            confidence_threshold=0.9,
            max_preferences_per_query=5,
        )
        assert learner._confidence_threshold == 0.9
        assert learner._max_preferences_per_query == 5

    def test_stats_includes_conversation_fields(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        stats = learner.stats
        assert stats["learner"] == "ConversationLearner"
        assert stats["confidence_threshold"] == 0.7
        assert stats["total_preferences_detected"] == 0
        assert stats["total_memory_updates"] == 0


# ============================================================
# Analysis Prompt Tests
# ============================================================


class TestBuildAnalysisPrompt:
    """_build_analysis_prompt 테스트"""

    def test_basic_prompt(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        req = LearningAnalysisRequest(query="남양주 약국 찾아줘")
        prompt = learner._build_analysis_prompt(req)
        assert "남양주 약국 찾아줘" in prompt

    def test_prompt_with_response(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        req = LearningAnalysisRequest(
            query="짧게 답해줘",
            response="네 간결하게 답변하겠습니다",
        )
        prompt = learner._build_analysis_prompt(req)
        assert "짧게 답해줘" in prompt
        assert "간결하게" in prompt

    def test_prompt_with_tool_calls(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        req = LearningAnalysisRequest(
            query="test",
            tool_calls=[
                {"name": "kakao_keyword_search"},
                {"name": "naver_local_search"},
            ],
        )
        prompt = learner._build_analysis_prompt(req)
        assert "kakao_keyword_search" in prompt
        assert "naver_local_search" in prompt

    def test_prompt_truncates_long_query(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        long_query = "x" * 5000
        req = LearningAnalysisRequest(query=long_query)
        prompt = learner._build_analysis_prompt(req)
        # 2000자로 잘림
        assert len(prompt) < 5000

    def test_prompt_with_metadata(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        req = LearningAnalysisRequest(
            query="test",
            metadata={"iterations": 2, "confidence": 0.8},
        )
        prompt = learner._build_analysis_prompt(req)
        assert "2회" in prompt
        assert "0.8" in prompt


# ============================================================
# Extract Preferences Tests
# ============================================================


class TestExtractPreferences:
    """_extract_preferences 테스트"""

    def test_extract_valid_preferences(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        parsed = {
            "preferences": [
                {
                    "category": "communication_style",
                    "key": "response_length",
                    "value": "concise",
                    "confidence": 0.85,
                    "evidence": "사용자가 짧게 답해달라고 요청",
                }
            ]
        }
        prefs = learner._extract_preferences(parsed, "짧게 답해줘")
        assert len(prefs) == 1
        assert prefs[0].category == PreferenceCategory.COMMUNICATION_STYLE
        assert prefs[0].key == "response_length"
        assert prefs[0].value == "concise"
        assert prefs[0].confidence == 0.85
        assert prefs[0].source_query == "짧게 답해줘"

    def test_extract_empty_preferences(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        prefs = learner._extract_preferences({"preferences": []}, "test")
        assert prefs == []

    def test_extract_no_preferences_key(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        prefs = learner._extract_preferences({}, "test")
        assert prefs == []

    def test_extract_invalid_category_skipped(self):
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        parsed = {
            "preferences": [
                {
                    "category": "nonexistent_category",
                    "key": "test",
                    "value": "test",
                    "confidence": 0.8,
                }
            ]
        }
        prefs = learner._extract_preferences(parsed, "test")
        assert prefs == []

    def test_extract_max_limit(self):
        """max_preferences_per_query 초과 시 잘림"""
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            max_preferences_per_query=2,
        )
        parsed = {
            "preferences": [
                {"category": "communication_style", "key": f"pref_{i}", "value": "v", "confidence": 0.8}
                for i in range(5)
            ]
        }
        prefs = learner._extract_preferences(parsed, "test")
        assert len(prefs) == 2

    def test_extract_sorted_by_confidence(self):
        """confidence 내림차순 정렬"""
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        parsed = {
            "preferences": [
                {"category": "communication_style", "key": "low", "value": "v", "confidence": 0.3},
                {"category": "tool_preference", "key": "high", "value": "v", "confidence": 0.9},
                {"category": "domain_interest", "key": "mid", "value": "v", "confidence": 0.6},
            ]
        }
        prefs = learner._extract_preferences(parsed, "test")
        assert prefs[0].key == "high"
        assert prefs[1].key == "mid"
        assert prefs[2].key == "low"

    def test_extract_malformed_entry_skipped(self):
        """잘못된 엔트리는 스킵 (다른 유효 엔트리는 유지)"""
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        parsed = {
            "preferences": [
                {"category": "communication_style", "key": "", "value": "v", "confidence": 0.8},  # 빈 key → 실패
                {"category": "tool_preference", "key": "valid", "value": "kakao", "confidence": 0.7},  # 유효
            ]
        }
        prefs = learner._extract_preferences(parsed, "test")
        assert len(prefs) == 1
        assert prefs[0].key == "valid"

    def test_extract_non_list_preferences(self):
        """preferences가 list가 아닌 경우"""
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        prefs = learner._extract_preferences({"preferences": "not a list"}, "test")
        assert prefs == []


# ============================================================
# Full Analyze Flow Tests
# ============================================================


class TestConversationLearnerAnalyze:
    """analyze() 전체 플로우 테스트"""

    @pytest.mark.asyncio
    async def test_analyze_detects_preference(self):
        """선호 감지 → 이벤트 저장 + 메모리 upsert"""
        llm_response = _make_llm_response([
            {
                "category": "communication_style",
                "key": "response_length",
                "value": "concise",
                "confidence": 0.85,
                "evidence": "사용자가 짧게 요청",
            }
        ])
        llm = _make_mock_llm(llm_response)
        store = _make_mock_event_store()
        manager = _make_mock_index_manager()

        learner = ConversationLearner(
            llm=llm,
            event_store=store,
            index_manager=manager,
            confidence_threshold=0.7,
        )

        req = LearningAnalysisRequest(
            query="짧게 답변해줘",
            response="네 간결하게 답하겠습니다",
        )
        result = await learner.analyze(req)

        # 선호 감지됨
        assert len(result.preferences) == 1
        assert result.preferences[0].key == "response_length"

        # 이벤트 저장됨
        assert result.events_stored == 1
        store.ingest.assert_called_once()

        # 메모리 upsert됨 (confidence 0.85 >= threshold 0.7)
        assert PREFERENCE_MEMORY_DOMAIN in result.memory_topics_updated
        manager.upsert_topic.assert_called_once()

    @pytest.mark.asyncio
    async def test_analyze_below_threshold_no_memory_update(self):
        """confidence < threshold → 이벤트는 저장하되 메모리 upsert 안 함"""
        llm_response = _make_llm_response([
            {
                "category": "domain_interest",
                "key": "crypto_trading",
                "value": "interested",
                "confidence": 0.4,
                "evidence": "암묵적 관심",
            }
        ])
        llm = _make_mock_llm(llm_response)
        store = _make_mock_event_store()
        manager = _make_mock_index_manager()

        learner = ConversationLearner(
            llm=llm,
            event_store=store,
            index_manager=manager,
            confidence_threshold=0.7,
        )

        req = LearningAnalysisRequest(query="비트코인 가격 알려줘")
        result = await learner.analyze(req)

        assert len(result.preferences) == 1
        assert result.events_stored == 1  # 이벤트는 저장
        assert result.memory_topics_updated == []  # 메모리는 업데이트 안 함
        manager.upsert_topic.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyze_short_query_skipped(self):
        """5자 미만 쿼리는 분석 스킵"""
        llm = _make_mock_llm()
        learner = ConversationLearner(
            llm=llm,
            event_store=_make_mock_event_store(),
        )

        req = LearningAnalysisRequest(query="hi")
        result = await learner.analyze(req)

        assert result == LearningAnalysisResult()
        llm.complete.assert_not_called()  # LLM 호출 없음

    @pytest.mark.asyncio
    async def test_analyze_no_preferences_detected(self):
        """선호 감지 안 됨 → 빈 결과"""
        llm = _make_mock_llm('{"preferences": []}')
        learner = ConversationLearner(
            llm=llm,
            event_store=_make_mock_event_store(),
        )

        req = LearningAnalysisRequest(query="파이썬에서 리스트 정렬 어떻게 해?")
        result = await learner.analyze(req)

        assert result.preferences == []
        assert result.events_stored == 0

    @pytest.mark.asyncio
    async def test_analyze_multiple_preferences(self):
        """다중 선호 감지"""
        llm_response = _make_llm_response([
            {
                "category": "communication_style",
                "key": "language",
                "value": "korean",
                "confidence": 0.95,
                "evidence": "한국어로 대화",
            },
            {
                "category": "tool_preference",
                "key": "map_service",
                "value": "kakao",
                "confidence": 0.75,
                "evidence": "카카오맵으로 검색",
            },
        ])
        llm = _make_mock_llm(llm_response)
        store = _make_mock_event_store()
        manager = _make_mock_index_manager()

        learner = ConversationLearner(
            llm=llm,
            event_store=store,
            index_manager=manager,
            confidence_threshold=0.7,
        )

        req = LearningAnalysisRequest(query="카카오맵으로 남양주 약국 찾아줘")
        result = await learner.analyze(req)

        assert len(result.preferences) == 2
        assert result.events_stored == 2
        # 두 선호 모두 threshold 이상 → 메모리 업데이트
        assert PREFERENCE_MEMORY_DOMAIN in result.memory_topics_updated

    @pytest.mark.asyncio
    async def test_analyze_llm_returns_invalid_json(self):
        """LLM이 잘못된 JSON 반환 → 빈 결과 (에러 아님)"""
        llm = _make_mock_llm("This is not JSON at all")
        learner = ConversationLearner(
            llm=llm,
            event_store=_make_mock_event_store(),
        )

        req = LearningAnalysisRequest(query="테스트 쿼리입니다")
        result = await learner.analyze(req)

        assert result == LearningAnalysisResult()

    @pytest.mark.asyncio
    async def test_safe_analyze_llm_failure(self):
        """LLM 호출 실패 → safe_analyze가 빈 결과 반환"""
        llm = _make_mock_llm()
        llm.complete.side_effect = RuntimeError("API error")

        learner = ConversationLearner(
            llm=llm,
            event_store=_make_mock_event_store(),
        )

        req = LearningAnalysisRequest(query="테스트 쿼리입니다")
        result = await learner.safe_analyze(req)

        assert result == LearningAnalysisResult()
        assert learner.stats["total_errors"] == 1


# ============================================================
# Memory Update Tests
# ============================================================


class TestUpdatePreferenceMemory:
    """_update_preference_memory 테스트"""

    def test_update_new_preference(self):
        manager = _make_mock_index_manager()
        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            index_manager=manager,
        )

        pref = Preference(
            category=PreferenceCategory.COMMUNICATION_STYLE,
            key="response_length",
            value="concise",
            confidence=0.85,
        )

        result = learner._update_preference_memory("global", pref)
        assert result is True
        manager.upsert_topic.assert_called_once()

        # upsert 내용 검증
        call_kwargs = manager.upsert_topic.call_args[1]
        assert call_kwargs["domain"] == PREFERENCE_MEMORY_DOMAIN
        assert call_kwargs["scope"] == "global"
        assert "response_length" in call_kwargs["content"]
        assert "concise" in call_kwargs["content"]

    def test_update_merges_with_existing(self):
        """기존 선호도가 있으면 내용 병합"""
        existing = MagicMock()
        existing.version = 2
        existing.content = "## 자동 감지된 선호도\n\n- 기존 선호"
        manager = _make_mock_index_manager(existing_topic=existing)

        learner = ConversationLearner(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            index_manager=manager,
        )

        pref = Preference(
            category=PreferenceCategory.TOOL_PREFERENCE,
            key="search_engine",
            value="naver",
            confidence=0.9,
        )

        result = learner._update_preference_memory("global", pref)
        assert result is True

        call_kwargs = manager.upsert_topic.call_args[1]
        assert call_kwargs["expected_version"] == 2  # read-before-write
        assert "기존 선호" in call_kwargs["content"]
        assert "search_engine" in call_kwargs["content"]


# ============================================================
# System Prompt Tests
# ============================================================


class TestSystemPrompt:
    """시스템 프롬프트 검증"""

    def test_prompt_contains_all_categories(self):
        for cat in PreferenceCategory:
            assert cat.value in CONVERSATION_ANALYSIS_SYSTEM

    def test_prompt_instructs_json_only(self):
        assert "JSON" in CONVERSATION_ANALYSIS_SYSTEM

    def test_prompt_has_conservative_confidence(self):
        assert "보수적" in CONVERSATION_ANALYSIS_SYSTEM

    def test_prompt_limits_max_extraction(self):
        assert "최대 3개" in CONVERSATION_ANALYSIS_SYSTEM


# ============================================================
# Integration-style Tests
# ============================================================


class TestConversationLearnerStats:
    """통계 누적 테스트"""

    @pytest.mark.asyncio
    async def test_stats_accumulate(self):
        llm_response = _make_llm_response([
            {
                "category": "communication_style",
                "key": "tone",
                "value": "casual",
                "confidence": 0.8,
            }
        ])
        llm = _make_mock_llm(llm_response)
        store = _make_mock_event_store()
        manager = _make_mock_index_manager()

        learner = ConversationLearner(
            llm=llm,
            event_store=store,
            index_manager=manager,
        )

        # 2번 분석 실행
        for _ in range(2):
            req = LearningAnalysisRequest(query="가볍게 대화하자")
            await learner.safe_analyze(req)

        stats = learner.stats
        assert stats["total_analyses"] == 2
        assert stats["total_preferences_detected"] == 2
        assert stats["total_memory_updates"] == 2
