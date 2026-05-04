"""ARIA Engine - User Behavior Monitoring Checks

GA4 Data API 기반 사용자 행동 분석 (ToolExecutor + cron 스크립트 공용)
- check_traffic_overview: 세션/사용자/이탈률/참여율 개요
- check_exit_pages: 이탈률 높은 페이지 감지
- check_conversion_rate: 전환율 변화 추적
- check_funnel_dropoff: 퍼널 단계별 이탈 분석
- run_behavior_audit: 위 4가지 통합 실행

인증: Google OAuth2 (기존 GoogleTokenManager 재사용)
API: GA4 Data API v1beta (POST runReport)

설계 원칙:
- Exit-Safe: GA4 API 읽기 전용 (데이터 수정 불가)
- ProductConfig.ga4_measurement_id 설정 시 자동 활성화
- 실제 API 호출에는 GA4 property_id(숫자) 필요
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

GA4_API_BASE = "https://analyticsdata.googleapis.com/v1beta"

# === Default Thresholds ===
DEFAULT_BOUNCE_RATE_WARNING = 70.0     # 이탈률 70% 이상 WARNING
DEFAULT_BOUNCE_RATE_CRITICAL = 85.0    # 이탈률 85% 이상 CRITICAL
DEFAULT_CONVERSION_DROP_WARNING = 20.0  # 전환율 20% 하락 WARNING
DEFAULT_CONVERSION_DROP_CRITICAL = 40.0 # 전환율 40% 하락 CRITICAL
DEFAULT_FUNNEL_DROP_WARNING = 50.0     # 퍼널 단계 이탈 50% 이상 WARNING
DEFAULT_FUNNEL_DROP_CRITICAL = 70.0    # 퍼널 단계 이탈 70% 이상 CRITICAL


async def _ga4_run_report(
    client: httpx.AsyncClient,
    property_id: str,
    access_token: str,
    dimensions: list[str],
    metrics: list[str],
    date_range_days: int = 7,
    limit: int = 20,
    order_by_metric: str | None = None,
    order_desc: bool = True,
) -> dict[str, Any]:
    """GA4 Data API runReport 호출

    Args:
        property_id: GA4 숫자 프로퍼티 ID (예: "123456789")
        access_token: OAuth2 access token
        dimensions: 차원 목록 (예: ["pagePath", "pageTitle"])
        metrics: 지표 목록 (예: ["sessions", "bounceRate"])
        date_range_days: 검색 기간 (일)
        limit: 결과 행 수 제한
        order_by_metric: 정렬 기준 지표
        order_desc: 내림차순 여부

    Returns:
        GA4 API 응답 (rows / dimensionHeaders / metricHeaders)
    """
    body: dict[str, Any] = {
        "dateRanges": [{"startDate": f"{date_range_days}daysAgo", "endDate": "today"}],
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": m} for m in metrics],
        "limit": str(limit),
    }

    if order_by_metric:
        body["orderBys"] = [{
            "metric": {"metricName": order_by_metric},
            "desc": order_desc,
        }]

    resp = await client.post(
        f"{GA4_API_BASE}/properties/{property_id}:runReport",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=body,
    )

    if resp.status_code == 200:
        return resp.json()
    elif resp.status_code == 403:
        return {"error": "GA4 API 권한 없음 (Analytics Data API 활성화 + OAuth scope 확인)", "status": 403}
    elif resp.status_code == 404:
        return {"error": f"GA4 프로퍼티 '{property_id}' 미발견", "status": 404}
    else:
        return {"error": f"GA4 API HTTP {resp.status_code}", "status": resp.status_code}


def _parse_rows(
    data: dict[str, Any],
) -> list[dict[str, str]]:
    """GA4 API 응답에서 행 데이터 파싱"""
    rows = data.get("rows", [])
    dim_headers = [h.get("name", "") for h in data.get("dimensionHeaders", [])]
    met_headers = [h.get("name", "") for h in data.get("metricHeaders", [])]

    parsed = []
    for row in rows:
        item: dict[str, str] = {}
        for i, dv in enumerate(row.get("dimensionValues", [])):
            if i < len(dim_headers):
                item[dim_headers[i]] = dv.get("value", "")
        for i, mv in enumerate(row.get("metricValues", [])):
            if i < len(met_headers):
                item[met_headers[i]] = mv.get("value", "")
        parsed.append(item)
    return parsed


# ============================================================
# 1. Traffic Overview — 세션/사용자/이탈률/참여율
# ============================================================


async def check_traffic_overview(
    property_id: str,
    access_token: str,
    period_days: int = 7,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """GA4 트래픽 개요

    Returns:
        {property_id, sessions, active_users, new_users,
         bounce_rate, engagement_rate, avg_session_duration,
         screen_page_views, issues, checked_at}
    """
    result: dict[str, Any] = {
        "property_id": property_id,
        "check_type": "traffic_overview",
        "sessions": 0,
        "active_users": 0,
        "new_users": 0,
        "bounce_rate": 0.0,
        "engagement_rate": 0.0,
        "avg_session_duration": 0.0,
        "screen_page_views": 0,
        "period_days": period_days,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await _ga4_run_report(
                client, property_id, access_token,
                dimensions=["date"],
                metrics=[
                    "sessions", "activeUsers", "newUsers",
                    "bounceRate", "engagementRate",
                    "averageSessionDuration", "screenPageViews",
                ],
                date_range_days=period_days,
                limit=1,
            )

            if "error" in data:
                result["issues"].append({
                    "type": "api_error",
                    "severity": "high",
                    "message": data["error"],
                })
                return result

            # 합산 값은 totals에서 가져오거나 단일 행에서
            totals = data.get("totals", [{}])
            if totals:
                met_headers = [h.get("name", "") for h in data.get("metricHeaders", [])]
                values = totals[0].get("metricValues", []) if isinstance(totals, list) else []
                for i, mv in enumerate(values):
                    if i < len(met_headers):
                        val = mv.get("value", "0")
                        name = met_headers[i]
                        try:
                            if name == "sessions":
                                result["sessions"] = int(val)
                            elif name == "activeUsers":
                                result["active_users"] = int(val)
                            elif name == "newUsers":
                                result["new_users"] = int(val)
                            elif name == "bounceRate":
                                result["bounce_rate"] = round(float(val) * 100, 2)
                            elif name == "engagementRate":
                                result["engagement_rate"] = round(float(val) * 100, 2)
                            elif name == "averageSessionDuration":
                                result["avg_session_duration"] = round(float(val), 1)
                            elif name == "screenPageViews":
                                result["screen_page_views"] = int(val)
                        except (ValueError, TypeError):
                            pass

            # 이탈률 높으면 이슈
            if result["bounce_rate"] >= DEFAULT_BOUNCE_RATE_CRITICAL:
                result["issues"].append({
                    "type": "high_bounce_rate",
                    "severity": "high",
                    "message": f"전체 이탈률 {result['bounce_rate']:.1f}% (CRITICAL 기준: {DEFAULT_BOUNCE_RATE_CRITICAL}%)",
                })
            elif result["bounce_rate"] >= DEFAULT_BOUNCE_RATE_WARNING:
                result["issues"].append({
                    "type": "high_bounce_rate",
                    "severity": "medium",
                    "message": f"전체 이탈률 {result['bounce_rate']:.1f}% (WARNING 기준: {DEFAULT_BOUNCE_RATE_WARNING}%)",
                })

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "medium",
            "message": f"GA4 API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error",
            "severity": "medium",
            "message": f"GA4 API 오류: {str(e)[:200]}",
        })

    return result


# ============================================================
# 2. Exit Pages — 이탈률 높은 페이지
# ============================================================


async def check_exit_pages(
    property_id: str,
    access_token: str,
    period_days: int = 7,
    min_sessions: int = 10,
    bounce_warning: float = DEFAULT_BOUNCE_RATE_WARNING,
    bounce_critical: float = DEFAULT_BOUNCE_RATE_CRITICAL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """이탈률 높은 페이지 감지

    Args:
        min_sessions: 최소 세션 수 (노이즈 제거)

    Returns:
        {property_id, exit_pages, total_pages, problem_pages, issues, checked_at}
    """
    result: dict[str, Any] = {
        "property_id": property_id,
        "check_type": "exit_pages",
        "exit_pages": [],
        "total_pages": 0,
        "problem_pages": 0,
        "period_days": period_days,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await _ga4_run_report(
                client, property_id, access_token,
                dimensions=["pagePath", "pageTitle"],
                metrics=["sessions", "bounceRate", "engagementRate"],
                date_range_days=period_days,
                limit=50,
                order_by_metric="bounceRate",
                order_desc=True,
            )

            if "error" in data:
                result["issues"].append({
                    "type": "api_error",
                    "severity": "high",
                    "message": data["error"],
                })
                return result

            rows = _parse_rows(data)
            result["total_pages"] = len(rows)

            for row in rows:
                try:
                    sessions = int(row.get("sessions", "0"))
                    bounce_rate = round(float(row.get("bounceRate", "0")) * 100, 1)
                except (ValueError, TypeError):
                    continue

                if sessions < min_sessions:
                    continue

                page_path = row.get("pagePath", "?")
                page_title = row.get("pageTitle", "")

                page_info = {
                    "path": page_path,
                    "title": page_title[:50],
                    "sessions": sessions,
                    "bounce_rate": bounce_rate,
                }
                result["exit_pages"].append(page_info)

                if bounce_rate >= bounce_critical:
                    result["problem_pages"] += 1
                    result["issues"].append({
                        "type": "high_exit_page",
                        "severity": "high",
                        "message": (
                            f"'{page_path}' 이탈률 {bounce_rate}% "
                            f"({sessions}세션)"
                        ),
                    })
                elif bounce_rate >= bounce_warning:
                    result["problem_pages"] += 1
                    result["issues"].append({
                        "type": "high_exit_page",
                        "severity": "medium",
                        "message": (
                            f"'{page_path}' 이탈률 {bounce_rate}% "
                            f"({sessions}세션)"
                        ),
                    })

            # 상위 10개만 유지
            result["exit_pages"] = result["exit_pages"][:10]

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "medium",
            "message": f"GA4 API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error",
            "severity": "medium",
            "message": f"GA4 API 오류: {str(e)[:200]}",
        })

    return result


# ============================================================
# 3. Conversion Rate — 전환율 변화 (현재 vs 이전 기간)
# ============================================================


async def check_conversion_rate(
    property_id: str,
    access_token: str,
    conversion_event: str = "purchase",
    period_days: int = 7,
    drop_warning: float = DEFAULT_CONVERSION_DROP_WARNING,
    drop_critical: float = DEFAULT_CONVERSION_DROP_CRITICAL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """전환율 변화 추적 (현재 기간 vs 이전 동일 기간)

    Args:
        conversion_event: GA4 전환 이벤트명 (기본: purchase)
        drop_warning: 전환율 하락 WARNING 기준 (%)
        drop_critical: 전환율 하락 CRITICAL 기준 (%)

    Returns:
        {property_id, current_sessions, current_conversions, current_rate,
         previous_sessions, previous_conversions, previous_rate,
         rate_change_pct, issues, checked_at}
    """
    result: dict[str, Any] = {
        "property_id": property_id,
        "check_type": "conversion_rate",
        "conversion_event": conversion_event,
        "current_sessions": 0,
        "current_conversions": 0,
        "current_rate": 0.0,
        "previous_sessions": 0,
        "previous_conversions": 0,
        "previous_rate": 0.0,
        "rate_change_pct": 0.0,
        "period_days": period_days,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 현재 + 이전 기간을 한번에 비교
            body: dict[str, Any] = {
                "dateRanges": [
                    {"startDate": f"{period_days}daysAgo", "endDate": "today", "name": "current"},
                    {"startDate": f"{period_days * 2}daysAgo", "endDate": f"{period_days + 1}daysAgo", "name": "previous"},
                ],
                "dimensions": [{"name": "dateRange"}],
                "metrics": [
                    {"name": "sessions"},
                    {"name": "conversions"},
                ],
                "dimensionFilter": {
                    "filter": {
                        "fieldName": "eventName",
                        "stringFilter": {"value": conversion_event},
                    },
                },
            }

            resp = await client.post(
                f"{GA4_API_BASE}/properties/{property_id}:runReport",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=body,
            )

            if resp.status_code != 200:
                result["issues"].append({
                    "type": "api_error",
                    "severity": "high",
                    "message": f"GA4 API HTTP {resp.status_code}",
                })
                return result

            data = resp.json()
            rows = _parse_rows(data)

            for row in rows:
                date_range = row.get("dateRange", "")
                sessions = int(row.get("sessions", "0"))
                conversions = int(row.get("conversions", "0"))
                rate = (conversions / sessions * 100) if sessions > 0 else 0.0

                if date_range == "date_range_0":  # current
                    result["current_sessions"] = sessions
                    result["current_conversions"] = conversions
                    result["current_rate"] = round(rate, 2)
                elif date_range == "date_range_1":  # previous
                    result["previous_sessions"] = sessions
                    result["previous_conversions"] = conversions
                    result["previous_rate"] = round(rate, 2)

            # 전환율 변화율 계산
            if result["previous_rate"] > 0:
                change = ((result["current_rate"] - result["previous_rate"]) / result["previous_rate"]) * 100
                result["rate_change_pct"] = round(change, 1)

                # 하락 감지
                if change <= -drop_critical:
                    result["issues"].append({
                        "type": "conversion_drop",
                        "severity": "high",
                        "message": (
                            f"전환율 {abs(change):.1f}% 하락 "
                            f"({result['previous_rate']:.2f}% → {result['current_rate']:.2f}% / "
                            f"이벤트: {conversion_event})"
                        ),
                    })
                elif change <= -drop_warning:
                    result["issues"].append({
                        "type": "conversion_drop",
                        "severity": "medium",
                        "message": (
                            f"전환율 {abs(change):.1f}% 하락 "
                            f"({result['previous_rate']:.2f}% → {result['current_rate']:.2f}% / "
                            f"이벤트: {conversion_event})"
                        ),
                    })

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "medium",
            "message": f"GA4 API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error",
            "severity": "medium",
            "message": f"GA4 API 오류: {str(e)[:200]}",
        })

    return result


# ============================================================
# 4. Funnel Dropoff — 퍼널 단계별 이탈
# ============================================================


async def check_funnel_dropoff(
    property_id: str,
    access_token: str,
    funnel_events: list[str] | None = None,
    period_days: int = 7,
    drop_warning: float = DEFAULT_FUNNEL_DROP_WARNING,
    drop_critical: float = DEFAULT_FUNNEL_DROP_CRITICAL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """퍼널 단계별 이탈 분석

    Args:
        funnel_events: 퍼널 이벤트 목록 (순서대로)
            기본: ["page_view", "scroll", "click", "purchase"]
        drop_warning: 단계별 이탈 WARNING 기준 (%)

    Returns:
        {property_id, funnel_steps, bottleneck_step, issues, checked_at}
    """
    if funnel_events is None:
        funnel_events = ["page_view", "scroll", "click", "purchase"]

    result: dict[str, Any] = {
        "property_id": property_id,
        "check_type": "funnel_dropoff",
        "funnel_steps": [],
        "bottleneck_step": None,
        "period_days": period_days,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 각 이벤트의 사용자 수 조회
            data = await _ga4_run_report(
                client, property_id, access_token,
                dimensions=["eventName"],
                metrics=["eventCount", "totalUsers"],
                date_range_days=period_days,
                limit=100,
            )

            if "error" in data:
                result["issues"].append({
                    "type": "api_error",
                    "severity": "high",
                    "message": data["error"],
                })
                return result

            rows = _parse_rows(data)

            # 이벤트명 → 사용자수 매핑
            event_users: dict[str, int] = {}
            for row in rows:
                name = row.get("eventName", "")
                users = int(row.get("totalUsers", "0"))
                event_users[name] = users

            # 퍼널 단계 구성
            max_dropoff = 0.0
            bottleneck = None
            prev_users = 0

            for i, event in enumerate(funnel_events):
                users = event_users.get(event, 0)
                dropoff = 0.0

                if i > 0 and prev_users > 0:
                    dropoff = round((1 - users / prev_users) * 100, 1)

                step = {
                    "step": i + 1,
                    "event": event,
                    "users": users,
                    "dropoff_pct": dropoff,
                }
                result["funnel_steps"].append(step)

                # 병목 감지 (첫 단계 제외)
                if i > 0 and dropoff > max_dropoff:
                    max_dropoff = dropoff
                    bottleneck = event

                if i > 0 and dropoff >= drop_critical:
                    result["issues"].append({
                        "type": "funnel_bottleneck",
                        "severity": "high",
                        "message": (
                            f"퍼널 병목: '{funnel_events[i-1]}' → '{event}' "
                            f"이탈 {dropoff:.1f}% ({prev_users} → {users}명)"
                        ),
                    })
                elif i > 0 and dropoff >= drop_warning:
                    result["issues"].append({
                        "type": "funnel_bottleneck",
                        "severity": "medium",
                        "message": (
                            f"퍼널 이탈: '{funnel_events[i-1]}' → '{event}' "
                            f"이탈 {dropoff:.1f}% ({prev_users} → {users}명)"
                        ),
                    })

                prev_users = users

            result["bottleneck_step"] = bottleneck

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "medium",
            "message": f"GA4 API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error",
            "severity": "medium",
            "message": f"GA4 API 오류: {str(e)[:200]}",
        })

    return result


# ============================================================
# 5. 통합 감사
# ============================================================


async def run_behavior_audit(
    property_id: str,
    access_token: str,
    conversion_event: str = "purchase",
    funnel_events: list[str] | None = None,
    period_days: int = 7,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """사용자 행동 종합 감사

    Args:
        property_id: GA4 숫자 프로퍼티 ID
        access_token: OAuth2 access token
        conversion_event: 전환 추적 이벤트명
        funnel_events: 퍼널 이벤트 목록

    Returns:
        {property_id, overview, exit_pages, conversion, funnel,
         total_issues, high_issues, elapsed_ms}
    """
    start = time.monotonic()

    audit: dict[str, Any] = {
        "property_id": property_id,
        "overview": {},
        "exit_pages": {},
        "conversion": {},
        "funnel": {},
        "total_issues": 0,
        "high_issues": 0,
        "all_issues": [],
        "elapsed_ms": 0,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    audit["overview"] = await check_traffic_overview(
        property_id, access_token, period_days, timeout,
    )
    audit["exit_pages"] = await check_exit_pages(
        property_id, access_token, period_days, timeout=timeout,
    )
    audit["conversion"] = await check_conversion_rate(
        property_id, access_token, conversion_event, period_days, timeout=timeout,
    )
    audit["funnel"] = await check_funnel_dropoff(
        property_id, access_token, funnel_events, period_days, timeout=timeout,
    )

    # 이슈 통합
    all_issues = (
        audit["overview"].get("issues", [])
        + audit["exit_pages"].get("issues", [])
        + audit["conversion"].get("issues", [])
        + audit["funnel"].get("issues", [])
    )
    audit["all_issues"] = all_issues
    audit["total_issues"] = len(all_issues)
    audit["high_issues"] = len([i for i in all_issues if i.get("severity") == "high"])
    audit["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)

    return audit
