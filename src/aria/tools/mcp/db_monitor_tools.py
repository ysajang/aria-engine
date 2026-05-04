"""ARIA Engine - MCP Tool: Supabase DB 모니터링

ToolExecutor 1종 — 에이전트가 온디맨드로 호출 가능
- DbAuditTool: DB 종합 감사 (헬스 + 슬로우쿼리 + RLS + 린트)

인증: ProductConfig.supabase_service_role_key 사용
설계: monitoring/db_checks.py 핵심 로직 재사용 + ToolResult 래핑
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from aria.monitoring.db_checks import (
    check_db_health,
    check_rls_policies,
    check_slow_queries,
    run_db_audit,
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


class DbAuditTool(ToolExecutor):
    """Supabase DB 종합 감사 도구

    프로젝트의 DB 상태 / 슬로우 쿼리 / RLS 정책 / 구조 린트를 검사합니다
    """

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="db_audit",
            description=(
                "Supabase 프로젝트의 DB 상태를 종합 검사합니다. "
                "DB 사이즈, 커넥션 수, 슬로우 쿼리, RLS 미적용 테이블, "
                "데드 튜플, 구조적 린트를 점검합니다. "
                "Testorum, Mystel 등 Supabase 기반 서비스의 DB 건강 상태를 확인할 때 사용합니다."
            ),
            parameters=[
                ToolParameter(
                    name="project_ref",
                    type="string",
                    description="Supabase 프로젝트 참조 ID (예: abcdefghijk)",
                    required=True,
                ),
                ToolParameter(
                    name="service_role_key",
                    type="string",
                    description=(
                        "Supabase service_role key "
                        "(미지정 시 환경변수 SUPABASE_SERVICE_ROLE_KEY 사용)"
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="min_slow_ms",
                    type="number",
                    description="슬로우 쿼리 기준 (ms / 기본: 1000)",
                    required=False,
                    default=1000.0,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        project_ref = parameters.get("project_ref", "").strip()
        if not project_ref:
            return ToolResult(
                tool_name="db_audit",
                success=False,
                error="project_ref가 비어있습니다",
            )

        service_role_key = parameters.get("service_role_key") or os.environ.get(
            "SUPABASE_SERVICE_ROLE_KEY", ""
        )
        if not service_role_key:
            return ToolResult(
                tool_name="db_audit",
                success=False,
                error=(
                    "service_role_key 미설정. "
                    "파라미터 또는 환경변수 SUPABASE_SERVICE_ROLE_KEY 설정 필요"
                ),
            )

        access_token = os.environ.get("SUPABASE_ACCESS_TOKEN")
        min_slow_ms = float(parameters.get("min_slow_ms", 1000.0))

        try:
            audit = await run_db_audit(
                project_ref=project_ref,
                service_role_key=service_role_key,
                access_token=access_token,
                min_slow_ms=min_slow_ms,
            )

            # LLM 친화적 요약
            health = audit.get("health", {})
            slow = audit.get("slow_queries", {})
            rls = audit.get("rls", {})
            lints = audit.get("lints", {})

            summary_parts = [
                f"DB 감사 결과: {project_ref}",
                f"상태: {health.get('status', 'unknown')} / "
                f"사이즈: {health.get('db_size', 'unknown')} / "
                f"커넥션: {health.get('active_connections', 0)}/{health.get('max_connections', 0)}",
            ]

            slow_count = slow.get("total_found", 0)
            if slow_count:
                summary_parts.append(f"슬로우 쿼리: {slow_count}개 발견")

            unprotected = rls.get("unprotected_tables", [])
            if unprotected:
                table_names = [t.get("table_name", "?") for t in unprotected[:5]]
                summary_parts.append(f"RLS 미적용: {', '.join(table_names)}")

            lint_count = lints.get("total_lints", 0) if isinstance(lints, dict) else 0
            if lint_count:
                summary_parts.append(
                    f"DB 린트: {lint_count}개 "
                    f"(에러: {lints.get('error_count', 0)} / 경고: {lints.get('warning_count', 0)})"
                )

            summary_parts.append(
                f"총 이슈: {audit.get('total_issues', 0)}개 "
                f"(심각: {audit.get('high_issues', 0)}개)"
            )

            # 이슈 상세
            all_issues = audit.get("all_issues", [])
            if all_issues:
                summary_parts.append("\n이슈 목록:")
                for issue in all_issues[:10]:
                    severity_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
                        issue.get("severity", ""), "⚪"
                    )
                    summary_parts.append(
                        f"  {severity_emoji} [{issue.get('type', '')}] {issue.get('message', '')}"
                    )

            return ToolResult(
                tool_name="db_audit",
                success=True,
                output=audit,
                summary="\n".join(summary_parts),
            )

        except Exception as e:
            logger.error("db_audit_failed", project_ref=project_ref, error=str(e)[:200])
            return ToolResult(
                tool_name="db_audit",
                success=False,
                error=f"DB 감사 실패: {e}",
            )
