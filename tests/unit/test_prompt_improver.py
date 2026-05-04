"""ARIA Engine - PromptImprover Tests (Phase 5 축 4)

프롬프트 자기 개선 테스트
- record_low_confidence: 저신뢰 이벤트 기록
- 도메인별 그룹핑
- LLM 분석 → 지식 공백/프롬프트 개선 제안
- 메모리 축적
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.events.types import Event, EventQuery, EventSeverity
from aria.learning.prompt_improver import (
    PROMPT_ANALYSIS_SYSTEM,
    PROMPT_INSIGHTS_DOMAIN,
    PromptImprover,
)
from aria.learning.types import (
    LearningAnalysisRequest,
    LearningAnalysisResult,
    LearningEventType,
)


# === Test Helpers ===


def _make_mock_llm(response_content: str = "{}"):
    llm = MagicMock()
    llm.config = MagicMock()
    llm.config.cheap_model = "claude-haiku-4-5-20251001"
    llm.complete = AsyncMock(return_value={"content": response_content})
    return llm


def _make_low_conf_event(query: str, confidence: float, domain: str = "unknown") -> Event:
    return Event(
        event_id=f"evt-lc-{hash(query) % 10000}",
        event_type=LearningEventType.LOW_CONFIDENCE_QUERY.value,
        source="aria",
        severity=EventSeverity.INFO,
        data={"query": query, "confidence": confidence, "domain": domain},
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _make_mock_event_store(low_conf_events=None):
    store = MagicMock()
    mock_event = MagicMock()
    mock_event.event_id = "evt-pi-001"
    store.ingest = MagicMock(return_value=mock_event)

    def mock_query(q: EventQuery):
        if q.event_type == LearningEventType.LOW_CONFIDENCE_QUERY.value:
            return low_conf_events or []
        return []

    store.query = MagicMock(side_effect=mock_query)
    return store


def _make_mock_index_manager():
    manager = MagicMock()
    manager.get_topic.side_effect = Exception("not found")
    manager.upsert_topic = MagicMock()
    return manager


# ============================================================
# Init Tests
# ============================================================


class TestPromptImproverInit:

    def test_default_init(self):
        pi = PromptImprover(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        assert pi.enabled is True
        assert pi._low_confidence_threshold == 0.5
        assert pi._analysis_window_days == 7

    def test_custom_config(self):
        pi = PromptImprover(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
            low_confidence_threshold=0.4,
            analysis_window_days=14,
        )
        assert pi._low_confidence_threshold == 0.4
        assert pi._analysis_window_days == 14

    def test_stats(self):
        pi = PromptImprover(
            llm=_make_mock_llm(),
            event_store=_make_mock_event_store(),
        )
        stats = pi.stats
        assert stats["learner"] == "PromptImprover"
        assert stats["low_confidence_threshold"] == 0.5
        assert stats["total_low_confidence_recorded"] == 0


# ============================================================
# Record Low Confidence Tests
# ============================================================


class TestRecordLowConfidence:

    def test_record_below_threshold(self):
        store = _make_mock_event_store()
        pi = PromptImprover(
            llm=_make_mock_llm(), event_store=store,
            low_confidence_threshold=0.5,
        )

        event_id = pi.record_low_confidence(
            query="한국 상속세율 알려줘",
            confidence=0.35,
            domain="legal",
            tool_calls=0,
        )

        assert event_id == "evt-pi-001"
        assert pi._total_low_confidence_recorded == 1

        call_args = store.ingest.call_args[0][0]
        assert call_args.event_type == "low_confidence_query"
        assert call_args.data["domain"] == "legal"
        assert call_args.data["confidence"] == 0.35

    def test_skip_above_threshold(self):
        store = _make_mock_event_store()
        pi = PromptImprover(
            llm=_make_mock_llm(), event_store=store,
            low_confidence_threshold=0.5,
        )

        event_id = pi.record_low_confidence(
            query="안녕",
            confidence=0.8,
        )

        assert event_id is None
        store.ingest.assert_not_called()

    def test_skip_disabled(self):
        store = _make_mock_event_store()
        pi = PromptImprover(
            llm=_make_mock_llm(), event_store=store, enabled=False,
        )

        event_id = pi.record_low_confidence("test", confidence=0.2)

        assert event_id is None
        store.ingest.assert_not_called()

    def test_record_truncates_long_query(self):
        store = _make_mock_event_store()
        pi = PromptImprover(llm=_make_mock_llm(), event_store=store)

        pi.record_low_confidence(
            query="x" * 5000,
            confidence=0.3,
        )

        call_args = store.ingest.call_args[0][0]
        assert len(call_args.data["query"]) <= 1000


# ============================================================
# Group By Domain Tests
# ============================================================


class TestGroupByDomain:

    def test_group_with_domains(self):
        events = [
            _make_low_conf_event("법률 질문 1", 0.3, "legal"),
            _make_low_conf_event("법률 질문 2", 0.4, "legal"),
            _make_low_conf_event("의학 질문 1", 0.2, "medical"),
        ]
        pi = PromptImprover(
            llm=_make_mock_llm(), event_store=_make_mock_event_store()
        )

        grouped = pi._group_by_domain(events)

        assert "legal" in grouped
        assert len(grouped["legal"]) == 2
        assert "medical" in grouped
        assert len(grouped["medical"]) == 1

    def test_group_empty_domain_as_unknown(self):
        events = [
            _make_low_conf_event("질문 1", 0.3, ""),
            _make_low_conf_event("질문 2", 0.4, ""),
        ]
        pi = PromptImprover(
            llm=_make_mock_llm(), event_store=_make_mock_event_store()
        )

        grouped = pi._group_by_domain(events)
        assert "unknown" in grouped
        assert len(grouped["unknown"]) == 2


# ============================================================
# Analyze Tests
# ============================================================


class TestPromptImproverAnalyze:

    @pytest.mark.asyncio
    async def test_analyze_with_insights(self):
        events = [
            _make_low_conf_event("상속세 질문 1", 0.3, "legal"),
            _make_low_conf_event("상속세 질문 2", 0.35, "legal"),
            _make_low_conf_event("상속세 질문 3", 0.28, "legal"),
        ]
        store = _make_mock_event_store(events)

        llm_response = json.dumps({
            "domain_insights": [
                {
                    "domain": "legal",
                    "knowledge_gaps": ["한국 상속세법 최신 세율표"],
                    "improvement_suggestions": ["법률 관련 질문 시 공식 법령 참조 지침 추가"],
                    "priority": "high",
                }
            ],
            "overall_assessment": "법률 도메인 지식 보강 필요",
        })
        llm = _make_mock_llm(llm_response)
        manager = _make_mock_index_manager()

        pi = PromptImprover(
            llm=llm, event_store=store, index_manager=manager,
        )

        req = LearningAnalysisRequest(query="analysis")
        result = await pi.analyze(req)

        # knowledge_gap + prompt_improvement = 2 이벤트
        assert result.events_stored >= 1
        assert PROMPT_INSIGHTS_DOMAIN in result.memory_topics_updated

    @pytest.mark.asyncio
    async def test_analyze_no_events(self):
        store = _make_mock_event_store()
        pi = PromptImprover(llm=_make_mock_llm(), event_store=store)

        req = LearningAnalysisRequest(query="analysis")
        result = await pi.analyze(req)

        assert result == LearningAnalysisResult()

    @pytest.mark.asyncio
    async def test_analyze_too_few_queries_per_domain(self):
        """도메인당 3개 미만이면 분석 스킵"""
        events = [
            _make_low_conf_event("질문 1", 0.3, "legal"),
            _make_low_conf_event("질문 2", 0.4, "medical"),
        ]
        store = _make_mock_event_store(events)
        llm = _make_mock_llm()

        pi = PromptImprover(llm=llm, event_store=store)

        req = LearningAnalysisRequest(query="analysis")
        result = await pi.analyze(req)

        assert result == LearningAnalysisResult()
        llm.complete.assert_not_called()  # LLM 호출 없음

    @pytest.mark.asyncio
    async def test_analyze_multiple_domains(self):
        events = [
            _make_low_conf_event("법률 1", 0.3, "legal"),
            _make_low_conf_event("법률 2", 0.35, "legal"),
            _make_low_conf_event("법률 3", 0.28, "legal"),
            _make_low_conf_event("의학 1", 0.2, "medical"),
            _make_low_conf_event("의학 2", 0.25, "medical"),
            _make_low_conf_event("의학 3", 0.22, "medical"),
        ]
        store = _make_mock_event_store(events)

        llm_response = json.dumps({
            "domain_insights": [
                {"domain": "legal", "knowledge_gaps": ["세법"], "improvement_suggestions": ["지침"], "priority": "high"},
                {"domain": "medical", "knowledge_gaps": ["약학"], "improvement_suggestions": ["참조"], "priority": "medium"},
            ],
            "overall_assessment": "법률+의학 보강 필요",
        })
        llm = _make_mock_llm(llm_response)
        manager = _make_mock_index_manager()

        pi = PromptImprover(llm=llm, event_store=store, index_manager=manager)

        req = LearningAnalysisRequest(query="analysis")
        result = await pi.analyze(req)

        # 2 domains × 2 events (gap + suggestion) = 4
        assert result.events_stored >= 2


# ============================================================
# Build Analysis Prompt Tests
# ============================================================


class TestBuildAnalysisPrompt:

    def test_prompt_format(self):
        pi = PromptImprover(
            llm=_make_mock_llm(), event_store=_make_mock_event_store()
        )

        domains = {
            "legal": [
                {"query": "상속세 질문", "confidence": 0.3},
                {"query": "증여세 질문", "confidence": 0.4},
                {"query": "재산세 질문", "confidence": 0.35},
            ]
        }

        prompt = pi._build_analysis_prompt(domains)
        assert "legal" in prompt
        assert "3건" in prompt
        assert "상속세" in prompt


# ============================================================
# System Prompt Tests
# ============================================================


class TestPromptAnalysisPrompt:

    def test_prompt_has_output_format(self):
        assert "domain_insights" in PROMPT_ANALYSIS_SYSTEM
        assert "knowledge_gaps" in PROMPT_ANALYSIS_SYSTEM
        assert "improvement_suggestions" in PROMPT_ANALYSIS_SYSTEM

    def test_prompt_has_min_threshold(self):
        assert "3개 미만" in PROMPT_ANALYSIS_SYSTEM


# ============================================================
# Graceful Degradation Tests
# ============================================================


class TestPromptImproverGraceful:

    @pytest.mark.asyncio
    async def test_safe_analyze_on_failure(self):
        events = [_make_low_conf_event("q", 0.3, "d")] * 5
        store = _make_mock_event_store(events)
        llm = _make_mock_llm()
        llm.complete.side_effect = RuntimeError("API down")

        pi = PromptImprover(llm=llm, event_store=store)

        req = LearningAnalysisRequest(query="analysis")
        result = await pi.safe_analyze(req)

        assert result == LearningAnalysisResult()
        assert pi.stats["total_errors"] == 1
