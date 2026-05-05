"""ARIA Engine - Product Insight Engine (Phase B: AI 개인화)

모니터링 이력 기반 맞춤 인사이트 생성
- generate_insights: 추세 + 이상 탐지 → 개인화 인사이트 목록
- generate_health_score: 종합 건전성 점수 (0~100)
- generate_recommendations: 제품 특성 기반 맞춤 제안

설계:
- InsightStore 의존 (스냅샷 이력 필수)
- LLM 호출 없이 통계 기반 인사이트 생성 (비용 0)
- LLM 보강은 선택적 (에이전트가 인사이트를 LLM에 전달하여 자연어화)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from aria.monitoring.insight_store import InsightStore, Trend

logger = structlog.get_logger()

# === Health Score Weights ===
METRIC_WEIGHTS: dict[str, tuple[float, bool]] = {
    # (가중치, lower_is_better)
    "bounce_rate": (0.15, True),
    "refund_rate": (0.20, True),
    "failure_rate": (0.15, True),
    "churn_rate": (0.15, True),
    "conversion_rate": (0.15, False),  # higher is better
    "monthly_pct": (0.10, True),       # cost utilization
    "engagement_rate": (0.10, False),   # higher is better
}

# === Insight Categories ===
CATEGORY_IMPROVING = "improving"
CATEGORY_DECLINING = "declining"
CATEGORY_ANOMALY = "anomaly"
CATEGORY_STABLE = "stable"
CATEGORY_RECOMMENDATION = "recommendation"


def generate_health_score(
    store: InsightStore,
    product_id: str,
    window: int = 7,
) -> dict[str, Any]:
    """종합 건전성 점수 (0~100)

    가중 평균 기반:
    - 각 지표의 현재값 → 0~100 정규화
    - 가중치 적용 → 종합 점수

    Returns:
        {score, grade, metric_scores, snapshot_count, calculated_at}
    """
    result: dict[str, Any] = {
        "product_id": product_id,
        "score": 100.0,
        "grade": "A",
        "metric_scores": {},
        "snapshot_count": store.snapshot_count(product_id),
        "calculated_at": datetime.now(timezone.utc).isoformat(),
    }

    snaps = store.get_snapshots(product_id, last_n=window)
    if not snaps:
        result["score"] = 0.0
        result["grade"] = "N/A"
        return result

    # 마지막 스냅샷 기준
    latest = snaps[-1].metrics
    total_weight = 0.0
    weighted_sum = 0.0

    for metric, (weight, lower_is_better) in METRIC_WEIGHTS.items():
        val = latest.get(metric)
        if val is None:
            continue

        val = float(val)

        # 정규화: 0~100 점수
        if lower_is_better:
            # bounce_rate 0% → 100점 / 100% → 0점
            score = max(0, 100 - val)
        else:
            # conversion_rate 0% → 0점 / 100% → 100점
            score = min(100, val)

        result["metric_scores"][metric] = round(score, 1)
        weighted_sum += score * weight
        total_weight += weight

    if total_weight > 0:
        result["score"] = round(weighted_sum / total_weight, 1)
    else:
        result["score"] = 100.0

    # 등급
    s = result["score"]
    if s >= 90:
        result["grade"] = "A"
    elif s >= 75:
        result["grade"] = "B"
    elif s >= 60:
        result["grade"] = "C"
    elif s >= 40:
        result["grade"] = "D"
    else:
        result["grade"] = "F"

    return result


def generate_insights(
    store: InsightStore,
    product_id: str,
    window: int = 7,
    anomaly_sigma: float = 2.0,
) -> list[dict[str, Any]]:
    """개인화 인사이트 생성

    추세 분석 + 이상 탐지 결과를 인사이트 목록으로 변환

    Returns:
        [{category, metric, title, description, severity, data}]
    """
    insights: list[dict[str, Any]] = []

    # 1. 추세 분석
    trends = store.get_trends(product_id, window=window)
    for trend in trends:
        label = _metric_label(trend.metric)

        if trend.direction == "up":
            lower_is_better = METRIC_WEIGHTS.get(trend.metric, (0, True))[1]

            if lower_is_better and trend.change_pct > 10:
                insights.append({
                    "category": CATEGORY_DECLINING,
                    "metric": trend.metric,
                    "title": f"{label} 악화 추세",
                    "description": (
                        f"{label}이(가) 평균 대비 {trend.change_pct:.1f}% 상승 "
                        f"(현재: {trend.current:.1f} / 평균: {trend.avg:.1f})"
                    ),
                    "severity": "high" if trend.change_pct > 30 else "medium",
                    "data": trend.to_dict(),
                })
            elif not lower_is_better and trend.change_pct > 10:
                insights.append({
                    "category": CATEGORY_IMPROVING,
                    "metric": trend.metric,
                    "title": f"{label} 개선 추세",
                    "description": (
                        f"{label}이(가) 평균 대비 {trend.change_pct:.1f}% 상승 "
                        f"(현재: {trend.current:.1f} / 평균: {trend.avg:.1f})"
                    ),
                    "severity": "info",
                    "data": trend.to_dict(),
                })

        elif trend.direction == "down":
            lower_is_better = METRIC_WEIGHTS.get(trend.metric, (0, True))[1]

            if lower_is_better and abs(trend.change_pct) > 10:
                insights.append({
                    "category": CATEGORY_IMPROVING,
                    "metric": trend.metric,
                    "title": f"{label} 개선 추세",
                    "description": (
                        f"{label}이(가) 평균 대비 {abs(trend.change_pct):.1f}% 감소 "
                        f"(현재: {trend.current:.1f} / 평균: {trend.avg:.1f})"
                    ),
                    "severity": "info",
                    "data": trend.to_dict(),
                })
            elif not lower_is_better and abs(trend.change_pct) > 10:
                insights.append({
                    "category": CATEGORY_DECLINING,
                    "metric": trend.metric,
                    "title": f"{label} 하락 추세",
                    "description": (
                        f"{label}이(가) 평균 대비 {abs(trend.change_pct):.1f}% 감소 "
                        f"(현재: {trend.current:.1f} / 평균: {trend.avg:.1f})"
                    ),
                    "severity": "high" if abs(trend.change_pct) > 30 else "medium",
                    "data": trend.to_dict(),
                })

        elif trend.direction == "stable" and trend.volatility < 5:
            insights.append({
                "category": CATEGORY_STABLE,
                "metric": trend.metric,
                "title": f"{label} 안정",
                "description": (
                    f"{label}이(가) 안정적으로 유지 중 "
                    f"(변동성: {trend.volatility:.1f}%)"
                ),
                "severity": "info",
                "data": trend.to_dict(),
            })

    # 2. 이상 탐지
    anomalies = store.get_anomalies(product_id, window=window, threshold_sigma=anomaly_sigma)
    for anom in anomalies:
        label = _metric_label(anom["metric"])
        insights.append({
            "category": CATEGORY_ANOMALY,
            "metric": anom["metric"],
            "title": f"{label} 이상 감지",
            "description": (
                f"{label}이(가) 평균에서 {anom['deviation_sigma']:.1f}σ 벗어남 "
                f"(현재: {anom['current']:.1f} / 평균: {anom['avg']:.1f} / "
                f"방향: {'상승' if anom['direction'] == 'above' else '하락'})"
            ),
            "severity": "high" if anom["deviation_sigma"] >= 3 else "medium",
            "data": anom,
        })

    return insights


def generate_recommendations(
    store: InsightStore,
    product_id: str,
    product_config: dict[str, Any] | None = None,
    window: int = 7,
) -> list[dict[str, Any]]:
    """제품 특성 기반 맞춤 제안

    Args:
        product_config: ProductConfig.model_dump() 결과 (선택)

    Returns:
        [{title, description, priority, category}]
    """
    recs: list[dict[str, Any]] = []

    snaps = store.get_snapshots(product_id, last_n=window)
    if not snaps:
        recs.append({
            "title": "모니터링 데이터 수집 시작",
            "description": "아직 스냅샷 데이터가 없습니다. 모니터링 도구를 실행하여 데이터를 축적하세요.",
            "priority": "high",
            "category": "setup",
        })
        return recs

    latest = snaps[-1].metrics
    trends = store.get_trends(product_id, window=window)
    trend_map = {t.metric: t for t in trends}

    # 이탈률 높으면
    bounce = latest.get("bounce_rate", 0)
    if bounce > 60:
        recs.append({
            "title": "랜딩 페이지 최적화",
            "description": (
                f"이탈률 {bounce:.1f}%로 높습니다. "
                "CTA 명확화, 로딩 속도 개선, 모바일 UX 점검을 권장합니다."
            ),
            "priority": "high" if bounce > 80 else "medium",
            "category": "ux",
        })

    # 환불율 높으면
    refund = latest.get("refund_rate", 0)
    if refund > 3:
        recs.append({
            "title": "환불 원인 분석 필요",
            "description": (
                f"환불율 {refund:.1f}%입니다. "
                "고객 피드백 수집, 온보딩 개선, 기대치 관리를 권장합니다."
            ),
            "priority": "high" if refund > 7 else "medium",
            "category": "business",
        })

    # 전환율 하락 추세
    conv_trend = trend_map.get("conversion_rate")
    if conv_trend and conv_trend.direction == "down" and abs(conv_trend.change_pct) > 15:
        recs.append({
            "title": "전환율 하락 대응",
            "description": (
                f"전환율이 평균 대비 {abs(conv_trend.change_pct):.1f}% 하락 중입니다. "
                "가격 정책, 퍼널 UX, A/B 테스트를 검토하세요."
            ),
            "priority": "high",
            "category": "growth",
        })

    # 비용 증가 추세
    cost_trend = trend_map.get("monthly_pct")
    if cost_trend and cost_trend.direction == "up" and cost_trend.change_pct > 20:
        recs.append({
            "title": "인프라 비용 증가 대응",
            "description": (
                f"비용이 평균 대비 {cost_trend.change_pct:.1f}% 증가 중입니다. "
                "Prompt Caching, CDN 최적화, DB 쿼리 효율화를 검토하세요."
            ),
            "priority": "medium",
            "category": "cost",
        })

    # 데이터 축적 충분하면 안정성 평가
    if len(snaps) >= 14:
        high_vol_metrics = [
            t for t in trends if t.volatility > 30
        ]
        if high_vol_metrics:
            names = ", ".join(_metric_label(t.metric) for t in high_vol_metrics[:3])
            recs.append({
                "title": "지표 변동성 안정화 필요",
                "description": (
                    f"변동성 높은 지표: {names}. "
                    "원인 분석 후 안정화 조치를 권장합니다."
                ),
                "priority": "medium",
                "category": "stability",
            })

    # 스냅샷 부족 경고
    if len(snaps) < 7:
        recs.append({
            "title": "데이터 축적 진행 중",
            "description": (
                f"현재 {len(snaps)}일치 데이터 보유. "
                "7일 이상 축적되면 추세 분석 정확도가 향상됩니다."
            ),
            "priority": "low",
            "category": "setup",
        })

    return recs


def _metric_label(metric: str) -> str:
    """지표 이름 → 한글 레이블"""
    labels: dict[str, str] = {
        "bounce_rate": "이탈률",
        "refund_rate": "환불율",
        "failure_rate": "결제 실패율",
        "churn_rate": "구독 이탈률",
        "conversion_rate": "전환율",
        "monthly_pct": "월 비용 사용률",
        "engagement_rate": "참여율",
        "sessions": "세션 수",
        "active_users": "활성 사용자",
        "daily_cost": "일 비용",
        "monthly_cost": "월 비용",
    }
    return labels.get(metric, metric)
