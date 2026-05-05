"""ARIA Engine - MCP Tool: 주간 제품 리포트

ToolExecutor 1종 — 에이전트가 온디맨드로 호출 가능
- WeeklyReportTool: 모니터링 결과 통합 리포트 생성 + 텔레그램 전송

설계: monitoring/weekly_report.py 핵심 로직 재사용 + ToolResult 래핑
체크 결과는 파라미터로 직접 전달받거나, 각 도구를 순차 호출 후 사용
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from aria.monitoring.weekly_report import (
    build_weekly_report,
    generate_report,
    send_report_telegram,
)
from aria.tools.tool_types import (
    SafetyLevelHint,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolResult,
)

logger = structlog.get_logger()


class WeeklyReportTool(ToolExecutor):
    """주간 제품 리포트 생성 도구

    사전 실행된 모니터링 결과를 통합하여 Markdown 리포트를 생성합니다.
    텔레그램 전송도 선택적으로 수행합니다.
    """

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="weekly_report",
            description=(
                "주간 제품 건전성 리포트를 생성합니다. "
                "SEO, DB, 의존성, 결제, 사용자 행동, 비용 모니터링 결과를 "
                "통합하여 Markdown 리포트로 만들고, "
                "선택적으로 텔레그램에 전송합니다. "
                "각 모니터링 도구를 먼저 실행한 후 결과를 이 도구에 전달하세요."
            ),
            parameters=[
                ToolParameter(
                    name="product_name",
                    type="string",
                    description="제품명 (예: Testorum / Mystel)",
                    required=True,
                ),
                ToolParameter(
                    name="period_label",
                    type="string",
                    description="리포트 기간 (예: '2026-05-01 ~ 2026-05-07')",
                    required=True,
                ),
                ToolParameter(
                    name="checks",
                    type="object",
                    description=(
                        "모니터링 결과 dict. 키: seo/db/dep/payment/behavior/cost "
                        "(각 모니터링 도구의 output을 그대로 전달)"
                    ),
                    required=False,
                    default={},
                ),
                ToolParameter(
                    name="send_telegram",
                    type="boolean",
                    description="텔레그램 전송 여부 (기본: false)",
                    required=False,
                    default=False,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        product_name = parameters.get("product_name", "").strip()
        if not product_name:
            return ToolResult(
                tool_name="weekly_report",
                success=False,
                error="product_name이 비어있습니다",
            )

        period_label = parameters.get("period_label", "").strip()
        if not period_label:
            return ToolResult(
                tool_name="weekly_report",
                success=False,
                error="period_label이 비어있습니다",
            )

        checks = parameters.get("checks", {})
        if not isinstance(checks, dict):
            checks = {}

        send_tg = parameters.get("send_telegram", False)

        try:
            report = await generate_report(
                product_name=product_name,
                period_label=period_label,
                checks=checks,
            )

            # 텔레그램 전송
            tg_result = None
            if send_tg:
                bot_token = os.environ.get("ARIA_TELEGRAM_BOT_TOKEN", "")
                chat_id = os.environ.get("ARIA_TELEGRAM_CHAT_ID", "")
                if bot_token and chat_id:
                    tg_result = await send_report_telegram(
                        report["markdown"], bot_token, chat_id,
                    )
                    report["telegram"] = tg_result
                else:
                    report["telegram"] = {
                        "success": False,
                        "error": "ARIA_TELEGRAM_BOT_TOKEN / ARIA_TELEGRAM_CHAT_ID 미설정",
                    }

            return ToolResult(
                tool_name="weekly_report",
                success=True,
                output=report,
            )

        except Exception as e:
            logger.error("weekly_report_failed", error=str(e)[:200])
            return ToolResult(
                tool_name="weekly_report",
                success=False,
                error=f"리포트 생성 실패: {e}",
            )
