"""#7 세금 자료 정리

/admin tax 또는 cron(분기별) 트리거
결제 이벤트 카테고리 분류 → 간이과세 부가세/종소세 자료 정리

LLM: 불필요 (규칙 기반 카테고리 분류 + 템플릿)
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

# 매출 카테고리 분류 규칙 (event_type → 카테고리)
REVENUE_CATEGORIES: dict[str, str] = {
    "payment_completed": "서비스 매출",
    "subscription_created": "구독 매출",
    "subscription_renewed": "구독 매출",
    "adsense_revenue": "광고 수익",
    "affiliate_revenue": "제휴 수익",
}

# 비용 카테고리 분류 규칙
EXPENSE_CATEGORIES: dict[str, str] = {
    "api_cost": "API 비용",
    "server_cost": "서버 비용",
    "domain_cost": "도메인 비용",
    "tool_subscription": "도구 구독",
}

DEFAULT_BUSINESS = {
    "name": "YSajang",
    "number": "",
    "tax_type": "간이과세자",
}


def build_tax_summary(
    event_store: Any = None,
    **kwargs: Any,
) -> tuple[WorkflowDefinition, dict[str, WorkflowFunc]]:
    """세금 자료 정리 워크플로우 빌드"""

    async def categorize_transactions(ctx: WorkflowContext) -> dict[str, Any]:
        """이벤트에서 매출/비용 분류"""
        # 기간 설정 (기본: 이번 달)
        now = datetime.now(timezone(timedelta(hours=9)))
        year = ctx.get("year", now.year)
        month = ctx.get("month", now.month)

        from datetime import date

        period_start = date(year, month, 1)
        if month == 12:
            period_end = date(year + 1, 1, 1)
        else:
            period_end = date(year, month + 1, 1)

        since = datetime(period_start.year, period_start.month, period_start.day, tzinfo=timezone.utc).isoformat()
        until = datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc).isoformat()

        ctx.set("period", f"{year}년 {month}월")
        ctx.set("business", ctx.get("business", DEFAULT_BUSINESS))

        # 이벤트 조회
        revenue_cats: dict[str, dict[str, Any]] = {}
        expense_cats: dict[str, dict[str, Any]] = {}

        if event_store:
            from aria.events.types import EventQuery

            for source in ["testorum", "talksim", "autotube", "aria"]:
                events = event_store.query(EventQuery(
                    source=source,
                    since=since,
                    until=until,
                    limit=500,
                ))

                for ev in events:
                    amount = ev.data.get("amount", 0)
                    if not amount:
                        continue

                    # 매출 분류
                    if ev.event_type in REVENUE_CATEGORIES:
                        cat = REVENUE_CATEGORIES[ev.event_type]
                        if cat not in revenue_cats:
                            revenue_cats[cat] = {"name": cat, "amount": 0, "count": 0}
                        revenue_cats[cat]["amount"] += amount
                        revenue_cats[cat]["count"] += 1

                    # 비용 분류
                    elif ev.event_type in EXPENSE_CATEGORIES:
                        cat = EXPENSE_CATEGORIES[ev.event_type]
                        if cat not in expense_cats:
                            expense_cats[cat] = {"name": cat, "amount": 0}
                        expense_cats[cat]["amount"] += amount

        revenue_list = sorted(revenue_cats.values(), key=lambda x: -x["amount"])
        expense_list = sorted(expense_cats.values(), key=lambda x: -x["amount"])
        total_revenue = sum(c["amount"] for c in revenue_list)
        total_expense = sum(c["amount"] for c in expense_list)
        taxable_income = total_revenue - total_expense

        # 간이과세 부가세 (업종 부가가치율 적용 — 서비스업 30%)
        vat = int(taxable_income * 0.3 * 0.1) if taxable_income > 0 else 0

        return {
            "revenue_categories": revenue_list,
            "expense_categories": expense_list,
            "total_revenue": total_revenue,
            "total_expense": total_expense,
            "taxable_income": taxable_income,
            "vat": vat,
        }

    definition = WorkflowDefinition(
        workflow_id="tax-summary",
        name="세금 자료 정리",
        description="결제 이벤트 분류 → 간이과세 부가세/종소세 자료",
        category="admin",
        scope="global",
        steps=[
            WorkflowStep(
                name="categorize",
                step_type=StepType.FUNCTION,
                config={"func_name": "tax_categorize"},
                output_key="tax_data",
            ),
            WorkflowStep(
                name="render-report",
                step_type=StepType.TEMPLATE,
                config={"template_name": "admin/tax_summary.md.j2"},
                output_key="report",
            ),
        ],
    )

    functions = {"tax_categorize": categorize_transactions}
    return definition, functions
