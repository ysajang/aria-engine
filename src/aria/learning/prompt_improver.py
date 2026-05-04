"""ARIA Engine - Prompt Improver (Phase 5 축 4)

confidence 낮은 쿼리 패턴을 분석하여 지식 보강/프롬프트 개선 제안

동작 플로우:
    1. /v1/query 응답 후 confidence < threshold → record_low_confidence() 호출
    2. EventStore에 low_confidence_query 이벤트 저장 (LLM 없음 / 비용 $0)
    3. analyze() — 주기적 분석 (매일 1회 또는 수동 트리거)
       → 저신뢰 쿼리를 도메인별 그룹핑
       → cheap 모델로 지식 공백/프롬프트 개선점 분석
       → DomainInsight 생성 → 이벤트 + 메모리 저장

비용: record_low_confidence $0 / analyze() ~$0.0003 (Haiku)
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
    DomainInsight,
    LearningAnalysisRequest,
    LearningAnalysisResult,
    LearningEventType,
)
from aria.memory.index_manager import IndexManager
from aria.providers.llm_provider import LLMProvider

logger = structlog.get_logger()

# 메모리 토픽 도메인
PROMPT_INSIGHTS_DOMAIN = "prompt-insights"

# 프롬프트 개선 분석 시스템 프롬프트
PROMPT_ANALYSIS_SYSTEM = """당신은 AI 시스템 프롬프트 개선 전문가입니다.

AI 어시스턴트가 낮은 신뢰도(confidence)로 응답한 쿼리들을 도메인별로 분석하여
지식 공백과 프롬프트 개선점을 식별하세요.

JSON 형태로만 응답하세요:
{
    "domain_insights": [
        {
            "domain": "도메인명 (예: legal / medical / finance)",
            "knowledge_gaps": ["식별된 지식 공백 1", "지식 공백 2"],
            "improvement_suggestions": ["프롬프트 개선 제안 1", "제안 2"],
            "priority": "high" | "medium" | "low"
        }
    ],
    "overall_assessment": "전체 평가 요약 (1~2문장)"
}

규칙:
1. 쿼리가 3개 미만인 도메인은 무시 (통계적 의미 부족)
2. knowledge_gaps: 벡터DB에 추가하면 개선될 지식 영역
3. improvement_suggestions: 시스템 프롬프트에 추가할 지침
4. 구체적이고 실행 가능한 제안만"""


class PromptImprover(BaseLearner):
    """프롬프트 자기 개선 — 저신뢰 도메인 분석 + 개선 제안

    Args:
        llm: LLM 프로바이더 (analyze 시에만 사용)
        event_store: 이벤트 저장소
        index_manager: 메모리 인덱스 매니저
        enabled: 활성화 여부
        low_confidence_threshold: 저신뢰 기준 confidence
        analysis_window_days: 분석 기간 (일)
    """

    def __init__(
        self,
        llm: LLMProvider,
        event_store: EventStore,
        index_manager: IndexManager | None = None,
        enabled: bool = True,
        low_confidence_threshold: float = 0.5,
        analysis_window_days: int = 7,
    ) -> None:
        super().__init__(
            llm=llm,
            event_store=event_store,
            index_manager=index_manager,
            enabled=enabled,
        )
        self._low_confidence_threshold = low_confidence_threshold
        self._analysis_window_days = analysis_window_days
        self._total_low_confidence_recorded: int = 0

    @property
    def stats(self) -> dict[str, Any]:
        base = super().stats
        base.update({
            "low_confidence_threshold": self._low_confidence_threshold,
            "analysis_window_days": self._analysis_window_days,
            "total_low_confidence_recorded": self._total_low_confidence_recorded,
        })
        return base

    # === 이벤트 기록 (LLM 호출 없음) ===

    def record_low_confidence(
        self,
        query: str,
        confidence: float,
        domain: str = "",
        tool_calls: int = 0,
    ) -> str | None:
        """저신뢰 응답을 EventStore에 기록

        /v1/query 응답 후 confidence < threshold일 때 호출
        LLM 호출 없이 이벤트 저장만 → 비용 $0

        Args:
            query: 사용자 쿼리
            confidence: 응답 신뢰도
            domain: 감지된 도메인 (의도분석에서 추출 가능)
            tool_calls: 사용된 도구 수

        Returns:
            event_id 또는 None
        """
        if not self._enabled:
            return None

        if confidence >= self._low_confidence_threshold:
            return None  # 임계값 이상이면 기록 안 함

        event_id = self._store_learning_event(
            LearningEventType.LOW_CONFIDENCE_QUERY,
            {
                "query": query[:1000],
                "confidence": round(confidence, 4),
                "domain": domain,
                "tool_calls": tool_calls,
            },
        )

        if event_id:
            self._total_low_confidence_recorded += 1

        return event_id

    # === LLM 기반 분석 (주기적) ===

    async def analyze(
        self,
        request: LearningAnalysisRequest,
    ) -> LearningAnalysisResult:
        """저신뢰 쿼리 패턴 분석 → 지식 공백/프롬프트 개선 제안

        주기적으로 호출 (매일 1회 또는 수동)
        EventStore에서 low_confidence_query 이벤트 집계 → 도메인별 분석

        Args:
            request: 분석 요청 (query 필드는 무시 — 이벤트 기반 분석)

        Returns:
            분석 결과
        """
        # 1. 저신뢰 이벤트 조회
        since = (
            datetime.now(timezone.utc) - timedelta(days=self._analysis_window_days)
        ).isoformat()

        events = self._event_store.query(EventQuery(
            source="aria",
            event_type=LearningEventType.LOW_CONFIDENCE_QUERY.value,
            since=since,
            limit=500,
        ))

        if not events:
            return LearningAnalysisResult()

        # 2. 도메인별 그룹핑
        domain_queries = self._group_by_domain(events)

        # 3개 미만 쿼리인 도메인 제외
        significant_domains = {
            d: qs for d, qs in domain_queries.items() if len(qs) >= 3
        }

        if not significant_domains:
            return LearningAnalysisResult()

        # 3. cheap 모델로 분석
        user_prompt = self._build_analysis_prompt(significant_domains)
        raw_response = await self._call_cheap_llm(
            PROMPT_ANALYSIS_SYSTEM,
            user_prompt,
        )

        parsed = self._parse_json_response(raw_response)
        domain_insights_raw = parsed.get("domain_insights", [])
        overall = parsed.get("overall_assessment", "")

        # 4. DomainInsight 생성 + 이벤트 저장
        events_stored = 0
        memory_topics_updated: list[str] = []

        for raw_insight in domain_insights_raw:
            if not isinstance(raw_insight, dict):
                continue

            domain = raw_insight.get("domain", "")
            if not domain:
                continue

            # knowledge_gap 이벤트
            gaps = raw_insight.get("knowledge_gaps", [])
            if gaps:
                event_id = self._store_learning_event(
                    LearningEventType.KNOWLEDGE_GAP_DETECTED,
                    {
                        "domain": domain,
                        "gaps": gaps[:5],
                        "query_count": len(significant_domains.get(domain, [])),
                    },
                )
                if event_id:
                    events_stored += 1

            # prompt_improvement 이벤트
            suggestions = raw_insight.get("improvement_suggestions", [])
            if suggestions:
                event_id = self._store_learning_event(
                    LearningEventType.PROMPT_IMPROVEMENT_SUGGESTED,
                    {
                        "domain": domain,
                        "suggestions": suggestions[:5],
                        "priority": raw_insight.get("priority", "medium"),
                    },
                )
                if event_id:
                    events_stored += 1

        # 5. 메모리에 인사이트 축적
        if domain_insights_raw or overall:
            updated = self._update_insights_memory(
                scope=request.scope,
                domains=significant_domains,
                overall=overall,
            )
            if updated:
                memory_topics_updated.append(PROMPT_INSIGHTS_DOMAIN)

        return LearningAnalysisResult(
            events_stored=events_stored,
            memory_topics_updated=memory_topics_updated,
        )

    def _group_by_domain(
        self,
        events: list,
    ) -> dict[str, list[dict[str, Any]]]:
        """이벤트를 도메인별로 그룹핑

        domain이 비어있는 이벤트는 "unknown" 도메인으로 분류
        """
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for event in events:
            domain = event.data.get("domain", "") or "unknown"
            grouped[domain].append({
                "query": event.data.get("query", ""),
                "confidence": event.data.get("confidence", 0),
            })

        return dict(grouped)

    def _build_analysis_prompt(
        self,
        domain_queries: dict[str, list[dict[str, Any]]],
    ) -> str:
        """분석 프롬프트 생성"""
        lines = [
            f"저신뢰 쿼리 분석 (최근 {self._analysis_window_days}일):",
            "",
        ]

        for domain, queries in domain_queries.items():
            avg_conf = sum(q["confidence"] for q in queries) / len(queries)
            lines.append(f"## 도메인: {domain} ({len(queries)}건 / 평균 confidence {avg_conf:.2f})")

            # 대표 쿼리 (최대 5개)
            for q in queries[:5]:
                lines.append(f"  - [{q['confidence']:.2f}] {q['query'][:200]}")
            lines.append("")

        return "\n".join(lines)

    def _update_insights_memory(
        self,
        scope: str,
        domains: dict[str, list[dict[str, Any]]],
        overall: str,
    ) -> bool:
        """프롬프트 개선 인사이트를 메모리에 저장"""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

        lines = [f"## 프롬프트 개선 인사이트 ({timestamp})\n"]
        if overall:
            lines.append(f"**평가**: {overall}\n")

        lines.append("**저신뢰 도메인 요약**:")
        for domain, queries in list(domains.items())[:10]:
            avg_conf = sum(q["confidence"] for q in queries) / len(queries)
            lines.append(
                f"- {domain}: {len(queries)}건 / "
                f"평균 confidence {avg_conf:.2f}"
            )

        return self._upsert_memory_topic(
            scope=scope,
            domain=PROMPT_INSIGHTS_DOMAIN,
            summary="프롬프트 개선 인사이트 — 저신뢰 도메인 분석 (자동 생성)",
            content="\n".join(lines),
        )
