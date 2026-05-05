"""ARIA Engine - MCP Tool: 프론트엔드 에러 분석

ToolExecutor 1종 — 에이전트 온디맨드 호출 또는 cron 사용
- FrontendErrorAnalyzeTool: 이벤트 스토어에서 프론트엔드 에러 조회 + 패턴 분석

인증: 불필요 (ARIA 내부 이벤트 스토어 조회)
설계: monitoring/frontend_checks.py 핵심 로직 재사용 + ToolResult 래핑
"""

from __future__ import annotations

from typing import Any

import structlog

from aria.monitoring.frontend_checks import (
    analyze_frontend_errors,
    detect_error_spike,
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


class FrontendErrorAnalyzeTool(ToolExecutor):
    """프론트엔드 JS 에러 분석 도구

    이벤트 스토어에서 frontend_error 이벤트를 조회하여
    에러 패턴 그룹핑 / 스파이크 감지 / 영향 URL 분석을 수행합니다
    """

    def __init__(self, event_store: Any = None) -> None:
        self._event_store = event_store

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="frontend_error_analyze",
            description=(
                "프론트엔드 JS 에러 이벤트를 분석합니다. "
                "에러 패턴 그룹핑, 스파이크 감지, 영향받는 URL/브라우저 분석을 수행합니다. "
                "제품의 클라이언트 에러 현황을 파악할 때 사용합니다."
            ),
            parameters=[
                ToolParameter(
                    name="product_id",
                    type="string",
                    description="분석할 제품 ID (예: testorum)",
                    required=True,
                ),
                ToolParameter(
                    name="hours",
                    type="integer",
                    description="분석 기간 (시간 / 기본: 24)",
                    required=False,
                    default=24,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        product_id = parameters.get("product_id", "").strip()
        hours = parameters.get("hours", 24)

        if not product_id:
            return ToolResult(
                tool_name="frontend_error_analyze",
                success=False,
                output={"error": "product_id 필수"},
                error="product_id가 지정되지 않았습니다",
            )

        if self._event_store is None:
            return ToolResult(
                tool_name="frontend_error_analyze",
                success=False,
                output={"error": "EventStore 미초기화"},
                error="이벤트 스토어가 초기화되지 않았습니다",
            )

        try:
            from aria.events.types import EventQuery

            # 이벤트 스토어에서 프론트엔드 에러 조회
            query = EventQuery(
                source=product_id,
                event_type="frontend_error",
                limit=500,
            )
            events = self._event_store.query(query)
            event_dicts = [e.model_dump(mode="json") for e in events]

            # 분석 실행
            analysis = analyze_frontend_errors(event_dicts, hours=hours)

            # 스파이크 감지
            spike = detect_error_spike(event_dicts)
            analysis["spike"] = spike

            return ToolResult(
                tool_name="frontend_error_analyze",
                success=True,
                output=analysis,
            )
        except Exception as e:
            logger.error("frontend_error_analyze_failed", error=str(e))
            return ToolResult(
                tool_name="frontend_error_analyze",
                success=False,
                output={"error": str(e)},
                error=f"프론트엔드 에러 분석 실패: {e}",
            )
