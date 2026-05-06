"""#10 인보이스 자동 생성

/admin invoice 명령으로 트리거
initial_data로 인보이스 정보 전달 → 템플릿 렌더링 → Gmail 초안 생성

LLM: 불필요 (템플릿 + Gmail 도구)
도구: mcp_gmail_create_draft
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

# YSajang 기본 발행인 정보
DEFAULT_ISSUER = {
    "name": "YSajang",
    "business_number": "",  # 실제 사업자번호는 .env 또는 메모리에서
    "address": "",
}


def build_invoice_generator(
    tool_registry: Any = None,
    **kwargs: Any,
) -> tuple[WorkflowDefinition, dict[str, WorkflowFunc]]:
    """인보이스 자동 생성 워크플로우 빌드"""

    async def prepare_invoice(ctx: WorkflowContext) -> dict[str, Any]:
        """인보이스 데이터 검증 및 준비"""
        recipient = ctx.get("recipient")
        items = ctx.get("items")
        if not recipient or not items:
            raise ValueError(
                "recipient와 items가 필요합니다. "
                "예: {recipient: {name, email}, items: [{description, quantity, unit_price}]}"
            )

        # 금액 자동 계산
        for item in items:
            item["total"] = item.get("total", item.get("quantity", 1) * item.get("unit_price", 0))

        total = sum(i["total"] for i in items)
        tax = int(total * 0.1) if ctx.get("include_tax", True) else 0

        # 인보이스 번호 자동 생성
        now = datetime.now(timezone(timedelta(hours=9)))
        invoice_number = ctx.get("invoice_number", f"INV-{now.strftime('%Y%m%d')}-001")

        return {
            "issue_date": now.isoformat(),
            "invoice_number": invoice_number,
            "issuer": ctx.get("issuer", DEFAULT_ISSUER),
            "recipient": recipient,
            "items": items,
            "total": total,
            "tax": tax,
            "grand_total": total + tax,
            "payment_info": ctx.get("payment_info", ""),
        }

    async def create_email_draft(ctx: WorkflowContext) -> dict[str, Any]:
        """인보이스를 Gmail 초안으로 생성"""
        invoice = ctx.get("invoice_data", {})
        body = ctx.get("invoice_body", "")
        recipient = invoice.get("recipient", {})

        if not tool_registry or not recipient.get("email"):
            return {"status": "skipped", "reason": "이메일 미설정 또는 ToolRegistry 없음"}

        result = await tool_registry.execute(
            tool_name="mcp_gmail_create_draft",
            arguments={
                "to": recipient["email"],
                "subject": f"인보이스 {invoice.get('invoice_number', '')}",
                "body": body,
            },
            context="invoice_generator",
        )

        if result.success:
            return result.output or {"status": "draft_created"}
        raise RuntimeError(f"Gmail 초안 생성 실패: {result.error}")

    definition = WorkflowDefinition(
        workflow_id="invoice-generator",
        name="인보이스 자동 생성",
        description="인보이스 렌더링 → Gmail 초안 생성",
        category="admin",
        scope="global",
        steps=[
            WorkflowStep(
                name="prepare",
                step_type=StepType.FUNCTION,
                config={"func_name": "invoice_prepare"},
                output_key="invoice_data",
            ),
            WorkflowStep(
                name="render",
                step_type=StepType.TEMPLATE,
                config={"template_name": "admin/invoice.md.j2"},
                output_key="invoice_body",
            ),
            WorkflowStep(
                name="create-draft",
                step_type=StepType.FUNCTION,
                config={"func_name": "invoice_draft"},
                output_key="email_result",
                on_error=OnError.SKIP,
            ),
        ],
    )

    functions = {
        "invoice_prepare": prepare_invoice,
        "invoice_draft": create_email_draft,
    }
    return definition, functions
