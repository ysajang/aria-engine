"""#4 바이럴 분석

cron 주간 또는 /marketing viral 명령으로 트리거
Testorum 이벤트(완료/공유/이탈) 집계 → 지표 계산 → 리포트

LLM: 불필요 (EventStore SQL집계 + 규칙 계산 + 템플릿)
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from aria.workflows.types import (
    StepType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowFunc,
    WorkflowStep,
)


def build_viral_analysis(
    event_store: Any = None,
    **kwargs: Any,
) -> tuple[WorkflowDefinition, dict[str, WorkflowFunc]]:
    """바이럴 분석 워크플로우 빌드"""

    async def aggregate_events(ctx: WorkflowContext) -> dict[str, Any]:
        """Testorum 이벤트 집계"""
        days = ctx.get("days", 7)
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=days)).isoformat()

        ctx.set("period", f"최근 {days}일 ({(now - timedelta(days=days)).strftime('%m/%d')} ~ {now.strftime('%m/%d')})")

        if not event_store:
            return {"completed": 0, "shared": 0, "dropped": 0, "events": []}

        from aria.events.types import EventQuery

        events = await event_store.query(EventQuery(
            source="testorum",
            since=since,
            limit=500,
        ))

        completed = sum(1 for e in events if e.event_type == "test_completed")
        shared = sum(1 for e in events if e.event_type == "test_shared")
        dropped = sum(1 for e in events if e.event_type == "test_dropped")

        return {
            "completed": completed,
            "shared": shared,
            "dropped": dropped,
            "events": events,
        }

    async def calculate_metrics(ctx: WorkflowContext) -> dict[str, Any]:
        """바이럴 지표 계산"""
        raw = ctx.get("raw_events", {})
        completed = raw.get("completed", 0)
        shared = raw.get("shared", 0)
        dropped = raw.get("dropped", 0)
        events = raw.get("events", [])

        share_rate = shared / max(completed, 1)

        # 테스트별 완료 수 집계
        test_completions: dict[str, int] = {}
        drop_by_question: dict[str, dict[int, int]] = {}

        for ev in events:
            test_name = ev.data.get("test_name", "unknown")
            if ev.event_type == "test_completed":
                test_completions[test_name] = test_completions.get(test_name, 0) + 1
            elif ev.event_type == "test_dropped":
                q_idx = ev.data.get("question_index", 0)
                if test_name not in drop_by_question:
                    drop_by_question[test_name] = {}
                drop_by_question[test_name][q_idx] = drop_by_question[test_name].get(q_idx, 0) + 1

        top_tests = sorted(
            [{"name": k, "completions": v} for k, v in test_completions.items()],
            key=lambda x: -x["completions"],
        )[:5]

        drop_points = []
        for test_name, questions in drop_by_question.items():
            total_drops = sum(questions.values())
            for q_idx, count in sorted(questions.items(), key=lambda x: -x[1])[:2]:
                drop_points.append({
                    "test_name": test_name,
                    "question_index": q_idx,
                    "drop_rate": count / max(total_drops, 1),
                    "count": count,
                })

        return {
            "stats": {
                "completed": completed,
                "shared": shared,
                "dropped": dropped,
                "share_rate": share_rate,
            },
            "top_tests": top_tests,
            "drop_points": sorted(drop_points, key=lambda x: -x["drop_rate"])[:5],
        }

    definition = WorkflowDefinition(
        workflow_id="viral-analysis",
        name="Testorum 바이럴 분석",
        description="Testorum 이벤트 집계 → 바이럴 지표 리포트",
        category="marketing",
        scope="testorum",
        steps=[
            WorkflowStep(
                name="aggregate",
                step_type=StepType.FUNCTION,
                config={"func_name": "viral_aggregate"},
                output_key="raw_events",
            ),
            WorkflowStep(
                name="calculate",
                step_type=StepType.FUNCTION,
                config={"func_name": "viral_calculate"},
                output_key="metrics",
            ),
            WorkflowStep(
                name="render-report",
                step_type=StepType.TEMPLATE,
                config={"template_name": "marketing/viral_report.md.j2"},
                output_key="report",
            ),
        ],
    )

    functions = {
        "viral_aggregate": aggregate_events,
        "viral_calculate": calculate_metrics,
    }
    return definition, functions
