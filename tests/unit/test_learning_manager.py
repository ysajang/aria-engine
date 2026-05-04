"""ARIA Engine - LearningManager + App Integration Tests (Phase 5 통합)

LearningManager 오케스트레이터 테스트
- 초기화 (config 기반 축별 on/off)
- post_query_hook (5개 모듈 연동)
- 통계 수집
- graceful degradation
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.core.config import AriaConfig, LearningConfig
from aria.learning.manager import LearningManager
from aria.learning.types import LearningAnalysisResult


# === Test Helpers ===


def _make_mock_llm():
    llm = MagicMock()
    llm.config = MagicMock()
    llm.config.cheap_model = "claude-haiku-4-5-20251001"
    llm.complete = AsyncMock(return_value={"content": '{"preferences": []}'})
    return llm


def _make_mock_event_store():
    store = MagicMock()
    mock_event = MagicMock()
    mock_event.event_id = "evt-lm-001"
    store.ingest = MagicMock(return_value=mock_event)
    store.query = MagicMock(return_value=[])
    return store


def _make_mock_index_manager():
    manager = MagicMock()
    manager.get_topic.side_effect = Exception("not found")
    manager.upsert_topic = MagicMock()
    return manager


# ============================================================
# Init Tests
# ============================================================


class TestLearningManagerInit:

    def test_default_init(self):
        lm = LearningManager(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        assert lm.enabled is True
        assert lm.conversation_learner.enabled is True
        assert lm.feedback_loop.enabled is True
        assert lm.tool_optimizer.enabled is True
        assert lm.prompt_improver.enabled is True
        assert lm.pattern_predictor.enabled is True

    def test_init_with_config(self):
        config = MagicMock(spec=AriaConfig)
        config.learning = MagicMock(spec=LearningConfig)
        config.learning.enabled = True
        config.learning.conversation_learning = True
        config.learning.feedback_loop = False  # 축 2 비활성화
        config.learning.tool_optimizer = True
        config.learning.prompt_improver = True
        config.learning.pattern_predictor = False  # 축 5 비활성화
        config.learning.preference_confidence_threshold = 0.8
        config.learning.max_preferences_per_query = 5
        config.learning.correction_memory_domain = "correction-log"
        config.learning.tool_metrics_window_days = 14
        config.learning.min_calls_for_priority = 10
        config.learning.low_confidence_threshold = 0.4
        config.learning.min_pattern_occurrences = 5
        config.learning.pattern_confidence_threshold = 0.7

        lm = LearningManager(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            config=config,
        )

        assert lm.enabled is True
        assert lm.conversation_learner.enabled is True
        assert lm.feedback_loop.enabled is False
        assert lm.tool_optimizer.enabled is True
        assert lm.prompt_improver.enabled is True
        assert lm.pattern_predictor.enabled is False

    def test_init_fully_disabled(self):
        config = MagicMock(spec=AriaConfig)
        config.learning = MagicMock(spec=LearningConfig)
        config.learning.enabled = False
        # 개별 축은 True여도 전체 비활성화면 모두 꺼짐
        config.learning.conversation_learning = True
        config.learning.feedback_loop = True
        config.learning.tool_optimizer = True
        config.learning.prompt_improver = True
        config.learning.pattern_predictor = True
        config.learning.preference_confidence_threshold = 0.7
        config.learning.max_preferences_per_query = 3
        config.learning.correction_memory_domain = "correction-log"
        config.learning.tool_metrics_window_days = 7
        config.learning.min_calls_for_priority = 5
        config.learning.low_confidence_threshold = 0.5
        config.learning.min_pattern_occurrences = 3
        config.learning.pattern_confidence_threshold = 0.6

        lm = LearningManager(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            config=config,
        )

        assert lm.enabled is False
        assert lm.conversation_learner.enabled is False
        assert lm.feedback_loop.enabled is False


# ============================================================
# Stats Tests
# ============================================================


class TestLearningManagerStats:

    def test_stats_structure(self):
        lm = LearningManager(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )

        stats = lm.get_stats()
        assert stats["enabled"] is True
        assert "axes" in stats
        assert "conversation_learner" in stats["axes"]
        assert "feedback_loop" in stats["axes"]
        assert "tool_optimizer" in stats["axes"]
        assert "prompt_improver" in stats["axes"]
        assert "pattern_predictor" in stats["axes"]

    def test_stats_per_axis(self):
        lm = LearningManager(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )

        stats = lm.get_stats()
        for axis_name, axis_stats in stats["axes"].items():
            assert "learner" in axis_stats
            assert "enabled" in axis_stats
            assert "total_analyses" in axis_stats


# ============================================================
# Post Query Hook Tests
# ============================================================


class TestPostQueryHook:

    @pytest.mark.asyncio
    async def test_hook_runs_all_axes(self):
        llm = _make_mock_llm()
        store = _make_mock_event_store()
        manager = _make_mock_index_manager()

        lm = LearningManager(
            llm=llm, event_store=store, index_manager=manager,
        )

        await lm.post_query_hook(
            query="남양주 약국 찾아줘",
            answer="약국 10곳을 찾았습니다",
            confidence=0.75,
            scope="global",
            tool_calls_made=2,
            tool_results=[
                {"name": "kakao_search", "success": True, "latency_ms": 250},
            ],
            intent_action="search_knowledge",
        )

        # LLM 호출됨 (축 1 conversation_learner)
        assert llm.complete.call_count >= 1

        # 이벤트 저장됨 (축 3 record_execution + 축 5 record_query)
        assert store.ingest.call_count >= 1

    @pytest.mark.asyncio
    async def test_hook_with_feedback(self):
        """이전 쿼리가 있으면 축 2 피드백 분석도 실행"""
        llm = _make_mock_llm()
        # 축 1 응답 + 축 2 응답 모두 빈 결과
        llm.complete = AsyncMock(return_value={"content": '{"preferences": [], "has_feedback": false}'})

        lm = LearningManager(
            llm=llm, event_store=_make_mock_event_store(),
        )

        await lm.post_query_hook(
            query="아니 틀렸어",
            answer="수정된 응답",
            confidence=0.6,
            previous_query="원래 질문",
        )

        # 축 1 + 축 2 = LLM 호출 2회
        assert llm.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_hook_disabled(self):
        llm = _make_mock_llm()
        store = _make_mock_event_store()

        config = MagicMock(spec=AriaConfig)
        config.learning = MagicMock(spec=LearningConfig)
        config.learning.enabled = False
        config.learning.conversation_learning = True
        config.learning.feedback_loop = True
        config.learning.tool_optimizer = True
        config.learning.prompt_improver = True
        config.learning.pattern_predictor = True
        config.learning.preference_confidence_threshold = 0.7
        config.learning.max_preferences_per_query = 3
        config.learning.correction_memory_domain = "correction-log"
        config.learning.tool_metrics_window_days = 7
        config.learning.min_calls_for_priority = 5
        config.learning.low_confidence_threshold = 0.5
        config.learning.min_pattern_occurrences = 3
        config.learning.pattern_confidence_threshold = 0.6

        lm = LearningManager(
            llm=llm, event_store=store, config=config,
        )

        await lm.post_query_hook(
            query="test",
            answer="response",
            confidence=0.5,
        )

        # 전체 비활성화 → 아무것도 실행 안 함
        llm.complete.assert_not_called()
        store.ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_hook_graceful_on_error(self):
        """학습 실패 → 예외 전파 없음"""
        llm = _make_mock_llm()
        llm.complete.side_effect = RuntimeError("LLM down")
        store = _make_mock_event_store()
        store.ingest.side_effect = RuntimeError("Store down")

        lm = LearningManager(llm=llm, event_store=store)

        # 예외 없이 완료 (graceful)
        await lm.post_query_hook(
            query="테스트 쿼리입니다",
            answer="테스트 응답",
            confidence=0.5,
        )
        # 여기까지 도달하면 성공

    @pytest.mark.asyncio
    async def test_hook_tool_recording(self):
        """도구 결과가 있으면 축 3 기록"""
        store = _make_mock_event_store()
        lm = LearningManager(
            llm=_make_mock_llm(), event_store=store,
        )

        await lm.post_query_hook(
            query="약국 찾기",
            answer="결과",
            confidence=0.8,
            tool_results=[
                {"name": "kakao_search", "success": True, "latency_ms": 200},
                {"name": "naver_search", "success": False, "latency_ms": 5000, "error": "timeout"},
            ],
        )

        # 도구 2개 + 쿼리 기록 1개 = 최소 3개 이벤트
        assert store.ingest.call_count >= 3

    @pytest.mark.asyncio
    async def test_hook_low_confidence_recording(self):
        """저신뢰 응답이면 축 4 기록"""
        store = _make_mock_event_store()
        lm = LearningManager(
            llm=_make_mock_llm(), event_store=store,
        )

        await lm.post_query_hook(
            query="상속세율 알려줘",
            answer="잘 모르겠습니다",
            confidence=0.3,  # 저신뢰
            intent_action="respond",
        )

        # 축 4 record_low_confidence + 축 5 record_query = 최소 2개
        assert store.ingest.call_count >= 2
