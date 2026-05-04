"""ARIA Engine - MCP Tool: 결제 이상 감지

ToolExecutor 1종 — 에이전트가 온디맨드로 호출 가능
- PaymentAuditTool: 결제 종합 감사 (환불율 + 실패율 + 이탈률)

인증: ProductConfig.payment_provider 기반 프로바이더별 API 키 사용
설계: monitoring/payment_checks.py 핵심 로직 재사용 + ToolResult 래핑
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from aria.monitoring.payment_checks import run_payment_audit
from aria.tools.tool_types import (
    SafetyLevelHint,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolResult,
)

logger = structlog.get_logger()

# 프로바이더별 환경변수 매핑
PROVIDER_ENV_KEYS: dict[str, str] = {
    "stripe": "STRIPE_SECRET_KEY",
    "lemonsqueezy": "LEMONSQUEEZY_API_KEY",
    "toss": "TOSS_SECRET_KEY",
}


class PaymentAuditTool(ToolExecutor):
    """결제 종합 감사 도구

    프로바이더별 결제 현황을 분석합니다:
    - 환불율 (refund rate)
    - 결제 실패율 (failure rate / Stripe만)
    - 구독 이탈률 (churn rate / Stripe·LemonSqueezy)
    """

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="payment_audit",
            description=(
                "결제 프로바이더의 결제 현황을 종합 분석합니다. "
                "환불율, 결제 실패율, 구독 이탈률을 검사하여 이상 징후를 보고합니다. "
                "Stripe, LemonSqueezy, Toss Payments를 지원합니다. "
                "Testorum이나 Mystel 등의 결제 건전성을 확인할 때 사용합니다."
            ),
            parameters=[
                ToolParameter(
                    name="provider",
                    type="string",
                    description="결제 프로바이더 (stripe / lemonsqueezy / toss)",
                    required=True,
                ),
                ToolParameter(
                    name="api_key",
                    type="string",
                    description=(
                        "프로바이더 API 키 "
                        "(미지정 시 환경변수 사용: STRIPE_SECRET_KEY / "
                        "LEMONSQUEEZY_API_KEY / TOSS_SECRET_KEY)"
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="store_id",
                    type="string",
                    description="LemonSqueezy 스토어 ID (선택 / LemonSqueezy 전용)",
                    required=False,
                ),
                ToolParameter(
                    name="period_days",
                    type="number",
                    description="환불/실패 검사 기간 (일 / 기본: 7)",
                    required=False,
                    default=7.0,
                ),
                ToolParameter(
                    name="churn_period_days",
                    type="number",
                    description="이탈률 검사 기간 (일 / 기본: 30)",
                    required=False,
                    default=30.0,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        provider = parameters.get("provider", "").strip().lower()
        if not provider:
            return ToolResult(
                tool_name="payment_audit",
                success=False,
                error="provider가 비어있습니다. stripe/lemonsqueezy/toss 중 선택",
            )

        if provider not in PROVIDER_ENV_KEYS:
            return ToolResult(
                tool_name="payment_audit",
                success=False,
                error=f"지원하지 않는 프로바이더: '{provider}'. 허용: stripe/lemonsqueezy/toss",
            )

        # API 키 결정: 파라미터 > 환경변수
        api_key = parameters.get("api_key") or os.environ.get(
            PROVIDER_ENV_KEYS[provider], "",
        )
        if not api_key:
            env_var = PROVIDER_ENV_KEYS[provider]
            return ToolResult(
                tool_name="payment_audit",
                success=False,
                error=f"{env_var} 미설정. 파라미터 또는 환경변수 설정 필요",
            )

        store_id = parameters.get("store_id")
        period_days = int(parameters.get("period_days", 7))
        churn_period_days = int(parameters.get("churn_period_days", 30))

        try:
            audit = await run_payment_audit(
                provider=provider,
                api_key=api_key,
                store_id=store_id,
                period_days=period_days,
                churn_period_days=churn_period_days,
            )

            # LLM 친화적 요약 생성
            summary_parts = [
                f"결제 감사 결과: {provider.upper()}",
                f"검사 기간: 환불/실패 {period_days}일 / 이탈률 {churn_period_days}일",
            ]

            # 환불
            refunds = audit.get("refunds", {})
            if refunds.get("check_type") == "refund_rate":
                total = refunds.get("total_charges", refunds.get("total_orders", refunds.get("total_payments", 0)))
                refund_count = refunds.get("total_refunds", refunds.get("total_cancels", 0))
                rate = refunds.get("refund_rate", 0)
                emoji = "🔴" if rate >= 10 else "🟡" if rate >= 5 else "🟢"
                summary_parts.append(
                    f"\n{emoji} 환불율: {rate:.1f}% ({refund_count}/{total}건)"
                )

            # 실패
            failures = audit.get("failures", {})
            if failures.get("check_type") == "failure_rate":
                fail_rate = failures.get("failure_rate", 0)
                fail_count = failures.get("total_failures", 0)
                total_attempts = failures.get("total_attempts", 0)
                emoji = "🔴" if fail_rate >= 8 else "🟡" if fail_rate >= 3 else "🟢"
                summary_parts.append(
                    f"{emoji} 결제 실패율: {fail_rate:.1f}% ({fail_count}/{total_attempts}건)"
                )
                # 실패 원인
                for reason in failures.get("top_failure_reasons", [])[:3]:
                    summary_parts.append(
                        f"  - {reason.get('reason', '?')}: {reason.get('count', 0)}건"
                    )
            elif failures.get("note"):
                summary_parts.append(f"ℹ️ 결제 실패: {failures['note']}")

            # 이탈
            churn = audit.get("churn", {})
            if churn.get("check_type") == "churn_rate":
                churn_rate = churn.get("churn_rate", 0)
                active = churn.get("active_subscriptions", 0)
                canceled = churn.get("canceled_in_period", 0)
                emoji = "🔴" if churn_rate >= 10 else "🟡" if churn_rate >= 5 else "🟢"
                summary_parts.append(
                    f"{emoji} 구독 이탈률: {churn_rate:.1f}% "
                    f"(활성 {active} / 취소 {canceled})"
                )
                for reason in churn.get("top_cancel_reasons", [])[:3]:
                    summary_parts.append(
                        f"  - {reason.get('reason', '?')}: {reason.get('count', 0)}건"
                    )
            elif churn.get("note"):
                summary_parts.append(f"ℹ️ 이탈률: {churn['note']}")

            # 이슈 요약
            total_issues = audit.get("total_issues", 0)
            high_issues = audit.get("high_issues", 0)
            if total_issues > 0:
                summary_parts.append(
                    f"\n⚠️ 총 {total_issues}건 이슈 (심각: {high_issues}건)"
                )
            else:
                summary_parts.append("\n✅ 이상 징후 없음")

            return ToolResult(
                tool_name="payment_audit",
                success=True,
                output=audit,
                summary="\n".join(summary_parts),
            )

        except Exception as e:
            logger.error("payment_audit_failed", error=str(e)[:200])
            return ToolResult(
                tool_name="payment_audit",
                success=False,
                error=f"결제 감사 실패: {e}",
            )
