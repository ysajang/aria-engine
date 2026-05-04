"""ARIA Engine - MCP Tool: 사용자 행동 분석

ToolExecutor 1종 — 에이전트가 온디맨드로 호출 가능
- BehaviorAuditTool: GA4 기반 사용자 행동 종합 분석

인증: Google OAuth2 (기존 GoogleTokenManager 재사용)
설계: monitoring/behavior_checks.py 핵심 로직 재사용 + ToolResult 래핑
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from aria.monitoring.behavior_checks import run_behavior_audit
from aria.tools.tool_types import (
    SafetyLevelHint,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolResult,
)

logger = structlog.get_logger()


class BehaviorAuditTool(ToolExecutor):
    """사용자 행동 종합 분석 도구

    GA4 Data API를 통해 트래픽 개요, 이탈 페이지,
    전환율 변화, 퍼널 이탈을 분석합니다.
    """

    def __init__(self, token_manager: Any = None) -> None:
        """
        Args:
            token_manager: GoogleTokenManager 인스턴스 (access_token 자동 갱신)
                          미지정 시 파라미터로 access_token 직접 전달 필요
        """
        self._token_manager = token_manager

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="behavior_audit",
            description=(
                "GA4 기반 사용자 행동을 종합 분석합니다. "
                "트래픽 개요(세션/이탈률/참여율), 이탈률 높은 페이지, "
                "전환율 변화(현재 vs 이전 기간), 퍼널 단계별 이탈을 검사합니다. "
                "Testorum이나 Mystel 등의 사용자 행동 패턴을 파악할 때 사용합니다."
            ),
            parameters=[
                ToolParameter(
                    name="property_id",
                    type="string",
                    description="GA4 프로퍼티 ID (숫자 / 예: '123456789')",
                    required=True,
                ),
                ToolParameter(
                    name="access_token",
                    type="string",
                    description=(
                        "Google OAuth2 access token "
                        "(미지정 시 GoogleTokenManager 자동 사용)"
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="conversion_event",
                    type="string",
                    description="전환 추적 이벤트명 (기본: purchase)",
                    required=False,
                    default="purchase",
                ),
                ToolParameter(
                    name="period_days",
                    type="number",
                    description="분석 기간 (일 / 기본: 7)",
                    required=False,
                    default=7.0,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def _get_access_token(self, parameters: dict[str, Any]) -> str | None:
        """access_token 확보: 파라미터 > TokenManager"""
        token = parameters.get("access_token")
        if token:
            return token

        if self._token_manager is not None:
            try:
                return await self._token_manager.get_access_token()
            except Exception as e:
                logger.warning("token_manager_failed", error=str(e)[:100])
                return None

        return None

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        property_id = parameters.get("property_id", "").strip()
        if not property_id:
            return ToolResult(
                tool_name="behavior_audit",
                success=False,
                error="property_id가 비어있습니다. GA4 숫자 프로퍼티 ID를 지정하세요",
            )

        access_token = await self._get_access_token(parameters)
        if not access_token:
            return ToolResult(
                tool_name="behavior_audit",
                success=False,
                error=(
                    "Google OAuth2 access_token 미확보. "
                    "파라미터로 직접 전달하거나 GoogleTokenManager 설정 필요"
                ),
            )

        conversion_event = parameters.get("conversion_event", "purchase")
        period_days = int(parameters.get("period_days", 7))

        try:
            audit = await run_behavior_audit(
                property_id=property_id,
                access_token=access_token,
                conversion_event=conversion_event,
                period_days=period_days,
            )

            # LLM 친화적 요약 생성
            summary_parts = [
                f"사용자 행동 분석: GA4 프로퍼티 {property_id}",
                f"분석 기간: {period_days}일",
            ]

            # 트래픽 개요
            overview = audit.get("overview", {})
            if overview.get("check_type") == "traffic_overview":
                sessions = overview.get("sessions", 0)
                users = overview.get("active_users", 0)
                bounce = overview.get("bounce_rate", 0)
                engagement = overview.get("engagement_rate", 0)
                bounce_emoji = "🔴" if bounce >= 85 else "🟡" if bounce >= 70 else "🟢"
                summary_parts.append(
                    f"\n📊 트래픽: {sessions:,}세션 / {users:,}사용자"
                )
                summary_parts.append(
                    f"{bounce_emoji} 이탈률: {bounce:.1f}% / 참여율: {engagement:.1f}%"
                )

            # 이탈 페이지
            exit_pages = audit.get("exit_pages", {})
            problem_count = exit_pages.get("problem_pages", 0)
            if problem_count > 0:
                summary_parts.append(f"\n🚪 문제 페이지: {problem_count}개")
                for page in exit_pages.get("exit_pages", [])[:3]:
                    summary_parts.append(
                        f"  - {page.get('path', '?')}: 이탈률 {page.get('bounce_rate', 0)}%"
                    )

            # 전환율
            conversion = audit.get("conversion", {})
            if conversion.get("check_type") == "conversion_rate":
                current_rate = conversion.get("current_rate", 0)
                change = conversion.get("rate_change_pct", 0)
                change_emoji = "📈" if change > 0 else "📉" if change < 0 else "➡️"
                summary_parts.append(
                    f"\n{change_emoji} 전환율: {current_rate:.2f}% "
                    f"(변화: {change:+.1f}% / 이벤트: {conversion.get('conversion_event', 'purchase')})"
                )

            # 퍼널
            funnel = audit.get("funnel", {})
            bottleneck = funnel.get("bottleneck_step")
            if bottleneck:
                summary_parts.append(f"\n🔻 퍼널 병목: '{bottleneck}'")

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
                tool_name="behavior_audit",
                success=True,
                output=audit,
                summary="\n".join(summary_parts),
            )

        except Exception as e:
            logger.error("behavior_audit_failed", error=str(e)[:200])
            return ToolResult(
                tool_name="behavior_audit",
                success=False,
                error=f"행동 분석 실패: {e}",
            )
