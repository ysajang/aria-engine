#!/usr/bin/env python3
"""ARIA Engine - Server Monitor (Cron Script)

독립 실행 모니터링 스크립트 — crontab에 등록하여 사용

사용법:
    python scripts/aria_monitor.py --check health
    python scripts/aria_monitor.py --check errors
    python scripts/aria_monitor.py --check traffic
    python scripts/aria_monitor.py --check security
    python scripts/aria_monitor.py --check all

환경변수:
    ARIA_API_URL          ARIA 서버 주소 (기본: http://localhost:8100)
    ARIA_API_KEY          ARIA API 인증 키
    ARIA_MONITOR_TARGETS  헬스체크 대상 URL (쉼표 구분)
    ARIA_MONITOR_LOG_PATHS  로그 파일 경로 (쉼표 구분)
    ARIA_MONITOR_CHECK_PORTS  스캔 포트 (쉼표 구분 / 기본: 22,80,443,8100)

crontab 예시 ($ARIA_HOME = ARIA Engine 프로젝트 루트):
    # 헬스체크 — 5분마다
    */5  * * * * $ARIA_HOME/.venv/bin/python $ARIA_HOME/scripts/aria_monitor.py --check health >> /tmp/aria-monitor.log 2>&1

    # 에러 로그 분석 — 30분마다
    */30 * * * * $ARIA_HOME/.venv/bin/python $ARIA_HOME/scripts/aria_monitor.py --check errors >> /tmp/aria-monitor.log 2>&1

    # 트래픽 이상 감지 — 15분마다
    */15 * * * * $ARIA_HOME/.venv/bin/python $ARIA_HOME/scripts/aria_monitor.py --check traffic >> /tmp/aria-monitor.log 2>&1

    # 보안 취약점 스캔 — 매일 06:00
    0    6 * * * $ARIA_HOME/.venv/bin/python $ARIA_HOME/scripts/aria_monitor.py --check security >> /tmp/aria-monitor.log 2>&1

플로우:
    cron → checks.py 실행 → /v1/events POST → AlertManager 자동 평가 → 텔레그램
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가 (cron 환경에서 import 가능하도록)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import httpx


# === 환경변수 ===

ARIA_API_URL = os.environ.get("ARIA_API_URL", "http://localhost:8100")
ARIA_API_KEY = os.environ.get("ARIA_API_KEY", "")
MONITOR_TARGETS = os.environ.get("ARIA_MONITOR_TARGETS", "")
MONITOR_LOG_PATHS = os.environ.get("ARIA_MONITOR_LOG_PATHS", "")
MONITOR_CHECK_PORTS = os.environ.get("ARIA_MONITOR_CHECK_PORTS", "22,80,443,3306,5432,6379,8100")
HEALTHCHECK_TIMEOUT = float(os.environ.get("ARIA_MONITOR_HEALTHCHECK_TIMEOUT", "10.0"))
ANOMALY_THRESHOLD = float(os.environ.get("ARIA_MONITOR_ANOMALY_THRESHOLD", "3.0"))


def _log(level: str, msg: str) -> None:
    """간단한 로깅 (cron 환경용)"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [{level.upper()}] {msg}", flush=True)


def _parse_list(s: str) -> list[str]:
    """쉼표 구분 문자열 → 리스트"""
    return [x.strip() for x in s.split(",") if x.strip()]


def _parse_ports(s: str) -> list[int]:
    """쉼표 구분 포트 문자열 → 정수 리스트"""
    try:
        return [int(p.strip()) for p in s.split(",") if p.strip()]
    except ValueError:
        return [22, 80, 443, 8100]


async def post_events(events: list[dict]) -> bool:
    """ARIA /v1/events 엔드포인트로 이벤트 전송

    Returns:
        성공 여부
    """
    if not events:
        return True

    if not ARIA_API_KEY:
        _log("warn", "ARIA_API_KEY 미설정 — 이벤트 전송 스킵")
        return False

    url = f"{ARIA_API_URL}/v1/events"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": ARIA_API_KEY,
    }
    payload = {"events": events}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code == 200:
            data = resp.json()
            _log("info", f"이벤트 {data.get('ingested', 0)}건 전송 완료")
            return True
        else:
            _log("error", f"이벤트 전송 실패: HTTP {resp.status_code} — {resp.text[:200]}")
            return False

    except httpx.ConnectError:
        _log("error", f"ARIA 서버 연결 실패: {ARIA_API_URL}")
        return False
    except Exception as e:
        _log("error", f"이벤트 전송 에러: {str(e)[:200]}")
        return False


# === 체크 실행 ===


async def run_health_check() -> list[dict]:
    """헬스체크 실행 → EventInput 리스트 반환"""
    from aria.monitoring.checks import run_healthcheck

    targets = _parse_list(MONITOR_TARGETS)
    if not targets:
        # 기본: ARIA 자체 헬스체크
        targets = [f"{ARIA_API_URL}/v1/health"]

    events = []
    for url in targets:
        _log("info", f"헬스체크: {url}")
        result = await run_healthcheck(url, timeout=HEALTHCHECK_TIMEOUT)

        severity = "info"
        if result["status"] != "healthy":
            severity = "error"
        elif result.get("ssl_expiry_days") is not None and result["ssl_expiry_days"] < 14:
            severity = "warning"

        events.append({
            "event_type": "health_check",
            "source": "aria",
            "severity": severity,
            "data": result,
        })

        status_icon = "✅" if result["status"] == "healthy" else "❌"
        _log("info", f"  {status_icon} {url} → {result['status']} ({result['response_time_ms']}ms)")

    return events


async def run_error_analysis() -> list[dict]:
    """에러 로그 분석 → EventInput 리스트 반환"""
    from aria.monitoring.checks import analyze_error_logs

    log_paths = _parse_list(MONITOR_LOG_PATHS)
    if not log_paths:
        _log("warn", "ARIA_MONITOR_LOG_PATHS 미설정 — 에러 분석 스킵")
        return []

    events = []
    for log_path in log_paths:
        _log("info", f"에러 분석: {log_path}")
        result = analyze_error_logs(log_path, minutes=30)

        if result.get("error"):
            _log("warn", f"  ⚠️ {result['error']}")
            continue

        severity = result.get("severity", "info")
        events.append({
            "event_type": "error_log_analysis",
            "source": "aria",
            "severity": severity,
            "data": result,
        })

        _log("info", f"  에러 {result['error_count']}건 / 경고 {result['warning_count']}건 (심각도: {severity})")

    return events


async def run_traffic_check() -> list[dict]:
    """트래픽 이상 감지 → EventInput 리스트 반환"""
    from aria.monitoring.checks import check_traffic_anomaly

    log_paths = _parse_list(MONITOR_LOG_PATHS)
    if not log_paths:
        _log("warn", "ARIA_MONITOR_LOG_PATHS 미설정 — 트래픽 분석 스킵")
        return []

    events = []
    for log_path in log_paths:
        _log("info", f"트래픽 분석: {log_path}")
        result = check_traffic_anomaly(
            log_path,
            window_minutes=15,
            anomaly_threshold=ANOMALY_THRESHOLD,
        )

        if result.get("error"):
            _log("warn", f"  ⚠️ {result['error']}")
            continue

        severity = result.get("severity", "info")
        events.append({
            "event_type": "traffic_analysis",
            "source": "aria",
            "severity": severity,
            "data": result,
        })

        anomaly_icon = "🚨" if result["anomaly_detected"] else "✅"
        _log("info", f"  {anomaly_icon} {result['current_rpm']:.1f} rpm (평균 {result['baseline_rpm']:.1f} / {result['ratio']:.1f}x)")

    return events


async def run_security_scan() -> list[dict]:
    """보안 스캔 → EventInput 리스트 반환"""
    from aria.monitoring.checks import run_security_scan

    targets = _parse_list(MONITOR_TARGETS)
    if not targets:
        _log("warn", "ARIA_MONITOR_TARGETS 미설정 — 보안 스캔 스킵")
        return []

    ports = _parse_ports(MONITOR_CHECK_PORTS)

    events = []
    for url in targets:
        # https:// 보정
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        _log("info", f"보안 스캔: {url}")
        result = await run_security_scan(url, ports=ports)

        severity = result.get("severity", "info")
        events.append({
            "event_type": "security_scan",
            "source": "aria",
            "severity": severity,
            "data": result,
        })

        issue_count = len(result.get("issues", []))
        high_count = sum(1 for i in result.get("issues", []) if i.get("severity") == "high")
        _log("info", f"  보안 점수: {result['headers_score']}/{result['headers_max_score']} / 이슈 {issue_count}건 (고위험 {high_count}건)")

    return events


# === 메인 ===


async def main(check_type: str) -> int:
    """메인 실행

    Args:
        check_type: health / errors / traffic / security / all

    Returns:
        종료 코드 (0=성공 / 1=실패)
    """
    _log("info", f"=== ARIA Monitor 시작: {check_type} ===")

    all_events: list[dict] = []

    if check_type in ("health", "all"):
        events = await run_health_check()
        all_events.extend(events)

    if check_type in ("errors", "all"):
        events = await run_error_analysis()
        all_events.extend(events)

    if check_type in ("traffic", "all"):
        events = await run_traffic_check()
        all_events.extend(events)

    if check_type in ("security", "all"):
        events = await run_security_scan()
        all_events.extend(events)

    if not all_events:
        _log("info", "이벤트 없음 — 종료")
        return 0

    # ARIA /v1/events로 전송
    success = await post_events(all_events)

    _log("info", f"=== ARIA Monitor 완료: {len(all_events)}건 {'전송 성공' if success else '전송 실패'} ===")
    return 0 if success else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ARIA Server Monitor — cron 스케줄러용 독립 실행 스크립트",
    )
    parser.add_argument(
        "--check",
        choices=["health", "errors", "traffic", "security", "all"],
        required=True,
        help="실행할 체크 유형",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(main(args.check))
    sys.exit(exit_code)
