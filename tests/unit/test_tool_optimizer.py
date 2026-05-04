"""ARIA Engine - ToolOptimizer Tests (Phase 5 축 3)

도구 사용 최적화 테스트
- record_execution: 이벤트 기록
- get_metrics: 단일 도구 메트릭 집계
- get_all_metrics: 전체 도구 메트릭 집계 + 우선순위 정렬
- get_priority_ranking: 우선순위 순위 반환
- analyze: LLM 기반 주기적 분석
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.events.types import Event, EventQuery, EventSeverity
from aria.learning.tool_optimizer import (
    TOOL_ANALYSIS_SYSTEM,
    TOOL_INSIGHTS_DOMAIN,
    ToolOptimizer,
)
from aria.learning.types import (
    LearningAnalysisRequest,
    LearningAnalysisResult,
    LearningEventType,
    ToolMetrics,
)


# === Test Helpers ===


def _make_mock_llm(response_content: str = "{}"):
    llm = MagicMock()
    llm.config = MagicMock()
    llm.config.cheap_model = "claude-haiku-4-5-20251001"
    llm.complete = AsyncMock(return_value={"content": response_content})
    return llm


def _make_mock_event_store(success_events=None, failure_events=None):
    """EventStore mock — query 호출 시 이벤트 반환"""
    store = MagicMock()

    # ingest mock
    mock_event = MagicMock()
    mock_event.event_id = "evt-tool-001"
    store.ingest = MagicMock(return_value=mock_event)

    # query mock — event_type으로 분기
    def mock_query(q: EventQuery):
        if q.event_type == LearningEventType.TOOL_SUCCESS.value:
            return success_events or []
        elif q.event_type == LearningEventType.TOOL_FAILURE.value:
            return failure_events or []
        return []

    store.query = MagicMock(side_effect=mock_query)
    return store


def _make_event(tool_name: str, success: bool, latency_ms: float = 100.0, error: str = "") -> Event:
    """테스트용 Event 생성"""
    data = {"tool_name": tool_name, "latency_ms": latency_ms}
    if error:
        data["error"] = error
    return Event(
        event_id=f"evt-{tool_name}-{success}",
        event_type=LearningEventType.TOOL_SUCCESS.value if success else LearningEventType.TOOL_FAILURE.value,
        source="aria",
        severity=EventSeverity.INFO if success else EventSeverity.WARNING,
        data=data,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _make_mock_index_manager():
    manager = MagicMock()
    manager.get_topic.side_effect = Exception("not found")
    manager.upsert_topic = MagicMock()
    return manager


# ============================================================
# Init Tests
# ============================================================


class TestToolOptimizerInit:

    def test_default_init(self):
        opt = ToolOptimizer(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        assert opt.enabled is True
        assert opt._metrics_window_days == 7
        assert opt._min_calls_for_priority == 5

    def test_custom_config(self):
        opt = ToolOptimizer(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            metrics_window_days=30,
            min_calls_for_priority=10,
        )
        assert opt._metrics_window_days == 30
        assert opt._min_calls_for_priority == 10

    def test_stats(self):
        opt = ToolOptimizer(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        stats = opt.stats
        assert stats["learner"] == "ToolOptimizer"
        assert stats["metrics_window_days"] == 7
        assert stats["total_recorded"] == 0


# ============================================================
# Record Execution Tests
# ============================================================


class TestRecordExecution:

    def test_record_success(self):
        store = _make_mock_event_store()
        opt = ToolOptimizer(llm=_make_mock_llm(), event_store=store)

        event_id = opt.record_execution("kakao_search", success=True, latency_ms=250.5)

        assert event_id == "evt-tool-001"
        assert opt._total_recorded == 1
        store.ingest.assert_called_once()

        call_args = store.ingest.call_args[0][0]
        assert call_args.event_type == "tool_success"
        assert call_args.data["tool_name"] == "kakao_search"
        assert call_args.data["latency_ms"] == 250.5

    def test_record_failure(self):
        store = _make_mock_event_store()
        opt = ToolOptimizer(llm=_make_mock_llm(), event_store=store)

        event_id = opt.record_execution(
            "naver_search", success=False, latency_ms=5000, error="timeout"
        )

        assert event_id == "evt-tool-001"
        call_args = store.ingest.call_args[0][0]
        assert call_args.event_type == "tool_failure"
        assert call_args.data["error"] == "timeout"
        assert call_args.severity == EventSeverity.WARNING

    def test_record_disabled(self):
        store = _make_mock_event_store()
        opt = ToolOptimizer(
            llm=_make_mock_llm(), event_store=store, enabled=False
        )

        event_id = opt.record_execution("test", success=True)

        assert event_id is None
        store.ingest.assert_not_called()


# ============================================================
# Get Metrics Tests
# ============================================================


class TestGetMetrics:

    def test_metrics_with_data(self):
        success_events = [
            _make_event("kakao_search", True, 200),
            _make_event("kakao_search", True, 300),
            _make_event("kakao_search", True, 150),
            _make_event("other_tool", True, 500),
        ]
        failure_events = [
            _make_event("kakao_search", False, 5000, "timeout"),
        ]
        store = _make_mock_event_store(success_events, failure_events)
        opt = ToolOptimizer(
            llm=_make_mock_llm(), event_store=store, min_calls_for_priority=3
        )

        metrics = opt.get_metrics("kakao_search")

        assert metrics.tool_name == "kakao_search"
        assert metrics.success_count == 3
        assert metrics.failure_count == 1
        assert metrics.total_calls == 4
        assert metrics.success_rate == 0.75
        assert metrics.avg_latency_ms > 0
        assert "timeout" in metrics.error_patterns
        # 4 >= min_calls(3) → 우선순위 계산됨
        assert metrics.priority_score != 0.5  # 기본값이 아닌 계산된 값

    def test_metrics_empty(self):
        store = _make_mock_event_store()
        opt = ToolOptimizer(llm=_make_mock_llm(), event_store=store)

        metrics = opt.get_metrics("nonexistent_tool")

        assert metrics.success_count == 0
        assert metrics.failure_count == 0
        assert metrics.priority_score == 0.5  # 미사용 = 중립

    def test_metrics_below_min_calls(self):
        """최소 호출 수 미달 → 우선순위 계산 안 함 (기본값 유지)"""
        success_events = [_make_event("test_tool", True, 100)]
        store = _make_mock_event_store(success_events)
        opt = ToolOptimizer(
            llm=_make_mock_llm(), event_store=store, min_calls_for_priority=5
        )

        metrics = opt.get_metrics("test_tool")
        assert metrics.success_count == 1
        assert metrics.priority_score == 0.5  # 기본값


# ============================================================
# Get All Metrics Tests
# ============================================================


class TestGetAllMetrics:

    def test_all_metrics_sorted(self):
        success_events = [
            _make_event("good_tool", True, 100),
            _make_event("good_tool", True, 150),
            _make_event("good_tool", True, 120),
            _make_event("good_tool", True, 130),
            _make_event("good_tool", True, 110),
            _make_event("bad_tool", True, 200),
        ]
        failure_events = [
            _make_event("bad_tool", False, 5000, "err1"),
            _make_event("bad_tool", False, 4000, "err2"),
            _make_event("bad_tool", False, 6000, "err3"),
            _make_event("bad_tool", False, 3000, "err4"),
        ]
        store = _make_mock_event_store(success_events, failure_events)
        opt = ToolOptimizer(
            llm=_make_mock_llm(), event_store=store, min_calls_for_priority=3
        )

        all_metrics = opt.get_all_metrics()

        assert len(all_metrics) == 2
        # good_tool이 더 높은 우선순위
        assert all_metrics[0].tool_name == "good_tool"
        assert all_metrics[0].priority_score > all_metrics[1].priority_score

    def test_all_metrics_empty(self):
        store = _make_mock_event_store()
        opt = ToolOptimizer(llm=_make_mock_llm(), event_store=store)

        all_metrics = opt.get_all_metrics()
        assert all_metrics == []


# ============================================================
# Priority Ranking Tests
# ============================================================


class TestPriorityRanking:

    def test_ranking_format(self):
        success_events = [
            _make_event("tool_a", True, 100),
            _make_event("tool_a", True, 100),
            _make_event("tool_a", True, 100),
            _make_event("tool_a", True, 100),
            _make_event("tool_a", True, 100),
        ]
        store = _make_mock_event_store(success_events)
        opt = ToolOptimizer(
            llm=_make_mock_llm(), event_store=store, min_calls_for_priority=3
        )

        ranking = opt.get_priority_ranking()
        assert len(ranking) == 1
        assert ranking[0][0] == "tool_a"
        assert isinstance(ranking[0][1], float)


# ============================================================
# Analyze Tests
# ============================================================


class TestToolOptimizerAnalyze:

    @pytest.mark.asyncio
    async def test_analyze_with_insights(self):
        success_events = [
            _make_event("fast_tool", True, 100),
            _make_event("fast_tool", True, 150),
            _make_event("fast_tool", True, 120),
            _make_event("fast_tool", True, 130),
            _make_event("fast_tool", True, 110),
        ]
        failure_events = [
            _make_event("slow_tool", False, 5000, "timeout"),
            _make_event("slow_tool", False, 4000, "timeout"),
            _make_event("slow_tool", False, 6000, "timeout"),
            _make_event("slow_tool", False, 3000, "timeout"),
            _make_event("slow_tool", False, 7000, "timeout"),
        ]
        store = _make_mock_event_store(success_events, failure_events)

        llm_response = json.dumps({
            "insights": [
                {
                    "tool_name": "slow_tool",
                    "issue": "타임아웃 빈발",
                    "suggestion": "타임아웃 임계값 늘리거나 대안 도구 사용",
                    "priority": "high",
                }
            ],
            "overall_recommendation": "slow_tool 대신 fast_tool 우선 사용 권장",
        })
        llm = _make_mock_llm(llm_response)
        manager = _make_mock_index_manager()

        opt = ToolOptimizer(
            llm=llm, event_store=store, index_manager=manager,
            min_calls_for_priority=3,
        )

        req = LearningAnalysisRequest(query="tool analysis")
        result = await opt.analyze(req)

        assert result.events_stored >= 1
        assert TOOL_INSIGHTS_DOMAIN in result.memory_topics_updated

    @pytest.mark.asyncio
    async def test_analyze_no_data(self):
        """이벤트 없으면 빈 결과"""
        store = _make_mock_event_store()
        opt = ToolOptimizer(llm=_make_mock_llm(), event_store=store)

        req = LearningAnalysisRequest(query="tool analysis")
        result = await opt.analyze(req)

        assert result == LearningAnalysisResult()

    @pytest.mark.asyncio
    async def test_analyze_below_min_calls(self):
        """최소 호출 수 미달 → 분석 스킵"""
        success_events = [_make_event("tool_a", True, 100)]
        store = _make_mock_event_store(success_events)
        llm = _make_mock_llm()
        opt = ToolOptimizer(
            llm=llm, event_store=store, min_calls_for_priority=10
        )

        req = LearningAnalysisRequest(query="tool analysis")
        result = await opt.analyze(req)

        assert result == LearningAnalysisResult()
        llm.complete.assert_not_called()  # LLM 호출 없음


# ============================================================
# Build Metrics Prompt Tests
# ============================================================


class TestBuildMetricsPrompt:

    def test_prompt_format(self):
        opt = ToolOptimizer(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )

        metrics = [
            ToolMetrics(
                tool_name="kakao_search",
                success_count=8,
                failure_count=2,
                avg_latency_ms=250,
                priority_score=0.75,
                error_patterns=["timeout"],
            ),
        ]

        prompt = opt._build_metrics_prompt(metrics)
        assert "kakao_search" in prompt
        assert "80%" in prompt
        assert "250ms" in prompt
        assert "timeout" in prompt


# ============================================================
# System Prompt Tests
# ============================================================


class TestToolAnalysisPrompt:

    def test_prompt_has_threshold(self):
        assert "80%" in TOOL_ANALYSIS_SYSTEM

    def test_prompt_has_response_format(self):
        assert "insights" in TOOL_ANALYSIS_SYSTEM
        assert "overall_recommendation" in TOOL_ANALYSIS_SYSTEM


# ============================================================
# Graceful Degradation Tests
# ============================================================


class TestToolOptimizerGraceful:

    @pytest.mark.asyncio
    async def test_safe_analyze_on_failure(self):
        success_events = [_make_event("t", True, 100)] * 5
        store = _make_mock_event_store(success_events)
        llm = _make_mock_llm()
        llm.complete.side_effect = RuntimeError("API down")

        opt = ToolOptimizer(
            llm=llm, event_store=store, min_calls_for_priority=3
        )

        req = LearningAnalysisRequest(query="analysis")
        result = await opt.safe_analyze(req)

        assert result == LearningAnalysisResult()
        assert opt.stats["total_errors"] == 1
