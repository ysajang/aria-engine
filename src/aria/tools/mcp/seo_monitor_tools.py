"""ARIA Engine - MCP Tool: SEO 모니터링

ToolExecutor 1종 — 에이전트가 온디맨드로 호출 가능
- SeoAuditTool: SEO 종합 감사 (메타태그 + 깨진 링크 + robots/sitemap)

인증: 불필요 (ARIA 자체 모니터링)
설계: monitoring/seo_checks.py 핵심 로직 재사용 + ToolResult 래핑
"""

from __future__ import annotations

from typing import Any

import structlog

from aria.monitoring.seo_checks import (
    check_broken_links,
    check_meta_tags,
    check_robots_sitemap,
    run_seo_audit,
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


class SeoAuditTool(ToolExecutor):
    """SEO 종합 감사 도구

    페이지의 메타태그 / 깨진 링크 / robots.txt / sitemap.xml을 검사하여
    SEO 이슈를 발견하고 점수를 매깁니다
    """

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="seo_audit",
            description=(
                "웹페이지의 SEO 상태를 종합 검사합니다. "
                "메타태그(title/description/OG), 깨진 링크, "
                "robots.txt, sitemap.xml을 점검합니다. "
                "Testorum, Mystel 등 서비스의 검색엔진 최적화 상태를 확인할 때 사용합니다."
            ),
            parameters=[
                ToolParameter(
                    name="url",
                    type="string",
                    description="검사할 페이지 URL (예: https://testorum.app)",
                    required=True,
                ),
                ToolParameter(
                    name="check_links",
                    type="boolean",
                    description="깨진 링크 검사 포함 여부 (기본: true / false면 메타+robots만)",
                    required=False,
                    default=True,
                ),
                ToolParameter(
                    name="max_links",
                    type="integer",
                    description="검사할 최대 링크 수 (기본: 30)",
                    required=False,
                    default=30,
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
                tool_name="seo_audit",
                success=False,
                error="url이 비어있습니다",
            )

        if not url.startswith(("http://", "https://")):
            return ToolResult(
                tool_name="seo_audit",
                success=False,
                error="url은 http:// 또는 https://로 시작해야 합니다",
            )

        check_links = parameters.get("check_links", True)
        max_links = int(parameters.get("max_links", 30))

        try:
            audit = await run_seo_audit(
                url=url,
                check_links=check_links,
                max_links=max_links,
            )

            # LLM 친화적 요약 생성
            meta = audit.get("meta", {})
            links = audit.get("links", {})
            robots = audit.get("robots_sitemap", {})

            summary_parts = [
                f"SEO 감사 결과: {url}",
                f"메타태그 점수: {meta.get('score', 0)}/{meta.get('max_score', 7)}",
            ]

            if check_links:
                summary_parts.append(
                    f"링크: {links.get('checked', 0)}개 검사 / {links.get('broken', 0)}개 깨짐"
                )

            summary_parts.append(
                f"robots.txt: {'있음' if robots.get('robots_exists') else '없음'} / "
                f"sitemap.xml: {'있음' if robots.get('sitemap_exists') else '없음'} "
                f"({robots.get('sitemap_url_count', 0)}개 URL)"
            )

            summary_parts.append(
                f"총 이슈: {audit.get('total_issues', 0)}개 "
                f"(심각: {audit.get('high_issues', 0)}개)"
            )

            # 이슈 상세
            all_issues = audit.get("all_issues", [])
            if all_issues:
                summary_parts.append("\n이슈 목록:")
                for issue in all_issues[:10]:  # 최대 10개만
                    severity_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
                        issue.get("severity", ""), "⚪"
                    )
                    summary_parts.append(
                        f"  {severity_emoji} [{issue.get('type', '')}] {issue.get('message', '')}"
                    )
                if len(all_issues) > 10:
                    summary_parts.append(f"  ... 외 {len(all_issues) - 10}개")

            return ToolResult(
                tool_name="seo_audit",
                success=True,
                output=audit,
                summary="\n".join(summary_parts),
            )

        except Exception as e:
            logger.error("seo_audit_failed", url=url, error=str(e)[:200])
            return ToolResult(
                tool_name="seo_audit",
                success=False,
                error=f"SEO 감사 실패: {e}",
            )
