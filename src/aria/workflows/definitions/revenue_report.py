"""#6 매출/비용 자동 집계

cron 월초 또는 /admin revenue 명령으로 트리거
Supabase MCP execute_sql → 결제 기록 SQL 집계 → 월간 리포트

LLM: 불필요 (SQL 집계 + 규칙 + 템플릿)
도구: mcp_sb_{project}_execute_sql (복수 프로젝트 지원)
전제: Supabase MCP 연결 필요 (ARIA_SUPABASE_* 환경변수)
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from aria.workflows.types import (
    OnError,
    StepType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowFunc,
    WorkflowStep,
)


# 매출 리포트 템플릿 (내장)
REVENUE_REPORT_TEMPLATE = """\
💰 *{{ period }} 매출/비용 리포트*

*매출*
{% if revenue_rows %}
{% for row in revenue_rows %}
  {{ row.category or "미분류" }}: {{ "{:,.0f}".format(row.amount) }}원 ({{ row.count }}건)
{% endfor %}
  합계: {{ "{:,.0f}".format(total_revenue) }}원
{% else %}
  매출 기록 없음
{% endif %}

*비용*
{% if expense_rows %}
{% for row in expense_rows %}
  {{ row.category or "미분류" }}: {{ "{:,.0f}".format(row.amount) }}원
{% endfor %}
  합계: {{ "{:,.0f}".format(total_expense) }}원
{% else %}
  비용 기록 없음
{% endif %}

*손익*
  매출: {{ "{:,.0f}".format(total_revenue) }}원
  비용: {{ "{:,.0f}".format(total_expense) }}원
  순이익: {{ "{:,.0f}".format(net_income) }}원
  마진: {{ "{:.1f}".format(margin_pct) }}%

_Supabase 결제 기록 기준 / 실제 신고는 세무사 확인 필요_
"""


def build_revenue_report(
    tool_registry: Any = None,
    **kwargs: Any,
) -> tuple[WorkflowDefinition, dict[str, WorkflowFunc]]:
    """매출/비용 집계 워크플로우 빌드"""

    async def query_revenue(ctx: WorkflowContext) -> dict[str, Any]:
        """Supabase에서 매출 데이터 SQL 조회

        initial_data로 project 지정 가능 (기본: default)
        도구명 패턴: mcp_sb_{project}_execute_sql
        """
        now = datetime.now(timezone(timedelta(hours=9)))
        year = ctx.get("year", now.year)
        month = ctx.get("month", now.month)

        # 프로젝트명 (도구 접두사와 매칭)
        project = ctx.get("project", "default")
        tool_name = f"mcp_sb_{project}_execute_sql"

        # 테이블/컬럼명 오버라이드
        table = ctx.get("payments_table", "payments")
        date_col = ctx.get("date_column", "created_at")
        amount_col = ctx.get("amount_column", "amount")
        category_col = ctx.get("category_column", "category")
        type_col = ctx.get("type_column", "type")

        ctx.set("period", f"{year}년 {month}월")

        if not tool_registry:
            return {"revenue_rows": [], "expense_rows": []}

        # 매출 집계 SQL
        revenue_sql = (
            f"SELECT {category_col} as category, "
            f"SUM({amount_col}) as amount, "
            f"COUNT(*) as count "
            f"FROM {table} "
            f"WHERE EXTRACT(YEAR FROM {date_col}) = {year} "
            f"AND EXTRACT(MONTH FROM {date_col}) = {month} "
            f"AND {type_col} = 'revenue' "
            f"GROUP BY {category_col} "
            f"ORDER BY amount DESC"
        )

        # 비용 집계 SQL
        expense_sql = (
            f"SELECT {category_col} as category, "
            f"SUM({amount_col}) as amount, "
            f"COUNT(*) as count "
            f"FROM {table} "
            f"WHERE EXTRACT(YEAR FROM {date_col}) = {year} "
            f"AND EXTRACT(MONTH FROM {date_col}) = {month} "
            f"AND {type_col} = 'expense' "
            f"GROUP BY {category_col} "
            f"ORDER BY amount DESC"
        )

        revenue_rows = []
        expense_rows = []

        # 매출 조회
        try:
            result = await tool_registry.execute(
                tool_name=tool_name,
                arguments={"query": revenue_sql},
                context="revenue_report",
            )
            if result.success and result.output:
                rows = result.output if isinstance(result.output, list) else []
                for row in rows:
                    if isinstance(row, dict):
                        revenue_rows.append({
                            "category": row.get("category", "미분류"),
                            "amount": float(row.get("amount", 0)),
                            "count": int(row.get("count", 0)),
                        })
        except Exception:
            pass

        # 비용 조회
        try:
            result = await tool_registry.execute(
                tool_name=tool_name,
                arguments={"query": expense_sql},
                context="revenue_report",
            )
            if result.success and result.output:
                rows = result.output if isinstance(result.output, list) else []
                for row in rows:
                    if isinstance(row, dict):
                        expense_rows.append({
                            "category": row.get("category", "미분류"),
                            "amount": float(row.get("amount", 0)),
                            "count": int(row.get("count", 0)),
                        })
        except Exception:
            pass

        return {"revenue_rows": revenue_rows, "expense_rows": expense_rows}

    async def calculate_summary(ctx: WorkflowContext) -> dict[str, Any]:
        """매출/비용 합계 + 손익 계산"""
        revenue_rows = ctx.get("revenue_rows", [])
        expense_rows = ctx.get("expense_rows", [])

        total_revenue = sum(r.get("amount", 0) for r in revenue_rows)
        total_expense = sum(r.get("amount", 0) for r in expense_rows)
        net_income = total_revenue - total_expense
        margin_pct = (net_income / total_revenue * 100) if total_revenue > 0 else 0.0

        return {
            "total_revenue": total_revenue,
            "total_expense": total_expense,
            "net_income": net_income,
            "margin_pct": margin_pct,
        }

    definition = WorkflowDefinition(
        workflow_id="revenue-report",
        name="매출/비용 자동 집계",
        description="Supabase 결제 기록 SQL 집계 → 월간 리포트",
        category="admin",
        scope="global",
        steps=[
            WorkflowStep(
                name="query-data",
                step_type=StepType.FUNCTION,
                config={"func_name": "revenue_query"},
                output_key="query_result",
            ),
            WorkflowStep(
                name="calculate",
                step_type=StepType.FUNCTION,
                config={"func_name": "revenue_calculate"},
                output_key="summary",
            ),
            WorkflowStep(
                name="render-report",
                step_type=StepType.TEMPLATE,
                config={"template_string": REVENUE_REPORT_TEMPLATE},
                output_key="report",
            ),
        ],
    )

    functions = {
        "revenue_query": query_revenue,
        "revenue_calculate": calculate_summary,
    }
    return definition, functions
