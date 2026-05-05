"""ARIA Engine - MCP Tool: 비용 최적화 모니터링

ToolExecutor 1종 — 에이전트가 온디맨드로 호출 가능
- CostAuditTool: Vercel/Supabase/API 비용 종합 분석 + 절감 제안

인증: VERCEL_TOKEN + SUPABASE_ACCESS_TOKEN + ARIA 내부
설계: monitoring/cost_checks.py 핵심 로직 재사용 + ToolResult 래핑
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from aria.monitoring.cost_checks import run_cost_audit
from aria.tools.tool_types import (
    SafetyLevelHint,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolResult,
)

logger = structlog.get_logger()


class CostAuditTool(ToolExecutor):
    """비용 종합 감사 도구

    Vercel/Supabase/ARIA LLM 비용을 종합 분석하고
    사용량 추세와 절감 제안을 제공합니다.
    """

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="cost_audit",
            description=(
                "Vercel, Supabase, ARIA LLM API 비용을 종합 분석합니다. "
                "사용량 대비 플랜 한도 비율, 비용 추세, 절감 제안을 제공합니다. "
                "Testorum이나 Mystel 등의 인프라 비용 현황을 파악할 때 사용합니다."
            ),
            parameters=[
                ToolParameter(
                    name="supabase_project_ref",
                    type="string",
                    description="Supabase 프로젝트 참조 ID (선택)",
                    required=False,
                ),
                ToolParameter(
                    name="vercel_team_id",
                    type="string",
                    description="Vercel Team ID (개인 계정이면 생략)",
                    required=False,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        vercel_token = os.environ.get("VERCEL_TOKEN", "")
        supabase_token = os.environ.get("SUPABASE_ACCESS_TOKEN", "")
        aria_api_key = os.environ.get("ARIA_API_KEY", "")
        supabase_ref = parameters.get("supabase_project_ref") or os.environ.get("SUPABASE_PROJECT_REF", "")
        vercel_team_id = parameters.get("vercel_team_id") or os.environ.get("VERCEL_TEAM_ID")

        try:
            audit = await run_cost_audit(
                vercel_token=vercel_token or None,
                vercel_team_id=vercel_team_id,
                supabase_token=supabase_token or None,
                supabase_project_ref=supabase_ref or None,
                aria_api_key=aria_api_key or None,
            )

            # LLM 친화적 요약
            parts = ["비용 종합 감사 결과"]

            # Vercel
            v = audit.get("vercel", {})
            if v.get("check_type") == "usage":
                bw_pct = v.get("usage_pct", {}).get("bandwidth_gb", 0)
                fn_pct = v.get("usage_pct", {}).get("function_invocations", 0)
                parts.append(
                    f"\n☁️ Vercel: 대역폭 {bw_pct:.0f}% / 함수 {fn_pct:.0f}%"
                )
            elif v.get("note"):
                parts.append(f"\nℹ️ Vercel: {v['note']}")

            # Supabase
            s = audit.get("supabase", {})
            if s.get("check_type") == "usage":
                db_pct = s.get("usage_pct", {}).get("db_size_gb", 0)
                st_pct = s.get("usage_pct", {}).get("storage_gb", 0)
                parts.append(
                    f"🗄️ Supabase: DB {db_pct:.0f}% / Storage {st_pct:.0f}%"
                )
            elif s.get("note"):
                parts.append(f"ℹ️ Supabase: {s['note']}")

            # API Costs
            a = audit.get("api_costs", {})
            if a.get("check_type") == "api_costs":
                daily = a.get("daily_cost", 0)
                monthly = a.get("monthly_cost", 0)
                d_pct = a.get("daily_pct", 0)
                m_pct = a.get("monthly_pct", 0)
                cost_emoji = "🔴" if m_pct >= 90 else "🟡" if m_pct >= 70 else "🟢"
                parts.append(
                    f"{cost_emoji} LLM: 일 ${daily:.2f} ({d_pct:.0f}%) / 월 ${monthly:.2f} ({m_pct:.0f}%)"
                )

            # 절감 제안
            recs = audit.get("all_recommendations", [])
            if recs:
                parts.append(f"\n💡 절감 제안 ({len(recs)}건):")
                for r in recs[:5]:
                    parts.append(f"  - {r}")

            # 이슈
            total = audit.get("total_issues", 0)
            high = audit.get("high_issues", 0)
            if total > 0:
                parts.append(f"\n⚠️ {total}건 이슈 (심각: {high}건)")
            else:
                parts.append("\n✅ 비용 정상 범위")

            return ToolResult(
                tool_name="cost_audit",
                success=True,
                output=audit,
            )

        except Exception as e:
            logger.error("cost_audit_failed", error=str(e)[:200])
            return ToolResult(
                tool_name="cost_audit",
                success=False,
                error=f"비용 감사 실패: {e}",
            )
