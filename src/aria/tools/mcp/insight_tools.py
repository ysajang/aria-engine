"""ARIA Engine - MCP Tool: 제품 인사이트 (Phase B: AI 개인화)

ToolExecutor 2종:
- ProductInsightTool: 제품별 건전성 점수 + 개인화 인사이트 + 맞춤 제안
- SnapshotRecordTool: 모니터링 결과 스냅샷 기록

설계:
- InsightStore 인스턴스를 주입받아 제품별 이력 활용
- 체크 결과 → 스냅샷 자동 기록 → 추세 분석 → 인사이트 생성
"""

from __future__ import annotations

from typing import Any

import structlog

from aria.monitoring.insight_engine import (
    generate_health_score,
    generate_insights,
    generate_recommendations,
)
from aria.monitoring.insight_store import InsightStore
from aria.tools.tool_types import (
    SafetyLevelHint,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolResult,
)

logger = structlog.get_logger()


class ProductInsightTool(ToolExecutor):
    """제품 인사이트 도구

    축적된 모니터링 이력을 분석하여 건전성 점수,
    추세 인사이트, 맞춤 제안을 제공합니다.
    """

    def __init__(self, store: InsightStore) -> None:
        self._store = store

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="product_insight",
            description=(
                "제품별 건전성 점수와 개인화 인사이트를 제공합니다. "
                "축적된 모니터링 이력을 분석하여 추세, 이상 탐지, "
                "맞춤 제안을 생성합니다. 데이터가 많을수록 정확합니다."
            ),
            parameters=[
                ToolParameter(
                    name="product_id",
                    type="string",
                    description="제품 ID (예: testorum)",
                    required=True,
                ),
                ToolParameter(
                    name="window",
                    type="number",
                    description="분석 윈도우 (스냅샷 수 / 기본: 7)",
                    required=False,
                    default=7.0,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        product_id = parameters.get("product_id", "").strip().lower()
        if not product_id:
            return ToolResult(
                tool_name="product_insight",
                success=False,
                error="product_id가 비어있습니다",
            )

        window = int(parameters.get("window", 7))
        snap_count = self._store.snapshot_count(product_id)

        try:
            health = generate_health_score(self._store, product_id, window)
            insights = generate_insights(self._store, product_id, window)
            recs = generate_recommendations(self._store, product_id, window=window)

            output = {
                "product_id": product_id,
                "snapshot_count": snap_count,
                "health": health,
                "insights": insights,
                "recommendations": recs,
                "insight_count": len(insights),
                "recommendation_count": len(recs),
            }

            return ToolResult(
                tool_name="product_insight",
                success=True,
                output=output,
            )

        except Exception as e:
            logger.error("product_insight_failed", error=str(e)[:200])
            return ToolResult(
                tool_name="product_insight",
                success=False,
                error=f"인사이트 생성 실패: {e}",
            )


class SnapshotRecordTool(ToolExecutor):
    """모니터링 스냅샷 기록 도구

    모니터링 결과를 InsightStore에 기록하여
    향후 추세 분석과 인사이트 생성에 활용합니다.
    """

    def __init__(self, store: InsightStore) -> None:
        self._store = store

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="snapshot_record",
            description=(
                "모니터링 결과를 스냅샷으로 기록합니다. "
                "축적된 스냅샷은 product_insight 도구에서 "
                "추세 분석과 인사이트 생성에 사용됩니다. "
                "각 모니터링 도구 실행 후 결과를 이 도구로 기록하세요."
            ),
            parameters=[
                ToolParameter(
                    name="product_id",
                    type="string",
                    description="제품 ID (예: testorum)",
                    required=True,
                ),
                ToolParameter(
                    name="metrics",
                    type="object",
                    description=(
                        "지표 dict (예: {\"bounce_rate\": 45.0, \"refund_rate\": 2.0}). "
                        "지원 지표: bounce_rate/refund_rate/failure_rate/churn_rate/"
                        "conversion_rate/monthly_pct/engagement_rate/sessions"
                    ),
                    required=True,
                ),
                ToolParameter(
                    name="issues_count",
                    type="number",
                    description="이슈 수 (기본: 0)",
                    required=False,
                    default=0.0,
                ),
                ToolParameter(
                    name="high_issues",
                    type="number",
                    description="심각 이슈 수 (기본: 0)",
                    required=False,
                    default=0.0,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.WRITE,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        product_id = parameters.get("product_id", "").strip().lower()
        if not product_id:
            return ToolResult(
                tool_name="snapshot_record",
                success=False,
                error="product_id가 비어있습니다",
            )

        metrics = parameters.get("metrics", {})
        if not isinstance(metrics, dict) or not metrics:
            return ToolResult(
                tool_name="snapshot_record",
                success=False,
                error="metrics가 비어있습니다. 지표 dict를 전달하세요",
            )

        # 숫자 값만 필터
        clean_metrics: dict[str, float] = {}
        for k, v in metrics.items():
            try:
                clean_metrics[k] = float(v)
            except (TypeError, ValueError):
                continue

        if not clean_metrics:
            return ToolResult(
                tool_name="snapshot_record",
                success=False,
                error="유효한 숫자 지표가 없습니다",
            )

        issues_count = int(parameters.get("issues_count", 0))
        high_issues = int(parameters.get("high_issues", 0))

        try:
            snap = self._store.add_snapshot(
                product_id=product_id,
                metrics=clean_metrics,
                issues_count=issues_count,
                high_issues=high_issues,
            )

            total = self._store.snapshot_count(product_id)

            return ToolResult(
                tool_name="snapshot_record",
                success=True,
                output={
                    "product_id": product_id,
                    "recorded_metrics": list(clean_metrics.keys()),
                    "metric_count": len(clean_metrics),
                    "total_snapshots": total,
                    "timestamp": snap.timestamp,
                },
            )

        except Exception as e:
            logger.error("snapshot_record_failed", error=str(e)[:200])
            return ToolResult(
                tool_name="snapshot_record",
                success=False,
                error=f"스냅샷 기록 실패: {e}",
            )
