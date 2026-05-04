"""ARIA Engine - Tool Optimizer (Phase 5 축 3)

도구별 성공/실패율 축적 → 자동 우선순위 조정

동작 플로우:
    1. ToolRegistry.execute() 완료 후 record_execution() 호출
    2. tool_success / tool_failure 이벤트를 EventStore에 저장
    3. get_metrics() / get_all_metrics()로 집계 통계 조회
    4. analyze() — 주기적 분석 (고장률 높은 도구 감지 + 개선 제안)
    5. get_priority_ranking() — 우선순위 정렬된 도구 목록 반환

비용: record_execution은 LLM 호출 없음 (이벤트 저장만)
      analyze()만 cheap 모델 1회 ($0.0003)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

import structlog

from aria.events.event_store import EventStore
from aria.events.types import EventInput, EventQuery, EventSeverity
from aria.learning.base import BaseLearner
from aria.learning.types import (
    LearningAnalysisRequest,
    LearningAnalysisResult,
    LearningEventType,
    ToolMetrics,
)
from aria.memory.index_manager import IndexManager
from aria.providers.llm_provider import LLMProvider

logger = structlog.get_logger()

# 메모리 토픽 도메인 (도구 최적화 인사이트)
TOOL_INSIGHTS_DOMAIN = "tool-optimization"

# 도구 분석 시스템 프롬프트
TOOL_ANALYSIS_SYSTEM = """당신은 도구 사용 최적화 전문가입니다.

도구별 성능 통계를 분석하여 개선점을 제안하세요.

입력: 도구별 성능 메트릭 (성공률/실패률/평균 응답시간/에러 패턴)

JSON 형태로만 응답하세요:
{
    "insights": [
        {
            "tool_name": "도구명",
            "issue": "식별된 문제",
            "suggestion": "구체적 개선 제안",
            "priority": "high" | "medium" | "low"
        }
    ],
    "overall_recommendation": "전체 도구 사용 전략 요약 (1~2문장)"
}

규칙:
1. 성공률 80% 미만인 도구만 insights에 포함
2. 응답시간 5초 이상인 도구에 대해 대안 제안
3. 사용 빈도 0인 도구는 무시
4. 구체적이고 실행 가능한 제안만 제공"""


class ToolOptimizer(BaseLearner):
    """도구 사용 최적화 — 성공/실패율 축적 + 우선순위 자동 조정

    Args:
        llm: LLM 프로바이더 (analyze 시에만 사용)
        event_store: 이벤트 저장소
        index_manager: 메모리 인덱스 매니저
        enabled: 활성화 여부
        metrics_window_days: 통계 집계 기간 (일)
        min_calls_for_priority: 우선순위 조정에 필요한 최소 호출 수
    """

    def __init__(
        self,
        llm: LLMProvider,
        event_store: EventStore,
        index_manager: IndexManager | None = None,
        enabled: bool = True,
        metrics_window_days: int = 7,
        min_calls_for_priority: int = 5,
    ) -> None:
        super().__init__(
            llm=llm,
            event_store=event_store,
            index_manager=index_manager,
            enabled=enabled,
        )
        self._metrics_window_days = metrics_window_days
        self._min_calls_for_priority = min_calls_for_priority
        self._total_recorded: int = 0

    @property
    def stats(self) -> dict[str, Any]:
        base = super().stats
        base.update({
            "metrics_window_days": self._metrics_window_days,
            "min_calls_for_priority": self._min_calls_for_priority,
            "total_recorded": self._total_recorded,
        })
        return base

    # === 이벤트 기록 (LLM 호출 없음) ===

    def record_execution(
        self,
        tool_name: str,
        success: bool,
        latency_ms: float = 0.0,
        error: str = "",
    ) -> str | None:
        """도구 실행 결과를 EventStore에 기록

        ToolRegistry.execute() 완료 후 호출
        LLM 호출 없이 이벤트 저장만 수행 → 비용 0

        Args:
            tool_name: 도구 이름
            success: 성공 여부
            latency_ms: 응답 시간 (밀리초)
            error: 에러 메시지 (실패 시)

        Returns:
            event_id 또는 None
        """
        if not self._enabled:
            return None

        event_type = (
            LearningEventType.TOOL_SUCCESS if success
            else LearningEventType.TOOL_FAILURE
        )

        data: dict[str, Any] = {
            "tool_name": tool_name,
            "latency_ms": round(latency_ms, 1),
        }
        if error:
            data["error"] = error[:500]

        event_id = self._store_learning_event(
            event_type,
            data,
            severity=EventSeverity.INFO if success else EventSeverity.WARNING,
        )

        if event_id:
            self._total_recorded += 1

        return event_id

    # === 메트릭 집계 ===

    def get_metrics(self, tool_name: str) -> ToolMetrics:
        """특정 도구의 성능 메트릭 집계

        EventStore에서 최근 N일간 이벤트를 조회하여 집계

        Args:
            tool_name: 도구 이름

        Returns:
            ToolMetrics (성공/실패 횟수 + 평균 응답시간 + 우선순위 점수)
        """
        since = (
            datetime.now(timezone.utc) - timedelta(days=self._metrics_window_days)
        ).isoformat()

        # 성공 이벤트 조회
        success_events = self._event_store.query(EventQuery(
            source="aria",
            event_type=LearningEventType.TOOL_SUCCESS.value,
            since=since,
            limit=500,
        ))

        # 실패 이벤트 조회
        failure_events = self._event_store.query(EventQuery(
            source="aria",
            event_type=LearningEventType.TOOL_FAILURE.value,
            since=since,
            limit=500,
        ))

        # 해당 도구 필터링
        success_count = 0
        failure_count = 0
        latencies: list[float] = []
        error_patterns: list[str] = []
        last_used_at: datetime | None = None

        for event in success_events:
            if event.data.get("tool_name") == tool_name:
                success_count += 1
                lat = event.data.get("latency_ms", 0)
                if lat:
                    latencies.append(float(lat))
                ts = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
                if last_used_at is None or ts > last_used_at:
                    last_used_at = ts

        for event in failure_events:
            if event.data.get("tool_name") == tool_name:
                failure_count += 1
                lat = event.data.get("latency_ms", 0)
                if lat:
                    latencies.append(float(lat))
                err = event.data.get("error", "")
                if err and err not in error_patterns:
                    error_patterns.append(err[:200])
                ts = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
                if last_used_at is None or ts > last_used_at:
                    last_used_at = ts

        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

        metrics = ToolMetrics(
            tool_name=tool_name,
            success_count=success_count,
            failure_count=failure_count,
            avg_latency_ms=round(avg_latency, 1),
            last_used_at=last_used_at,
            error_patterns=error_patterns[:5],
        )

        # 최소 호출 수 이상이면 우선순위 계산
        if metrics.total_calls >= self._min_calls_for_priority:
            metrics.calculate_priority()

        return metrics

    def get_all_metrics(self) -> list[ToolMetrics]:
        """모든 도구의 메트릭 집계 (우선순위 내림차순 정렬)

        Returns:
            ToolMetrics 목록 (priority_score 내림차순)
        """
        since = (
            datetime.now(timezone.utc) - timedelta(days=self._metrics_window_days)
        ).isoformat()

        # 모든 도구 이벤트 조회
        success_events = self._event_store.query(EventQuery(
            source="aria",
            event_type=LearningEventType.TOOL_SUCCESS.value,
            since=since,
            limit=500,
        ))
        failure_events = self._event_store.query(EventQuery(
            source="aria",
            event_type=LearningEventType.TOOL_FAILURE.value,
            since=since,
            limit=500,
        ))

        # 도구별 집계
        tool_data: dict[str, dict] = defaultdict(lambda: {
            "success": 0, "failure": 0, "latencies": [],
            "errors": [], "last_used": None,
        })

        for event in success_events:
            name = event.data.get("tool_name", "")
            if not name:
                continue
            d = tool_data[name]
            d["success"] += 1
            lat = event.data.get("latency_ms", 0)
            if lat:
                d["latencies"].append(float(lat))
            ts = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
            if d["last_used"] is None or ts > d["last_used"]:
                d["last_used"] = ts

        for event in failure_events:
            name = event.data.get("tool_name", "")
            if not name:
                continue
            d = tool_data[name]
            d["failure"] += 1
            lat = event.data.get("latency_ms", 0)
            if lat:
                d["latencies"].append(float(lat))
            err = event.data.get("error", "")
            if err and err not in d["errors"]:
                d["errors"].append(err[:200])
            ts = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
            if d["last_used"] is None or ts > d["last_used"]:
                d["last_used"] = ts

        # ToolMetrics 변환
        metrics_list: list[ToolMetrics] = []
        for name, d in tool_data.items():
            lats = d["latencies"]
            m = ToolMetrics(
                tool_name=name,
                success_count=d["success"],
                failure_count=d["failure"],
                avg_latency_ms=round(sum(lats) / len(lats), 1) if lats else 0.0,
                last_used_at=d["last_used"],
                error_patterns=d["errors"][:5],
            )
            if m.total_calls >= self._min_calls_for_priority:
                m.calculate_priority()
            metrics_list.append(m)

        # 우선순위 내림차순 정렬
        metrics_list.sort(key=lambda m: m.priority_score, reverse=True)
        return metrics_list

    def get_priority_ranking(self) -> list[tuple[str, float]]:
        """우선순위 정렬된 도구 목록 반환

        Returns:
            [(tool_name, priority_score), ...] 내림차순
        """
        metrics = self.get_all_metrics()
        return [(m.tool_name, m.priority_score) for m in metrics]

    # === LLM 기반 분석 (주기적) ===

    async def analyze(
        self,
        request: LearningAnalysisRequest,
    ) -> LearningAnalysisResult:
        """도구 사용 패턴 분석 → 개선 제안

        주기적으로 호출 (매일 1회 또는 수동 트리거)
        cheap 모델로 메트릭 분석 → 인사이트 생성

        Args:
            request: 분석 요청 (query 필드는 무시 — 메트릭 기반 분석)

        Returns:
            분석 결과 (이벤트 수 + 메모리 업데이트)
        """
        metrics = self.get_all_metrics()
        if not metrics:
            return LearningAnalysisResult()

        # 분석 대상: 최소 호출 수 이상인 도구만
        active_metrics = [m for m in metrics if m.total_calls >= self._min_calls_for_priority]
        if not active_metrics:
            return LearningAnalysisResult()

        # cheap 모델로 분석
        user_prompt = self._build_metrics_prompt(active_metrics)
        raw_response = await self._call_cheap_llm(
            TOOL_ANALYSIS_SYSTEM,
            user_prompt,
        )

        parsed = self._parse_json_response(raw_response)
        insights = parsed.get("insights", [])
        overall = parsed.get("overall_recommendation", "")

        events_stored = 0
        memory_topics_updated: list[str] = []

        # 인사이트별 이벤트 저장
        for insight in insights:
            if not isinstance(insight, dict):
                continue
            event_id = self._store_learning_event(
                LearningEventType.TOOL_PRIORITY_ADJUSTED,
                {
                    "tool_name": insight.get("tool_name", ""),
                    "issue": insight.get("issue", ""),
                    "suggestion": insight.get("suggestion", ""),
                    "priority": insight.get("priority", "medium"),
                },
            )
            if event_id:
                events_stored += 1

        # 메모리에 인사이트 축적
        if insights or overall:
            updated = self._update_tool_insights_memory(
                scope=request.scope,
                metrics=active_metrics,
                overall=overall,
            )
            if updated:
                memory_topics_updated.append(TOOL_INSIGHTS_DOMAIN)

        return LearningAnalysisResult(
            events_stored=events_stored,
            memory_topics_updated=memory_topics_updated,
        )

    def _build_metrics_prompt(self, metrics: list[ToolMetrics]) -> str:
        """메트릭 분석용 프롬프트 생성"""
        lines = ["도구별 성능 통계 (최근 {}일):".format(self._metrics_window_days)]

        for m in metrics:
            line = (
                f"- {m.tool_name}: "
                f"성공 {m.success_count}회 / 실패 {m.failure_count}회 "
                f"(성공률 {m.success_rate:.0%}) / "
                f"평균 응답 {m.avg_latency_ms:.0f}ms / "
                f"우선순위 {m.priority_score:.2f}"
            )
            if m.error_patterns:
                line += f" / 에러: {m.error_patterns[0][:100]}"
            lines.append(line)

        return "\n".join(lines)

    def _update_tool_insights_memory(
        self,
        scope: str,
        metrics: list[ToolMetrics],
        overall: str,
    ) -> bool:
        """도구 최적화 인사이트를 메모리에 저장"""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

        lines = [f"## 도구 최적화 인사이트 ({timestamp})\n"]
        if overall:
            lines.append(f"**전략**: {overall}\n")

        lines.append("**성능 요약**:")
        for m in metrics[:10]:
            status = "✅" if m.success_rate >= 0.8 else "⚠️"
            lines.append(
                f"- {status} {m.tool_name}: "
                f"성공률 {m.success_rate:.0%} / "
                f"{m.avg_latency_ms:.0f}ms / "
                f"priority {m.priority_score:.2f}"
            )

        return self._upsert_memory_topic(
            scope=scope,
            domain=TOOL_INSIGHTS_DOMAIN,
            summary="도구 사용 최적화 인사이트 (자동 생성)",
            content="\n".join(lines),
        )
