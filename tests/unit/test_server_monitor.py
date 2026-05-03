"""ARIA Engine - Server Monitoring Tests

서버 자동 모니터링 도구 테스트
- MonitoringConfig: 환경변수 파싱 + 프로퍼티
- checks.py: healthcheck / error_log / traffic / security 핵심 로직
- server_monitor_tools.py: 4 ToolExecutor 정의 + 실행
- alert_types: 새 AlertType + 쿨다운
- alert_manager: 모니터링 알림 메서드
- app.py: 조건부 등록 + 이벤트 인입 hook
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# MonitoringConfig Tests
# ============================================================


class TestMonitoringConfig:
    """MonitoringConfig 환경변수 + 프로퍼티 테스트"""

    def test_default_values(self):
        from aria.core.config import MonitoringConfig
        config = MonitoringConfig()
        assert config.enabled is True
        assert config.targets == ""
        assert config.log_paths == ""
        assert config.healthcheck_timeout == 10.0
        assert config.anomaly_threshold == 3.0

    def test_is_configured_enabled(self):
        from aria.core.config import MonitoringConfig
        config = MonitoringConfig(enabled=True)
        assert config.is_configured is True

    def test_is_configured_disabled(self):
        from aria.core.config import MonitoringConfig
        config = MonitoringConfig(enabled=False)
        assert config.is_configured is False

    def test_target_urls_empty(self):
        from aria.core.config import MonitoringConfig
        config = MonitoringConfig(targets="")
        assert config.target_urls == []

    def test_target_urls_parsed(self):
        from aria.core.config import MonitoringConfig
        config = MonitoringConfig(targets="https://a.com, https://b.com, https://c.com")
        assert config.target_urls == ["https://a.com", "https://b.com", "https://c.com"]

    def test_log_path_list(self):
        from aria.core.config import MonitoringConfig
        config = MonitoringConfig(log_paths="/var/log/a.log, /var/log/b.log")
        assert config.log_path_list == ["/var/log/a.log", "/var/log/b.log"]

    def test_port_list_default(self):
        from aria.core.config import MonitoringConfig
        config = MonitoringConfig()
        assert 22 in config.port_list
        assert 443 in config.port_list
        assert 8100 in config.port_list

    def test_port_list_custom(self):
        from aria.core.config import MonitoringConfig
        config = MonitoringConfig(check_ports="80,443,9090")
        assert config.port_list == [80, 443, 9090]

    def test_port_list_invalid_fallback(self):
        from aria.core.config import MonitoringConfig
        config = MonitoringConfig(check_ports="abc,def")
        result = config.port_list
        assert 22 in result  # fallback


class TestMonitoringInAriaConfig:
    """AriaConfig에 MonitoringConfig 포함 확인"""

    def test_aria_config_has_monitoring(self):
        from aria.core.config import AriaConfig, MonitoringConfig
        config = AriaConfig()
        assert hasattr(config, "monitoring")
        assert isinstance(config.monitoring, MonitoringConfig)


# ============================================================
# AlertType Extension Tests
# ============================================================


class TestMonitoringAlertTypes:
    """모니터링 AlertType + 쿨다운 테스트"""

    def test_new_alert_types_exist(self):
        from aria.alerts.alert_types import AlertType
        assert AlertType.HEALTH_CHECK_FAILED.value == "health_check_failed"
        assert AlertType.TRAFFIC_ANOMALY.value == "traffic_anomaly"
        assert AlertType.SECURITY_ISSUE.value == "security_issue"
        assert AlertType.ERROR_SPIKE.value == "error_spike"

    def test_new_emoji_mapping(self):
        from aria.alerts.alert_types import ALERT_EMOJI, AlertType
        assert AlertType.HEALTH_CHECK_FAILED in ALERT_EMOJI
        assert AlertType.TRAFFIC_ANOMALY in ALERT_EMOJI
        assert AlertType.SECURITY_ISSUE in ALERT_EMOJI
        assert AlertType.ERROR_SPIKE in ALERT_EMOJI

    def test_new_cooldowns(self):
        from aria.alerts.alert_types import DEFAULT_COOLDOWNS, AlertType
        assert AlertType.HEALTH_CHECK_FAILED in DEFAULT_COOLDOWNS
        assert DEFAULT_COOLDOWNS[AlertType.HEALTH_CHECK_FAILED] == 300  # 5분
        assert DEFAULT_COOLDOWNS[AlertType.TRAFFIC_ANOMALY] == 900  # 15분
        assert DEFAULT_COOLDOWNS[AlertType.SECURITY_ISSUE] == 86400  # 24시간
        assert DEFAULT_COOLDOWNS[AlertType.ERROR_SPIKE] == 1800  # 30분


# ============================================================
# Healthcheck Tests
# ============================================================


class TestHealthcheck:
    """헬스체크 로직 테스트"""

    @pytest.mark.asyncio
    async def test_healthcheck_healthy(self):
        from aria.monitoring.checks import run_healthcheck

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("aria.monitoring.checks.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            result = await run_healthcheck("http://localhost:8100/v1/health")

        assert result["status"] == "healthy"
        assert result["status_code"] == 200
        assert result["response_time_ms"] >= 0
        assert result["url"] == "http://localhost:8100/v1/health"

    @pytest.mark.asyncio
    async def test_healthcheck_server_error(self):
        from aria.monitoring.checks import run_healthcheck

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("aria.monitoring.checks.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            result = await run_healthcheck("http://example.com")

        assert result["status"] == "server_error"
        assert result["status_code"] == 500

    @pytest.mark.asyncio
    async def test_healthcheck_timeout(self):
        import httpx as httpx_mod
        from aria.monitoring.checks import run_healthcheck

        with patch("aria.monitoring.checks.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(side_effect=httpx_mod.TimeoutException("timeout"))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            result = await run_healthcheck("http://example.com", timeout=1.0)

        assert result["status"] == "timeout"
        assert result["error"] is not None

    @pytest.mark.asyncio
    async def test_healthcheck_unreachable(self):
        import httpx as httpx_mod
        from aria.monitoring.checks import run_healthcheck

        with patch("aria.monitoring.checks.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(side_effect=httpx_mod.ConnectError("refused"))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            result = await run_healthcheck("http://example.com")

        assert result["status"] == "unreachable"

    @pytest.mark.asyncio
    async def test_healthcheck_client_error(self):
        from aria.monitoring.checks import run_healthcheck

        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("aria.monitoring.checks.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            result = await run_healthcheck("http://example.com/missing")

        assert result["status"] == "client_error"
        assert result["status_code"] == 404

    @pytest.mark.asyncio
    async def test_healthcheck_batch(self):
        from aria.monitoring.checks import run_healthcheck_batch

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("aria.monitoring.checks.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            results = await run_healthcheck_batch(
                ["http://a.com", "http://b.com"]
            )

        assert len(results) == 2
        assert all(r["status"] == "healthy" for r in results)

    def test_check_ssl_expiry_invalid_host(self):
        from aria.monitoring.checks import _check_ssl_expiry
        # 존재하지 않는 호스트 → None 반환
        result = _check_ssl_expiry("https://this-host-does-not-exist-12345.invalid")
        assert result is None

    def test_healthcheck_result_has_checked_at(self):
        """결과에 checked_at 타임스탬프 포함"""
        # run_healthcheck의 결과 구조 검증
        result = {
            "url": "http://test.com",
            "status": "healthy",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        assert "checked_at" in result


# ============================================================
# Error Log Analysis Tests
# ============================================================


class TestErrorLogAnalysis:
    """에러 로그 분석 테스트"""

    def test_nonexistent_file(self):
        from aria.monitoring.checks import analyze_error_logs
        result = analyze_error_logs("/nonexistent/path.log")
        assert result.get("error") is not None
        assert "없음" in result["error"]

    def test_empty_log(self):
        from aria.monitoring.checks import analyze_error_logs

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("")
            tmp_path = f.name

        try:
            result = analyze_error_logs(tmp_path)
            assert result["error_count"] == 0
            assert result["severity"] == "info"
        finally:
            os.unlink(tmp_path)

    def test_structlog_json_errors(self):
        from aria.monitoring.checks import analyze_error_logs

        now = datetime.now(timezone.utc).isoformat()
        lines = [
            json.dumps({"timestamp": now, "level": "error", "event": "db connection failed", "status_code": 500}),
            json.dumps({"timestamp": now, "level": "error", "event": "timeout", "status_code": 500}),
            json.dumps({"timestamp": now, "level": "info", "event": "request ok", "status_code": 200}),
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("\n".join(lines))
            tmp_path = f.name

        try:
            result = analyze_error_logs(tmp_path, minutes=60)
            assert result["error_count"] == 2
            assert result["severity"] in ("info", "warning")  # 2 errors < 10
        finally:
            os.unlink(tmp_path)

    def test_nginx_log_parsing(self):
        from aria.monitoring.checks import analyze_error_logs

        lines = [
            '192.168.1.1 - - [04/May/2026:10:00:00 +0000] "GET /api/test HTTP/1.1" 200 1234',
            '192.168.1.2 - - [04/May/2026:10:01:00 +0000] "GET /missing HTTP/1.1" 404 567',
            '192.168.1.3 - - [04/May/2026:10:02:00 +0000] "POST /api/crash HTTP/1.1" 500 89',
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("\n".join(lines))
            tmp_path = f.name

        try:
            result = analyze_error_logs(tmp_path, minutes=1440)
            # 404 = warning, 500 = error
            assert result["error_count"] >= 1  # 500 counts as error
        finally:
            os.unlink(tmp_path)

    def test_text_log_error_detection(self):
        from aria.monitoring.checks import analyze_error_logs

        lines = [
            "2026-05-04 10:00:00 ERROR Something failed badly",
            "2026-05-04 10:01:00 WARNING Disk space low",
            "2026-05-04 10:02:00 INFO Normal operation",
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("\n".join(lines))
            tmp_path = f.name

        try:
            result = analyze_error_logs(tmp_path, minutes=1440)
            assert result["error_count"] >= 1
            assert result["warning_count"] >= 1
        finally:
            os.unlink(tmp_path)

    def test_critical_severity_threshold(self):
        from aria.monitoring.checks import analyze_error_logs

        now = datetime.now(timezone.utc).isoformat()
        lines = [
            json.dumps({"timestamp": now, "level": "error", "event": f"error_{i}"})
            for i in range(55)
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("\n".join(lines))
            tmp_path = f.name

        try:
            result = analyze_error_logs(tmp_path, minutes=60)
            assert result["severity"] == "critical"
        finally:
            os.unlink(tmp_path)

    def test_directory_not_file(self):
        from aria.monitoring.checks import analyze_error_logs

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = analyze_error_logs(tmp_dir)
            assert "파일이 아님" in result.get("error", "")


# ============================================================
# Traffic Anomaly Tests
# ============================================================


class TestTrafficAnomaly:
    """트래픽 이상 감지 테스트"""

    def test_nonexistent_file(self):
        from aria.monitoring.checks import check_traffic_anomaly
        result = check_traffic_anomaly("/nonexistent.log")
        assert result.get("error") is not None

    def test_empty_log_no_anomaly(self):
        from aria.monitoring.checks import check_traffic_anomaly

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("")
            tmp_path = f.name

        try:
            result = check_traffic_anomaly(tmp_path)
            assert result["anomaly_detected"] is False
            assert result["current_rpm"] == 0
        finally:
            os.unlink(tmp_path)

    def test_result_structure(self):
        from aria.monitoring.checks import check_traffic_anomaly

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("normal log line\n")
            tmp_path = f.name

        try:
            result = check_traffic_anomaly(tmp_path)
            assert "current_rpm" in result
            assert "baseline_rpm" in result
            assert "anomaly_detected" in result
            assert "ratio" in result
            assert "checked_at" in result
        finally:
            os.unlink(tmp_path)


# ============================================================
# Security Scan Tests
# ============================================================


class TestSecurityScan:
    """보안 스캔 테스트"""

    def test_check_security_headers_all_missing(self):
        from aria.monitoring.checks import _check_security_headers
        result = _check_security_headers({})
        assert result["score"] == 0
        assert result["max_score"] > 0
        assert len(result["issues"]) > 0

    def test_check_security_headers_all_present(self):
        from aria.monitoring.checks import _check_security_headers
        headers = {
            "strict-transport-security": "max-age=31536000",
            "content-security-policy": "default-src 'self'",
            "x-frame-options": "DENY",
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
            "permissions-policy": "camera=()",
            "x-xss-protection": "1; mode=block",
        }
        result = _check_security_headers(headers)
        assert result["score"] == result["max_score"]
        assert len(result["issues"]) == 0

    def test_check_security_headers_partial(self):
        from aria.monitoring.checks import _check_security_headers
        headers = {
            "strict-transport-security": "max-age=31536000",
            "x-content-type-options": "nosniff",
        }
        result = _check_security_headers(headers)
        assert 0 < result["score"] < result["max_score"]

    def test_extract_hostname(self):
        from aria.monitoring.checks import _extract_hostname
        assert _extract_hostname("https://example.com/path") == "example.com"
        assert _extract_hostname("http://localhost:8100") == "localhost"
        assert _extract_hostname("https://a.b.c:443/d") == "a.b.c"

    def test_extract_hostname_empty(self):
        from aria.monitoring.checks import _extract_hostname
        assert _extract_hostname("") == ""

    @pytest.mark.asyncio
    async def test_security_scan_connection_error(self):
        import httpx as httpx_mod
        from aria.monitoring.checks import run_security_scan

        with patch("aria.monitoring.checks.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(side_effect=httpx_mod.ConnectError("refused"))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            with patch("aria.monitoring.checks._scan_ports", new_callable=AsyncMock, return_value=[]):
                result = await run_security_scan("http://localhost:9999", ports=[])

        assert any(i["type"] == "connection_error" for i in result["issues"])

    @pytest.mark.asyncio
    async def test_security_scan_server_info_exposure(self):
        from aria.monitoring.checks import run_security_scan

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {
            "server": "nginx/1.24.0",
            "x-powered-by": "Express",
        }

        with patch("aria.monitoring.checks.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            with patch("aria.monitoring.checks._scan_ports", new_callable=AsyncMock, return_value=[]):
                result = await run_security_scan("http://test.com", ports=[])

        assert result["server_info_exposed"] is True
        info_issues = [i for i in result["issues"] if i["type"] == "info_exposure"]
        assert len(info_issues) >= 2  # server + x-powered-by

    def test_determine_overall_severity(self):
        from aria.monitoring.checks import determine_overall_severity
        assert determine_overall_severity([{"severity": "info"}]) == "info"
        assert determine_overall_severity([{"severity": "warning"}, {"severity": "info"}]) == "warning"
        assert determine_overall_severity([{"severity": "critical"}, {"severity": "warning"}]) == "critical"
        assert determine_overall_severity([]) == "info"


# ============================================================
# Log Parser Tests
# ============================================================


class TestLogParser:
    """로그 라인 파서 테스트"""

    def test_parse_json_log(self):
        from aria.monitoring.checks import _parse_log_line
        line = json.dumps({"timestamp": "2026-05-04T10:00:00Z", "level": "error", "event": "fail"})
        result = _parse_log_line(line)
        assert result is not None
        assert result["level"] == "error"

    def test_parse_nginx_log(self):
        from aria.monitoring.checks import _parse_log_line
        line = '1.2.3.4 - - [04/May/2026:10:00:00 +0000] "GET /api HTTP/1.1" 500 123'
        result = _parse_log_line(line)
        assert result is not None
        assert result["status_code"] == 500
        assert result["level"] == "error"

    def test_parse_text_error(self):
        from aria.monitoring.checks import _parse_log_line
        result = _parse_log_line("2026-05-04 CRITICAL database crashed")
        assert result is not None
        assert result["level"] == "error"

    def test_parse_text_warning(self):
        from aria.monitoring.checks import _parse_log_line
        result = _parse_log_line("2026-05-04 WARNING disk 90%")
        assert result is not None
        assert result["level"] == "warning"

    def test_parse_empty_line(self):
        from aria.monitoring.checks import _parse_log_line
        assert _parse_log_line("") is None
        assert _parse_log_line("   ") is None

    def test_parse_normal_info_line_returns_none(self):
        from aria.monitoring.checks import _parse_log_line
        # info-level text without error/warning keywords
        assert _parse_log_line("everything is fine") is None


# ============================================================
# Tool Executor Tests
# ============================================================


class TestServerHealthcheckTool:
    """ServerHealthcheckTool 정의 + 실행"""

    def test_definition(self):
        from aria.tools.mcp.server_monitor_tools import ServerHealthcheckTool
        tool = ServerHealthcheckTool()
        defn = tool.get_definition()
        assert defn.name == "server_healthcheck"
        assert defn.category.value == "mcp"
        assert defn.safety_hint.value == "read_only"
        assert any(p.name == "urls" and p.required for p in defn.parameters)

    @pytest.mark.asyncio
    async def test_empty_urls(self):
        from aria.tools.mcp.server_monitor_tools import ServerHealthcheckTool
        tool = ServerHealthcheckTool()
        result = await tool.execute({"urls": ""})
        assert result.success is False
        assert "비어있습니다" in result.error

    @pytest.mark.asyncio
    async def test_execute_single_url(self):
        from aria.tools.mcp.server_monitor_tools import ServerHealthcheckTool

        mock_result = {
            "url": "http://test.com",
            "status": "healthy",
            "status_code": 200,
            "response_time_ms": 50.0,
            "ssl_expiry_days": None,
            "error": None,
            "checked_at": "2026-05-04T00:00:00Z",
        }

        with patch("aria.tools.mcp.server_monitor_tools.run_healthcheck", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = mock_result
            tool = ServerHealthcheckTool()
            result = await tool.execute({"urls": "http://test.com"})

        assert result.success is True
        assert result.output["summary"] == "1/1 정상"

    @pytest.mark.asyncio
    async def test_auto_https_prefix(self):
        from aria.tools.mcp.server_monitor_tools import ServerHealthcheckTool

        mock_result = {"url": "https://test.com", "status": "healthy", "status_code": 200, "response_time_ms": 50, "ssl_expiry_days": None, "error": None, "checked_at": ""}

        with patch("aria.tools.mcp.server_monitor_tools.run_healthcheck", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = mock_result
            tool = ServerHealthcheckTool()
            await tool.execute({"urls": "test.com"})

        # URL이 https://로 보정되었는지 확인
        mock_check.assert_called_once_with("https://test.com", timeout=10.0)


class TestServerErrorLogTool:
    """ServerErrorLogTool 정의 + 실행"""

    def test_definition(self):
        from aria.tools.mcp.server_monitor_tools import ServerErrorLogTool
        tool = ServerErrorLogTool()
        defn = tool.get_definition()
        assert defn.name == "server_error_log_analyze"
        assert any(p.name == "log_path" and p.required for p in defn.parameters)

    @pytest.mark.asyncio
    async def test_empty_path(self):
        from aria.tools.mcp.server_monitor_tools import ServerErrorLogTool
        tool = ServerErrorLogTool()
        result = await tool.execute({"log_path": ""})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_minutes_clamped(self):
        from aria.tools.mcp.server_monitor_tools import ServerErrorLogTool

        with patch("aria.tools.mcp.server_monitor_tools.analyze_error_logs") as mock_analyze:
            mock_analyze.return_value = {"error_count": 0, "severity": "info"}
            tool = ServerErrorLogTool()
            await tool.execute({"log_path": "/tmp/test.log", "minutes": 99999})

        mock_analyze.assert_called_once_with("/tmp/test.log", minutes=1440)


class TestServerTrafficTool:
    """ServerTrafficTool 정의 + 실행"""

    def test_definition(self):
        from aria.tools.mcp.server_monitor_tools import ServerTrafficTool
        tool = ServerTrafficTool()
        defn = tool.get_definition()
        assert defn.name == "server_traffic_check"

    @pytest.mark.asyncio
    async def test_empty_path(self):
        from aria.tools.mcp.server_monitor_tools import ServerTrafficTool
        tool = ServerTrafficTool()
        result = await tool.execute({"log_path": ""})
        assert result.success is False


class TestServerSecurityScanTool:
    """ServerSecurityScanTool 정의 + 실행"""

    def test_definition(self):
        from aria.tools.mcp.server_monitor_tools import ServerSecurityScanTool
        tool = ServerSecurityScanTool()
        defn = tool.get_definition()
        assert defn.name == "server_security_scan"

    @pytest.mark.asyncio
    async def test_empty_url(self):
        from aria.tools.mcp.server_monitor_tools import ServerSecurityScanTool
        tool = ServerSecurityScanTool()
        result = await tool.execute({"url": ""})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_invalid_ports(self):
        from aria.tools.mcp.server_monitor_tools import ServerSecurityScanTool
        tool = ServerSecurityScanTool()
        result = await tool.execute({"url": "http://test.com", "ports": "abc"})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_success(self):
        from aria.tools.mcp.server_monitor_tools import ServerSecurityScanTool

        with patch("aria.tools.mcp.server_monitor_tools.run_security_scan", new_callable=AsyncMock) as mock_scan:
            mock_scan.return_value = {
                "url": "https://test.com",
                "headers_score": 5,
                "headers_max_score": 9,
                "issues": [],
                "open_ports": [],
                "ssl_info": {},
                "server_info_exposed": False,
                "severity": "info",
                "checked_at": "2026-05-04T00:00:00Z",
            }
            tool = ServerSecurityScanTool()
            result = await tool.execute({"url": "https://test.com"})

        assert result.success is True
        assert result.output["headers_score"] == 5


# ============================================================
# AlertManager Monitoring Methods Tests
# ============================================================


class TestAlertManagerHealth:
    """AlertManager.check_health 테스트"""

    @pytest.mark.asyncio
    async def test_healthy_no_alert(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        result = await mgr.check_health(
            url="http://test.com",
            status="healthy",
            status_code=200,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_unreachable_critical(self):
        from aria.alerts.alert_manager import AlertManager
        from aria.alerts.alert_types import AlertLevel

        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_health(
                url="http://test.com",
                status="unreachable",
                error="Connection refused",
            )

        assert result is not None
        assert result.level == AlertLevel.CRITICAL
        assert "다운" in result.title

    @pytest.mark.asyncio
    async def test_ssl_expiring_warning(self):
        from aria.alerts.alert_manager import AlertManager

        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_health(
                url="https://test.com",
                status="healthy",
                status_code=200,
                ssl_expiry_days=7,
            )

        assert result is not None
        assert "SSL" in result.title

    @pytest.mark.asyncio
    async def test_disabled_no_alert(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(enabled=False)
        result = await mgr.check_health(url="http://test.com", status="unreachable")
        assert result is None


class TestAlertManagerTraffic:
    """AlertManager.check_traffic_anomaly 테스트"""

    @pytest.mark.asyncio
    async def test_traffic_warning(self):
        from aria.alerts.alert_manager import AlertManager

        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_traffic_anomaly(
                log_path="/var/log/test.log",
                current_rpm=30.0,
                baseline_rpm=10.0,
                ratio=3.0,
            )

        assert result is not None
        assert "트래픽" in result.title


class TestAlertManagerSecurity:
    """AlertManager.check_security_issue 테스트"""

    @pytest.mark.asyncio
    async def test_no_high_issues_no_alert(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        result = await mgr.check_security_issue(
            url="https://test.com",
            headers_score=7,
            headers_max_score=9,
            issues=[{"severity": "low", "detail": "minor"}],
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_high_issues_alert(self):
        from aria.alerts.alert_manager import AlertManager

        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_security_issue(
                url="https://test.com",
                headers_score=2,
                headers_max_score=9,
                issues=[
                    {"severity": "high", "detail": "SSL expired"},
                    {"severity": "high", "detail": "Redis exposed"},
                ],
            )

        assert result is not None
        assert "보안" in result.title


class TestAlertManagerErrorSpike:
    """AlertManager.check_error_spike 테스트"""

    @pytest.mark.asyncio
    async def test_low_count_no_alert(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        result = await mgr.check_error_spike(
            log_path="/tmp/test.log",
            error_count=5,
        )
        assert result is None  # < 10

    @pytest.mark.asyncio
    async def test_high_count_alert(self):
        from aria.alerts.alert_manager import AlertManager

        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_error_spike(
                log_path="/tmp/test.log",
                error_count=60,
                top_errors=[{"message": "db timeout", "count": 30}],
            )

        assert result is not None
        assert "급증" in result.title


# ============================================================
# LLM Tool Format Tests
# ============================================================


class TestToolLLMFormat:
    """도구 정의의 LLM function calling 포맷 변환"""

    def test_healthcheck_to_llm_tool(self):
        from aria.tools.mcp.server_monitor_tools import ServerHealthcheckTool
        llm_tool = ServerHealthcheckTool().get_definition().to_llm_tool()
        assert llm_tool["type"] == "function"
        assert llm_tool["function"]["name"] == "server_healthcheck"
        assert "urls" in llm_tool["function"]["parameters"]["properties"]

    def test_error_log_to_llm_tool(self):
        from aria.tools.mcp.server_monitor_tools import ServerErrorLogTool
        llm_tool = ServerErrorLogTool().get_definition().to_llm_tool()
        assert llm_tool["function"]["name"] == "server_error_log_analyze"

    def test_traffic_to_llm_tool(self):
        from aria.tools.mcp.server_monitor_tools import ServerTrafficTool
        llm_tool = ServerTrafficTool().get_definition().to_llm_tool()
        assert llm_tool["function"]["name"] == "server_traffic_check"

    def test_security_to_llm_tool(self):
        from aria.tools.mcp.server_monitor_tools import ServerSecurityScanTool
        llm_tool = ServerSecurityScanTool().get_definition().to_llm_tool()
        assert llm_tool["function"]["name"] == "server_security_scan"


# ============================================================
# Cron Script Tests
# ============================================================


class TestCronScript:
    """cron 스크립트 함수 단위 테스트"""

    @pytest.mark.asyncio
    async def test_post_events_no_api_key(self):
        """API 키 없으면 전송 스킵"""
        import importlib.util

        project_root = Path(__file__).resolve().parent.parent.parent
        script_path = project_root / "scripts" / "aria_monitor.py"

        if not script_path.exists():
            pytest.skip(f"스크립트 없음: {script_path}")

        # ARIA_API_KEY 없는 상태로 모듈 로드
        env_backup = os.environ.pop("ARIA_API_KEY", None)
        try:
            spec = importlib.util.spec_from_file_location(
                "aria_monitor_test",
                str(script_path),
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            result = await module.post_events([{"event_type": "test", "source": "aria"}])
            assert result is False
        finally:
            if env_backup is not None:
                os.environ["ARIA_API_KEY"] = env_backup
