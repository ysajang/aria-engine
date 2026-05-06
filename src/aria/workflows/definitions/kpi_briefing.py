"""#9 주간 KPI 브리핑

매주 월요일 또는 /admin kpi 명령으로 트리거
각 제품 핵심 지표 + 비용 집계 → 템플릿 리포트

LLM: 불필요 (EventStore 집계 + 템플릿)
도구: /v1/events/stats + /v1/cost (내부 API)
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


def build_kpi_briefing(
    event_store: Any = None,
    aria_client: Any = None,
    **kwargs: Any,
) -> tuple[WorkflowDefinition, dict[str, WorkflowFunc]]:
    """KPI 브리핑 워크플로우 빌드"""

    async def collect_kpis(ctx: WorkflowContext) -> dict[str, Any]:
        """각 제품 이벤트 통계 + 비용 수집"""
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        since = week_ago.isoformat()
        until = now.isoformat()

        products_data = []
        sources = ["testorum", "talksim", "autotube"]

        for source in sources:
            kpis = []
            if event_store:
                from aria.events.types import EventQuery

                events = await event_store.query(EventQuery(
                    source=source,
                    since=since,
                    until=until,
                    limit=500,
                ))

                # 이벤트 타입별 집계
                type_counts: dict[str, int] = {}
                for ev in events:
                    t = ev.event_type
                    type_counts[t] = type_counts.get(t, 0) + 1

                total = len(events)
                kpis.append({"label": "총 이벤트", "value": f"{total}건", "change": ""})

                for et, count in sorted(type_counts.items(), key=lambda x: -x[1])[:3]:
                    kpis.append({"label": et, "value": f"{count}건", "change": ""})
            else:
                kpis.append({"label": "총 이벤트", "value": "데이터 없음", "change": ""})

            products_data.append({"name": source.capitalize(), "kpis": kpis})

        # 비용 수집
        daily_avg = 0.0
        weekly_total = 0.0
        monthly_total = 0.0
        monthly_limit = 300.0

        if aria_client:
            cost_data = await aria_client.get_cost()
            if "error" not in cost_data:
                monthly_total = cost_data.get("monthly_cost_usd", 0)
                monthly_limit = cost_data.get("monthly_limit_usd", 300)
                daily_avg = monthly_total / max(now.day, 1)
                weekly_total = daily_avg * 7

        ctx.set("period", f"{week_ago.strftime('%m/%d')} ~ {now.strftime('%m/%d')}")

        return {
            "products": products_data,
            "daily_avg": daily_avg,
            "weekly_total": weekly_total,
            "monthly_total": monthly_total,
            "monthly_limit": monthly_limit,
        }

    definition = WorkflowDefinition(
        workflow_id="kpi-briefing",
        name="주간 KPI 브리핑",
        description="각 제품 핵심 지표 + 비용 집계 리포트",
        category="admin",
        scope="global",
        steps=[
            WorkflowStep(
                name="collect-kpis",
                step_type=StepType.FUNCTION,
                config={"func_name": "kpi_collect"},
                output_key="kpi_data",
                description="제품별 이벤트 통계 + 비용 수집",
            ),
            WorkflowStep(
                name="render-report",
                step_type=StepType.TEMPLATE,
                config={"template_name": "admin/kpi_briefing.md.j2"},
                output_key="report",
                description="KPI 브리핑 리포트 렌더링",
            ),
        ],
    )

    functions = {"kpi_collect": collect_kpis}
    return definition, functions
