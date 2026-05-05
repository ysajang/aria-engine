"""ARIA Engine - Frontend Error Analysis Checks

프론트엔드 JS 에러 이벤트 분석 로직 (ToolExecutor + cron 공용)
- analyze_frontend_errors: 이벤트 스토어에서 프론트엔드 에러 조회 + 패턴 분석
- detect_error_spike: 최근 N분 내 에러 급증 감지
- group_errors_by_pattern: 에러 메시지 그룹핑 (스택트레이스 기반)

설계 원칙:
- AI API 호출 0 — 순수 Python 패턴 매칭
- 이벤트 스토어 기반 (제품에서 POST /v1/events로 수집된 frontend_error 이벤트)
- 모든 함수는 dict 반환 → ToolResult.output / EventInput.data 양쪽 사용
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

import structlog

logger = structlog.get_logger()

# === Default Thresholds ===
DEFAULT_SPIKE_WINDOW_MINUTES = 10
DEFAULT_SPIKE_THRESHOLD = 10       # N분 내 이 수 이상이면 spike
DEFAULT_ANALYSIS_HOURS = 24        # 분석 기간 (기본 24시간)

# 무시할 에러 패턴 (브라우저 확장 / 광고 스크립트 등)
IGNORE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"chrome-extension://", re.IGNORECASE),
    re.compile(r"moz-extension://", re.IGNORECASE),
    re.compile(r"ResizeObserver loop", re.IGNORECASE),
    re.compile(r"Script error\.$", re.IGNORECASE),
    re.compile(r"__gCrWeb", re.IGNORECASE),
    re.compile(r"googletag", re.IGNORECASE),
    re.compile(r"adsbygoogle", re.IGNORECASE),
]


def _should_ignore(message: str) -> bool:
    """무시해야 할 에러 패턴인지 확인"""
    for pattern in IGNORE_PATTERNS:
        if pattern.search(message):
            return True
    return False


def _normalize_stack(stack: str) -> str:
    """스택트레이스 정규화 (라인 번호/컬럼 제거 → 패턴 그룹핑용)"""
    # webpack chunk hash 제거
    normalized = re.sub(r"-[a-f0-9]{8,}\.js", "-[hash].js", stack)
    # 라인:컬럼 번호 제거
    normalized = re.sub(r":\d+:\d+", ":*:*", normalized)
    # 쿼리스트링 제거
    normalized = re.sub(r"\?[^\s)]+", "", normalized)
    return normalized[:500]  # 그룹핑 키 길이 제한


def _extract_error_signature(error: dict[str, Any]) -> str:
    """에러의 그룹핑 시그니처 생성"""
    message = error.get("message", "unknown")
    stack = error.get("stack", "")
    url = error.get("url", "")

    if stack:
        first_frame = stack.split("\n")[0] if "\n" in stack else stack
        return f"{message}|{_normalize_stack(first_frame)}"

    # 스택 없으면 메시지 + URL 조합
    return f"{message}|{url}"


def analyze_frontend_errors(
    events: list[dict[str, Any]],
    hours: int = DEFAULT_ANALYSIS_HOURS,
) -> dict[str, Any]:
    """프론트엔드 에러 이벤트 분석

    Args:
        events: 이벤트 스토어에서 조회한 frontend_error 이벤트 목록
                각 이벤트: {event_type, data: {message, stack, url, browser, ...}, timestamp, ...}
        hours: 분석 기간 (시간)

    Returns:
        {total, filtered, groups, top_errors, top_urls, top_browsers, severity, issues}
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result: dict[str, Any] = {
        "period_hours": hours,
        "total_events": len(events),
        "filtered_events": 0,
        "ignored_events": 0,
        "groups": [],
        "top_errors": [],
        "top_urls": [],
        "top_browsers": [],
        "severity": "none",
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    # 필터링: 기간 + 무시 패턴
    valid_events: list[dict[str, Any]] = []
    ignored = 0

    for event in events:
        data = event.get("data", {})
        message = data.get("message", "")

        if _should_ignore(message):
            ignored += 1
            continue

        # 타임스탬프 필터 (문자열 비교)
        ts = event.get("timestamp", "")
        if ts and ts < cutoff.isoformat():
            continue

        valid_events.append(event)

    result["filtered_events"] = len(valid_events)
    result["ignored_events"] = ignored

    if not valid_events:
        return result

    # 에러 그룹핑
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    url_counter: Counter[str] = Counter()
    browser_counter: Counter[str] = Counter()

    for event in valid_events:
        data = event.get("data", {})
        sig = _extract_error_signature(data)
        groups[sig].append(event)

        url = data.get("url", "unknown")
        url_counter[url] += 1

        browser = data.get("browser", "unknown")
        browser_counter[browser] += 1

    # 그룹 정렬 (빈도 높은 순)
    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)

    group_summaries = []
    for sig, group_events in sorted_groups[:10]:
        sample = group_events[0].get("data", {})
        group_summaries.append({
            "signature": sig[:200],
            "count": len(group_events),
            "message": sample.get("message", "unknown")[:200],
            "url": sample.get("url", "unknown"),
            "first_seen": group_events[-1].get("timestamp", ""),
            "last_seen": group_events[0].get("timestamp", ""),
        })

    result["groups"] = group_summaries
    result["top_errors"] = [
        {"message": g["message"], "count": g["count"]}
        for g in group_summaries[:5]
    ]
    result["top_urls"] = [
        {"url": url, "count": count}
        for url, count in url_counter.most_common(5)
    ]
    result["top_browsers"] = [
        {"browser": browser, "count": count}
        for browser, count in browser_counter.most_common(5)
    ]

    # 심각도 판단
    total = len(valid_events)
    if total >= 50:
        result["severity"] = "high"
        result["issues"].append({
            "severity": "high",
            "message": f"프론트엔드 에러 {total}건 발생 ({hours}시간 내)",
        })
    elif total >= 20:
        result["severity"] = "medium"
        result["issues"].append({
            "severity": "medium",
            "message": f"프론트엔드 에러 {total}건 발생 ({hours}시간 내)",
        })
    elif total >= 5:
        result["severity"] = "low"

    # 단일 에러 반복 (전체의 50% 이상이 같은 에러)
    if sorted_groups and len(sorted_groups[0][1]) > total * 0.5:
        top_msg = sorted_groups[0][1][0].get("data", {}).get("message", "unknown")
        result["issues"].append({
            "severity": "medium",
            "message": f"단일 에러가 전체의 {len(sorted_groups[0][1])}/{total}건 차지: {top_msg[:100]}",
        })

    return result


def detect_error_spike(
    events: list[dict[str, Any]],
    window_minutes: int = DEFAULT_SPIKE_WINDOW_MINUTES,
    threshold: int = DEFAULT_SPIKE_THRESHOLD,
) -> dict[str, Any]:
    """최근 N분 내 프론트엔드 에러 스파이크 감지

    Returns:
        {is_spike, count, window_minutes, threshold, severity}
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    recent_count = 0

    for event in events:
        data = event.get("data", {})
        message = data.get("message", "")

        if _should_ignore(message):
            continue

        ts = event.get("timestamp", "")
        if ts and ts >= cutoff.isoformat():
            recent_count += 1

    is_spike = recent_count >= threshold
    severity = "none"
    if recent_count >= threshold * 3:
        severity = "high"
    elif recent_count >= threshold:
        severity = "medium"

    return {
        "is_spike": is_spike,
        "count": recent_count,
        "window_minutes": window_minutes,
        "threshold": threshold,
        "severity": severity,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
