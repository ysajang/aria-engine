"""ARIA Engine - MCP Tool: API Contract Testing

ToolExecutor 1종 — 에이전트 온디맨드 호출 또는 cron 사용
- ApiContractTestTool: API 엔드포인트 응답 스키마 검증

인증: 대상 API 인증 정보 (파라미터로 전달)
설계: monitoring/contract_checks.py 핵심 로직 재사용 + ToolResult 래핑
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from aria.monitoring.contract_checks import (
    check_endpoint,
    run_contract_tests,
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


class ApiContractTestTool(ToolExecutor):
    """API Contract Test 도구

    제품 API 엔드포인트를 호출하고 응답 상태코드 + JSON 스키마를 검증합니다
    제품의 API가 정상 동작하고 예상된 형태로 응답하는지 확인할 때 사용합니다
    """

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="api_contract_test",
            description=(
                "제품 API 엔드포인트의 응답 스키마를 검증합니다. "
                "상태코드 확인 + JSON 스키마 검증으로 API가 "
                "예상대로 동작하는지 확인합니다. "
                "배포 후 API 정상 동작 확인이나 정기 검증에 사용합니다."
            ),
            parameters=[
                ToolParameter(
                    name="url",
                    type="string",
                    description="테스트할 API 엔드포인트 URL",
                    required=True,
                ),
                ToolParameter(
                    name="method",
                    type="string",
                    description="HTTP 메서드 (GET/POST/PUT/DELETE / 기본: GET)",
                    required=False,
                    default="GET",
                ),
                ToolParameter(
                    name="expected_status",
                    type="integer",
                    description="예상 HTTP 상태코드 (기본: 200)",
                    required=False,
                    default=200,
                ),
                ToolParameter(
                    name="expected_schema",
                    type="string",
                    description="예상 응답 JSON Schema (JSON 문자열)",
                    required=False,
                ),
                ToolParameter(
                    name="auth_header",
                    type="string",
                    description="인증 헤더 값 (예: Bearer xxx / Basic xxx)",
                    required=False,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        url = parameters.get("url", "").strip()
        if not url:
            return ToolResult(
                tool_name="api_contract_test",
                success=False,
                output={"error": "url 필수"},
                error="테스트할 URL이 지정되지 않았습니다",
            )

        # 스키마 파싱
        expected_schema = None
        schema_str = parameters.get("expected_schema")
        if schema_str:
            try:
                expected_schema = json.loads(schema_str)
            except json.JSONDecodeError as e:
                return ToolResult(
                    tool_name="api_contract_test",
                    success=False,
                    output={"error": f"스키마 JSON 파싱 실패: {e}"},
                    error="expected_schema가 유효한 JSON이 아닙니다",
                )

        # 인증 헤더
        headers = None
        auth_header = parameters.get("auth_header")
        if auth_header:
            headers = {"Authorization": auth_header}

        try:
            result = await check_endpoint(
                url=url,
                method=parameters.get("method", "GET"),
                headers=headers,
                expected_status=parameters.get("expected_status", 200),
                expected_schema=expected_schema,
            )

            return ToolResult(
                tool_name="api_contract_test",
                success=result["passed"],
                output=result,
                error=result["issues"][0]["message"] if result["issues"] else None,
            )
        except Exception as e:
            logger.error("contract_test_tool_error", error=str(e))
            return ToolResult(
                tool_name="api_contract_test",
                success=False,
                output={"error": str(e)},
                error=f"Contract Test 실패: {e}",
            )
