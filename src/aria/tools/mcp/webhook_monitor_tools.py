"""ARIA Engine - MCP Tool: 웹훅 Reconciliation

ToolExecutor 1종 — 에이전트 온디맨드 호출 또는 cron 사용
- WebhookReconciliationTool: 결제 provider API vs DB 대조

인증: 결제 provider API key + Supabase service_role key
설계: monitoring/webhook_checks.py 핵심 로직 재사용 + ToolResult 래핑
"""

from __future__ import annotations

from typing import Any

import structlog

from aria.monitoring.webhook_checks import run_webhook_reconciliation
from aria.tools.tool_types import (
    SafetyLevelHint,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolResult,
)

logger = structlog.get_logger()


class WebhookReconciliationTool(ToolExecutor):
    """웹훅 누락 감지 도구

    결제 provider API에서 최근 주문을 조회하고
    DB purchases 테이블과 대조하여 웹훅 누락 건을 감지합니다
    """

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="webhook_reconciliation",
            description=(
                "결제 웹훅 누락을 감지합니다. "
                "LemonSqueezy/Stripe API에서 최근 주문을 조회하고 "
                "DB purchases 테이블과 대조하여 웹훅이 누락된 건을 찾습니다. "
                "결제 후 DB에 기록이 없는 케이스를 발견할 때 사용합니다."
            ),
            parameters=[
                ToolParameter(
                    name="provider",
                    type="string",
                    description="결제 프로바이더 (lemonsqueezy / stripe)",
                    required=True,
                ),
                ToolParameter(
                    name="api_key",
                    type="string",
                    description="결제 프로바이더 API 키",
                    required=True,
                ),
                ToolParameter(
                    name="supabase_url",
                    type="string",
                    description="Supabase 프로젝트 URL",
                    required=True,
                ),
                ToolParameter(
                    name="supabase_service_key",
                    type="string",
                    description="Supabase service_role key",
                    required=True,
                ),
                ToolParameter(
                    name="store_id",
                    type="string",
                    description="LemonSqueezy Store ID (LS 전용)",
                    required=False,
                ),
                ToolParameter(
                    name="lookback_minutes",
                    type="integer",
                    description="조회 기간 (분 / 기본: 30)",
                    required=False,
                    default=30,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        provider = parameters.get("provider", "").strip().lower()
        api_key = parameters.get("api_key", "").strip()
        supabase_url = parameters.get("supabase_url", "").strip()
        supabase_service_key = parameters.get("supabase_service_key", "").strip()

        if not all([provider, api_key, supabase_url, supabase_service_key]):
            return ToolResult(
                tool_name="webhook_reconciliation",
                success=False,
                output={"error": "필수 파라미터 누락"},
                error="provider, api_key, supabase_url, supabase_service_key 모두 필수입니다",
            )

        try:
            result = await run_webhook_reconciliation(
                provider=provider,
                api_key=api_key,
                supabase_url=supabase_url,
                supabase_service_key=supabase_service_key,
                store_id=parameters.get("store_id"),
                lookback_minutes=parameters.get("lookback_minutes", 30),
            )

            if "error" in result and not result.get("issues"):
                return ToolResult(
                    tool_name="webhook_reconciliation",
                    success=False,
                    output=result,
                    error=result["error"],
                )

            return ToolResult(tool_name="webhook_reconciliation", success=True, output=result)
        except Exception as e:
            logger.error("webhook_recon_tool_error", error=str(e))
            return ToolResult(
                tool_name="webhook_reconciliation",
                success=False,
                output={"error": str(e)},
                error=f"웹훅 reconciliation 실패: {e}",
            )
