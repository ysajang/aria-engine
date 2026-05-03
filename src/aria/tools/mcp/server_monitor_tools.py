"""ARIA Engine - MCP Tool: 서버 자동 모니터링

ToolExecutor 4종 — 에이전트가 온디맨드로 호출 가능
- ServerHealthcheckTool: HTTP 헬스체크 (응답시간 / 상태코드 / SSL 만료)
- ServerErrorLogTool: 에러 로그 분석 (패턴 집계 / 심각도 판정)
- ServerTrafficTool: 트래픽 이상 감지 (이동평균 대비 스파이크)
- ServerSecurityScanTool: 보안 취약점 스캔 (헤더 / 포트 / SSL)

인증: 불필요 (ARIA 자체 모니터링)
설계: monitoring/checks.py 핵심 로직 재사용 + ToolResult 래핑

cron 스크립트(scripts/aria_monitor.py)도 동일한 checks.py 사용
→ 일관된 결과 보장
"""

from __future__ import annotations

from typing import Any

import structlog

from aria.monitoring.checks import (
    analyze_error_logs,
    check_traffic_anomaly,
    run_healthcheck,
    run_healthcheck_batch,
    run_security_scan,
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


class ServerHealthcheckTool(ToolExecutor):
    """HTTP 헬스체크 도구

    단일 URL 또는 여러 URL의 상태를 동시에 체크
    응답시간 / HTTP 상태코드 / SSL 인증서 만료일 확인
    """

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="server_healthcheck",
            description=(
                "서버 헬스체크를 수행합니다. "
                "HTTP 응답시간, 상태코드, SSL 인증서 만료일을 확인합니다. "
                "하나의 URL 또는 쉼표로 구분된 여러 URL을 동시에 체크할 수 있습니다. "
                "Testorum, Talksim 등 운영 서비스의 가용성을 모니터링할 때 사용합니다."
            ),
            parameters=[
                ToolParameter(
                    name="urls",
                    type="string",
                    description=(
                        "체크할 URL (쉼표 구분 가능. "
                        "예: 'https://testorum.app' 또는 "
                        "'https://testorum.app,https://talksim.app')"
                    ),
                    required=True,
                ),
                ToolParameter(
                    name="timeout",
                    type="number",
                    description="요청 타임아웃 초 (기본값 10)",
                    required=False,
                    default=10.0,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        urls_raw = parameters.get("urls", "").strip()
        if not urls_raw:
            return ToolResult(
                tool_name="server_healthcheck",
                success=False,
                error="urls가 비어있습니다",
            )

        timeout = float(parameters.get("timeout", 10.0))
        urls = [u.strip() for u in urls_raw.split(",") if u.strip()]

        if not urls:
            return ToolResult(
                tool_name="server_healthcheck",
                success=False,
                error="유효한 URL이 없습니다",
            )

        urls = [
            u if u.startswith(("http://", "https://")) else f"https://{u}"
            for u in urls
        ]

        try:
            if len(urls) == 1:
                result = await run_healthcheck(urls[0], timeout=timeout)
                results = [result]
            else:
                results = await run_healthcheck_batch(urls, timeout=timeout)

            healthy = sum(1 for r in results if r.get("status") == "healthy")
            total = len(results)

            logger.info("server_healthcheck_complete", total=total, healthy=healthy)

            return ToolResult(
                tool_name="server_healthcheck",
                success=True,
                output={"summary": f"{healthy}/{total} 정상", "results": results},
            )

        except Exception as e:
            error_str = str(e)[:300]
            logger.error("server_healthcheck_failed", error=error_str)
            return ToolResult(tool_name="server_healthcheck", success=False, error=f"헬스체크 실패: {error_str}")


class ServerErrorLogTool(ToolExecutor):
    """에러 로그 분석 도구"""

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="server_error_log_analyze",
            description=(
                "서버 로그 파일에서 에러를 분석합니다. "
                "404/500 에러 빈도, 에러 패턴 집계, 심각도 판정을 수행합니다. "
                "structlog JSON / nginx access log / 일반 텍스트 로그를 지원합니다."
            ),
            parameters=[
                ToolParameter(name="log_path", type="string", description="로그 파일 경로", required=True),
                ToolParameter(name="minutes", type="integer", description="분석할 최근 N분 (기본값 30)", required=False, default=30),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        log_path = parameters.get("log_path", "").strip()
        if not log_path:
            return ToolResult(tool_name="server_error_log_analyze", success=False, error="log_path가 비어있습니다")

        minutes = int(parameters.get("minutes", 30))
        minutes = max(1, min(minutes, 1440))

        try:
            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: analyze_error_logs(log_path, minutes=minutes))

            if result.get("error"):
                return ToolResult(tool_name="server_error_log_analyze", success=False, error=result["error"])

            logger.info("server_error_log_analyzed", log_path=log_path, error_count=result["error_count"], severity=result["severity"])
            return ToolResult(tool_name="server_error_log_analyze", success=True, output=result)

        except Exception as e:
            return ToolResult(tool_name="server_error_log_analyze", success=False, error=f"로그 분석 실패: {str(e)[:300]}")


class ServerTrafficTool(ToolExecutor):
    """트래픽 이상 감지 도구"""

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="server_traffic_check",
            description=(
                "트래픽 이상을 감지합니다. "
                "최근 요청량을 과거 평균과 비교하여 스파이크를 탐지합니다. "
                "DDoS 공격, 봇 크롤링, 비정상 트래픽 패턴을 식별합니다."
            ),
            parameters=[
                ToolParameter(name="log_path", type="string", description="로그 파일 경로", required=True),
                ToolParameter(name="window_minutes", type="integer", description="현재 윈도우 크기 분 (기본값 15)", required=False, default=15),
                ToolParameter(name="anomaly_threshold", type="number", description="이상 판정 배수 (기본값 3.0)", required=False, default=3.0),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        log_path = parameters.get("log_path", "").strip()
        if not log_path:
            return ToolResult(tool_name="server_traffic_check", success=False, error="log_path가 비어있습니다")

        window_minutes = int(parameters.get("window_minutes", 15))
        anomaly_threshold = float(parameters.get("anomaly_threshold", 3.0))

        try:
            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: check_traffic_anomaly(log_path, window_minutes=window_minutes, anomaly_threshold=anomaly_threshold)
            )

            if result.get("error"):
                return ToolResult(tool_name="server_traffic_check", success=False, error=result["error"])

            logger.info("server_traffic_checked", log_path=log_path, current_rpm=result["current_rpm"], anomaly=result["anomaly_detected"])
            return ToolResult(tool_name="server_traffic_check", success=True, output=result)

        except Exception as e:
            return ToolResult(tool_name="server_traffic_check", success=False, error=f"트래픽 분석 실패: {str(e)[:300]}")


class ServerSecurityScanTool(ToolExecutor):
    """보안 취약점 스캔 도구"""

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="server_security_scan",
            description=(
                "서버 보안 취약점을 스캔합니다. "
                "HTTP 보안 헤더(HSTS/CSP/X-Frame-Options 등), 위험 포트 노출, "
                "SSL/TLS 설정, 서버 정보 노출 여부를 확인합니다."
            ),
            parameters=[
                ToolParameter(name="url", type="string", description="스캔 대상 URL", required=True),
                ToolParameter(name="ports", type="string", description="체크할 포트 목록 (쉼표 구분)", required=False),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        url = parameters.get("url", "").strip()
        if not url:
            return ToolResult(tool_name="server_security_scan", success=False, error="url이 비어있습니다")

        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        ports_str = parameters.get("ports", "")
        ports: list[int] | None = None
        if ports_str:
            try:
                ports = [int(p.strip()) for p in ports_str.split(",") if p.strip()]
            except ValueError:
                return ToolResult(tool_name="server_security_scan", success=False, error="포트 번호가 올바르지 않습니다")

        try:
            result = await run_security_scan(url, ports=ports)
            logger.info("server_security_scan_complete", url=url, score=f"{result['headers_score']}/{result['headers_max_score']}", issues=len(result["issues"]))
            return ToolResult(tool_name="server_security_scan", success=True, output=result)

        except Exception as e:
            return ToolResult(tool_name="server_security_scan", success=False, error=f"보안 스캔 실패: {str(e)[:300]}")
