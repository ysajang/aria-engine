"""ARIA Engine - MCP Tool: 의존성 취약점 스캔

ToolExecutor 1종 — 에이전트가 온디맨드로 호출 가능
- DependencyAuditTool: GitHub Dependabot 기반 의존성 감사

인증: GITHUB_TOKEN 환경변수
설계: monitoring/dep_checks.py 핵심 로직 재사용 + ToolResult 래핑
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from aria.monitoring.dep_checks import (
    check_dependabot_alerts,
    run_dependency_audit,
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


class DependencyAuditTool(ToolExecutor):
    """의존성 취약점 스캔 도구

    GitHub Dependabot을 통해 레포의 취약한 의존성을 검사합니다
    """

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="dependency_audit",
            description=(
                "GitHub 레포의 의존성 취약점을 검사합니다. "
                "Dependabot 알림을 조회하여 critical/high/medium/low 등급별 "
                "취약점을 보고합니다. CVE ID와 패치 버전도 포함됩니다. "
                "Testorum, ARIA Engine 등의 코드 보안 상태를 확인할 때 사용합니다."
            ),
            parameters=[
                ToolParameter(
                    name="repo_urls",
                    type="string",
                    description=(
                        "GitHub 레포 URL (쉼표 구분 가능. "
                        "예: 'https://github.com/Testorum/testorum.git' 또는 "
                        "'Testorum/testorum,LordOfWins/aria-engine')"
                    ),
                    required=True,
                ),
                ToolParameter(
                    name="github_token",
                    type="string",
                    description="GitHub PAT (미지정 시 환경변수 GITHUB_TOKEN 사용)",
                    required=False,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        urls_raw = parameters.get("repo_urls", "").strip()
        if not urls_raw:
            return ToolResult(
                tool_name="dependency_audit",
                success=False,
                error="repo_urls가 비어있습니다",
            )

        repo_urls = [u.strip() for u in urls_raw.split(",") if u.strip()]
        if not repo_urls:
            return ToolResult(
                tool_name="dependency_audit",
                success=False,
                error="유효한 레포 URL이 없습니다",
            )

        github_token = parameters.get("github_token") or os.environ.get("GITHUB_TOKEN", "")
        if not github_token:
            return ToolResult(
                tool_name="dependency_audit",
                success=False,
                error="GITHUB_TOKEN 미설정. 파라미터 또는 환경변수 설정 필요",
            )

        try:
            audit = await run_dependency_audit(
                repo_urls=repo_urls,
                github_token=github_token,
            )

            # LLM 친화적 요약
            summary_parts = [
                f"의존성 감사 결과: {audit.get('repo_count', 0)}개 레포",
                f"총 취약점: {audit.get('total_alerts', 0)}개",
                f"  🔴 Critical: {audit.get('critical', 0)}",
                f"  🟠 High: {audit.get('high', 0)}",
                f"  🟡 Medium: {audit.get('medium', 0)}",
                f"  🟢 Low: {audit.get('low', 0)}",
            ]

            # 레포별 요약
            for repo in audit.get("repos", []):
                repo_name = f"{repo.get('owner', '?')}/{repo.get('repo', '?')}"
                summary_parts.append(
                    f"\n{repo_name}: {repo.get('total_alerts', 0)}개 알림 "
                    f"(C:{repo.get('critical', 0)} H:{repo.get('high', 0)} "
                    f"M:{repo.get('medium', 0)} L:{repo.get('low', 0)})"
                )

                # 상위 취약점 나열
                for alert in repo.get("alerts", [])[:5]:
                    severity_emoji = {
                        "critical": "🔴",
                        "high": "🟠",
                        "medium": "🟡",
                        "low": "🟢",
                    }.get(alert.get("severity", ""), "⚪")
                    patched = alert.get("patched_version", "")
                    patch_info = f" → {patched}" if patched else " (패치 없음)"
                    summary_parts.append(
                        f"  {severity_emoji} {alert.get('package', '?')} "
                        f"{alert.get('vulnerable_range', '?')}{patch_info}: "
                        f"{alert.get('summary', '?')[:80]}"
                    )

            return ToolResult(
                tool_name="dependency_audit",
                success=True,
                output=audit,
                summary="\n".join(summary_parts),
            )

        except Exception as e:
            logger.error("dependency_audit_failed", error=str(e)[:200])
            return ToolResult(
                tool_name="dependency_audit",
                success=False,
                error=f"의존성 감사 실패: {e}",
            )
