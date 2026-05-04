"""ARIA Engine - User Behavior Monitor Tests (Phase 3.5 Step 7)

사용자 행동 분석 테스트
- behavior_checks.py 핵심 로직 (GA4 Data API mock)
- BehaviorAuditTool ToolExecutor
- AlertType / AlertManager 사용자 행동 알림

총 테스트: ~42개
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# 1. GA4 API Helper Tests
# ============================================================


def _mock_ga4_response(
    rows: list[dict] | None = None,
    dim_headers: list[str] | None = None,
    met_headers: list[str] | None = None,
    totals: list[dict] | None = None,
    status: int = 200,
    error: str | None = None,
) -> MagicMock:
    """GA4 API mock 응답 생성"""
    mock_resp = MagicMock()
    mock_resp.status_code = status

    body: dict[str, Any] = {}
    if dim_headers:
        body["dimensionHeaders"] = [{"name": h} for h in dim_headers]
    if met_headers:
        body["metricHeaders"] = [{"name": h} for h in met_headers]
    if rows:
        body["rows"] = rows
    if totals:
        body["totals"] = totals
    if error:
        body["error"] = error

    mock_resp.json.return_value = body
    mock_resp.text = json.dumps(body)
    return mock_resp


class TestParseRows:
    """_parse_rows 유틸리티"""

    def test_empty_data(self):
        from aria.monitoring.behavior_checks import _parse_rows
        assert _parse_rows({}) == []

    def test_single_row(self):
        from aria.monitoring.behavior_checks import _parse_rows
        data = {
            "dimensionHeaders": [{"name": "pagePath"}],
            "metricHeaders": [{"name": "sessions"}],
            "rows": [{
                "dimensionValues": [{"value": "/home"}],
                "metricValues": [{"value": "100"}],
            }],
        }
        result = _parse_rows(data)
        assert len(result) == 1
        assert result[0]["pagePath"] == "/home"
        assert result[0]["sessions"] == "100"

    def test_multiple_rows(self):
        from aria.monitoring.behavior_checks import _parse_rows
        data = {
            "dimensionHeaders": [{"name": "pagePath"}],
            "metricHeaders": [{"name": "sessions"}, {"name": "bounceRate"}],
            "rows": [
                {
                    "dimensionValues": [{"value": "/"}],
                    "metricValues": [{"value": "200"}, {"value": "0.45"}],
                },
                {
                    "dimensionValues": [{"value": "/about"}],
                    "metricValues": [{"value": "50"}, {"value": "0.80"}],
                },
            ],
        }
        result = _parse_rows(data)
        assert len(result) == 2
        assert result[1]["bounceRate"] == "0.80"


class TestDefaultThresholds:
    """기본 임계치"""

    def test_bounce_thresholds(self):
        from aria.monitoring.behavior_checks import (
            DEFAULT_BOUNCE_RATE_WARNING,
            DEFAULT_BOUNCE_RATE_CRITICAL,
        )
        assert DEFAULT_BOUNCE_RATE_WARNING == 70.0
        assert DEFAULT_BOUNCE_RATE_CRITICAL == 85.0

    def test_conversion_thresholds(self):
        from aria.monitoring.behavior_checks import (
            DEFAULT_CONVERSION_DROP_WARNING,
            DEFAULT_CONVERSION_DROP_CRITICAL,
        )
        assert DEFAULT_CONVERSION_DROP_WARNING == 20.0
        assert DEFAULT_CONVERSION_DROP_CRITICAL == 40.0

    def test_funnel_thresholds(self):
        from aria.monitoring.behavior_checks import (
            DEFAULT_FUNNEL_DROP_WARNING,
            DEFAULT_FUNNEL_DROP_CRITICAL,
        )
        assert DEFAULT_FUNNEL_DROP_WARNING == 50.0
        assert DEFAULT_FUNNEL_DROP_CRITICAL == 70.0


# ============================================================
# 2. Traffic Overview
# ============================================================


class TestTrafficOverview:
    """check_traffic_overview"""

    @pytest.mark.asyncio
    async def test_api_error(self):
        from aria.monitoring.behavior_checks import check_traffic_overview

        with patch("aria.monitoring.behavior_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post.return_value = _mock_ga4_response(status=403)

            result = await check_traffic_overview("123456", "token")
            assert len(result["issues"]) == 1
            assert result["issues"][0]["type"] == "api_error"

    @pytest.mark.asyncio
    async def test_timeout(self):
        import httpx
        from aria.monitoring.behavior_checks import check_traffic_overview

        with patch("aria.monitoring.behavior_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post.side_effect = httpx.TimeoutException("timeout")

            result = await check_traffic_overview("123456", "token")
            assert result["issues"][0]["type"] == "timeout"

    @pytest.mark.asyncio
    async def test_normal_traffic(self):
        from aria.monitoring.behavior_checks import check_traffic_overview

        totals = [{
            "metricValues": [
                {"value": "1500"},  # sessions
                {"value": "1200"},  # activeUsers
                {"value": "300"},   # newUsers
                {"value": "0.45"},  # bounceRate
                {"value": "0.55"},  # engagementRate
                {"value": "120.5"}, # avgSessionDuration
                {"value": "5000"},  # screenPageViews
            ],
        }]
        met_headers = [
            "sessions", "activeUsers", "newUsers",
            "bounceRate", "engagementRate",
            "averageSessionDuration", "screenPageViews",
        ]

        with patch("aria.monitoring.behavior_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post.return_value = _mock_ga4_response(
                totals=totals, met_headers=met_headers,
            )

            result = await check_traffic_overview("123456", "token")
            assert result["sessions"] == 1500
            assert result["bounce_rate"] == 45.0
            assert len(result["issues"]) == 0  # 45% < 70%

    @pytest.mark.asyncio
    async def test_high_bounce_rate(self):
        from aria.monitoring.behavior_checks import check_traffic_overview

        totals = [{"metricValues": [
            {"value": "100"}, {"value": "80"}, {"value": "20"},
            {"value": "0.88"}, {"value": "0.12"}, {"value": "30"}, {"value": "150"},
        ]}]

        with patch("aria.monitoring.behavior_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post.return_value = _mock_ga4_response(
                totals=totals,
                met_headers=["sessions", "activeUsers", "newUsers", "bounceRate",
                            "engagementRate", "averageSessionDuration", "screenPageViews"],
            )

            result = await check_traffic_overview("123456", "token")
            assert result["bounce_rate"] == 88.0
            assert len(result["issues"]) == 1
            assert result["issues"][0]["severity"] == "high"


# ============================================================
# 3. Exit Pages
# ============================================================


class TestExitPages:
    """check_exit_pages"""

    @pytest.mark.asyncio
    async def test_no_problem_pages(self):
        from aria.monitoring.behavior_checks import check_exit_pages

        rows = [{
            "dimensionValues": [{"value": "/home"}, {"value": "Home"}],
            "metricValues": [{"value": "100"}, {"value": "0.30"}, {"value": "0.70"}],
        }]

        with patch("aria.monitoring.behavior_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post.return_value = _mock_ga4_response(
                rows=rows,
                dim_headers=["pagePath", "pageTitle"],
                met_headers=["sessions", "bounceRate", "engagementRate"],
            )

            result = await check_exit_pages("123456", "token")
            assert result["problem_pages"] == 0

    @pytest.mark.asyncio
    async def test_high_exit_page_detected(self):
        from aria.monitoring.behavior_checks import check_exit_pages

        rows = [{
            "dimensionValues": [{"value": "/checkout"}, {"value": "Checkout"}],
            "metricValues": [{"value": "50"}, {"value": "0.90"}, {"value": "0.10"}],
        }]

        with patch("aria.monitoring.behavior_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post.return_value = _mock_ga4_response(
                rows=rows,
                dim_headers=["pagePath", "pageTitle"],
                met_headers=["sessions", "bounceRate", "engagementRate"],
            )

            result = await check_exit_pages("123456", "token")
            assert result["problem_pages"] == 1
            assert result["issues"][0]["severity"] == "high"
            assert "/checkout" in result["issues"][0]["message"]

    @pytest.mark.asyncio
    async def test_min_sessions_filter(self):
        from aria.monitoring.behavior_checks import check_exit_pages

        rows = [{
            "dimensionValues": [{"value": "/rare"}, {"value": "Rare"}],
            "metricValues": [{"value": "3"}, {"value": "0.95"}, {"value": "0.05"}],
        }]

        with patch("aria.monitoring.behavior_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post.return_value = _mock_ga4_response(
                rows=rows,
                dim_headers=["pagePath", "pageTitle"],
                met_headers=["sessions", "bounceRate", "engagementRate"],
            )

            result = await check_exit_pages("123456", "token", min_sessions=10)
            assert result["problem_pages"] == 0  # 3 < 10 minimum


# ============================================================
# 4. Conversion Rate
# ============================================================


class TestConversionRate:
    """check_conversion_rate"""

    @pytest.mark.asyncio
    async def test_stable_conversion(self):
        from aria.monitoring.behavior_checks import check_conversion_rate

        rows = [
            {"dimensionValues": [{"value": "date_range_0"}],
             "metricValues": [{"value": "1000"}, {"value": "50"}]},
            {"dimensionValues": [{"value": "date_range_1"}],
             "metricValues": [{"value": "1000"}, {"value": "50"}]},
        ]

        with patch("aria.monitoring.behavior_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post.return_value = _mock_ga4_response(
                rows=rows,
                dim_headers=["dateRange"],
                met_headers=["sessions", "conversions"],
            )

            result = await check_conversion_rate("123456", "token")
            assert result["rate_change_pct"] == 0.0
            assert len(result["issues"]) == 0

    @pytest.mark.asyncio
    async def test_conversion_drop(self):
        from aria.monitoring.behavior_checks import check_conversion_rate

        rows = [
            {"dimensionValues": [{"value": "date_range_0"}],
             "metricValues": [{"value": "1000"}, {"value": "20"}]},  # 2%
            {"dimensionValues": [{"value": "date_range_1"}],
             "metricValues": [{"value": "1000"}, {"value": "50"}]},  # 5%
        ]

        with patch("aria.monitoring.behavior_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post.return_value = _mock_ga4_response(
                rows=rows,
                dim_headers=["dateRange"],
                met_headers=["sessions", "conversions"],
            )

            result = await check_conversion_rate("123456", "token")
            assert result["rate_change_pct"] == -60.0  # 2% vs 5%
            assert len(result["issues"]) == 1
            assert result["issues"][0]["severity"] == "high"


# ============================================================
# 5. Funnel Dropoff
# ============================================================


class TestFunnelDropoff:
    """check_funnel_dropoff"""

    @pytest.mark.asyncio
    async def test_healthy_funnel(self):
        from aria.monitoring.behavior_checks import check_funnel_dropoff

        rows = [
            {"dimensionValues": [{"value": "page_view"}], "metricValues": [{"value": "1000"}, {"value": "1000"}]},
            {"dimensionValues": [{"value": "scroll"}], "metricValues": [{"value": "800"}, {"value": "800"}]},
            {"dimensionValues": [{"value": "click"}], "metricValues": [{"value": "600"}, {"value": "600"}]},
            {"dimensionValues": [{"value": "purchase"}], "metricValues": [{"value": "400"}, {"value": "400"}]},
        ]

        with patch("aria.monitoring.behavior_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post.return_value = _mock_ga4_response(
                rows=rows,
                dim_headers=["eventName"],
                met_headers=["eventCount", "totalUsers"],
            )

            result = await check_funnel_dropoff("123456", "token")
            assert len(result["funnel_steps"]) == 4
            assert len(result["issues"]) == 0  # max drop ~33%

    @pytest.mark.asyncio
    async def test_bottleneck_detected(self):
        from aria.monitoring.behavior_checks import check_funnel_dropoff

        rows = [
            {"dimensionValues": [{"value": "page_view"}], "metricValues": [{"value": "1000"}, {"value": "1000"}]},
            {"dimensionValues": [{"value": "scroll"}], "metricValues": [{"value": "800"}, {"value": "800"}]},
            {"dimensionValues": [{"value": "click"}], "metricValues": [{"value": "100"}, {"value": "100"}]},
            {"dimensionValues": [{"value": "purchase"}], "metricValues": [{"value": "50"}, {"value": "50"}]},
        ]

        with patch("aria.monitoring.behavior_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post.return_value = _mock_ga4_response(
                rows=rows,
                dim_headers=["eventName"],
                met_headers=["eventCount", "totalUsers"],
            )

            result = await check_funnel_dropoff("123456", "token")
            assert result["bottleneck_step"] == "click"  # 800 → 100 = 87.5%
            assert any(i["severity"] == "high" for i in result["issues"])


# ============================================================
# 6. run_behavior_audit
# ============================================================


class TestBehaviorAudit:
    """run_behavior_audit 통합"""

    @pytest.mark.asyncio
    async def test_aggregates_issues(self):
        from aria.monitoring.behavior_checks import run_behavior_audit

        with patch("aria.monitoring.behavior_checks.check_traffic_overview") as m1, \
             patch("aria.monitoring.behavior_checks.check_exit_pages") as m2, \
             patch("aria.monitoring.behavior_checks.check_conversion_rate") as m3, \
             patch("aria.monitoring.behavior_checks.check_funnel_dropoff") as m4:

            m1.return_value = {"check_type": "traffic_overview", "issues": [{"type": "x", "severity": "high", "message": "test"}]}
            m2.return_value = {"check_type": "exit_pages", "issues": []}
            m3.return_value = {"check_type": "conversion_rate", "issues": [{"type": "y", "severity": "medium", "message": "test2"}]}
            m4.return_value = {"check_type": "funnel_dropoff", "issues": []}

            result = await run_behavior_audit("123", "token")
            assert result["total_issues"] == 2
            assert result["high_issues"] == 1
            assert result["elapsed_ms"] >= 0


# ============================================================
# 7. BehaviorAuditTool (ToolExecutor)
# ============================================================


class TestBehaviorAuditTool:
    """BehaviorAuditTool"""

    def test_definition(self):
        from aria.tools.mcp.behavior_monitor_tools import BehaviorAuditTool
        tool = BehaviorAuditTool()
        defn = tool.get_definition()
        assert defn.name == "behavior_audit"
        assert defn.safety_hint.value == "read_only"
        assert len(defn.parameters) == 4

    @pytest.mark.asyncio
    async def test_missing_property_id(self):
        from aria.tools.mcp.behavior_monitor_tools import BehaviorAuditTool
        tool = BehaviorAuditTool()
        result = await tool.execute({"property_id": ""})
        assert not result.success
        assert "비어있습니다" in result.error

    @pytest.mark.asyncio
    async def test_missing_access_token(self):
        from aria.tools.mcp.behavior_monitor_tools import BehaviorAuditTool
        tool = BehaviorAuditTool(token_manager=None)
        result = await tool.execute({"property_id": "123456"})
        assert not result.success
        assert "access_token" in result.error

    @pytest.mark.asyncio
    async def test_token_manager_used(self):
        from aria.tools.mcp.behavior_monitor_tools import BehaviorAuditTool

        mock_tm = AsyncMock()
        mock_tm.get_access_token.return_value = "test-token"

        tool = BehaviorAuditTool(token_manager=mock_tm)
        mock_audit = {
            "property_id": "123",
            "overview": {"check_type": "traffic_overview", "sessions": 100, "bounce_rate": 30.0, "active_users": 80, "engagement_rate": 70.0},
            "exit_pages": {"check_type": "exit_pages", "problem_pages": 0, "exit_pages": []},
            "conversion": {"check_type": "conversion_rate", "current_rate": 5.0, "rate_change_pct": 0.0, "conversion_event": "purchase"},
            "funnel": {"check_type": "funnel_dropoff", "bottleneck_step": None},
            "total_issues": 0,
            "high_issues": 0,
        }

        with patch("aria.tools.mcp.behavior_monitor_tools.run_behavior_audit", return_value=mock_audit):
            result = await tool.execute({"property_id": "123"})
            assert result.success
            mock_tm.get_access_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_direct_access_token(self):
        from aria.tools.mcp.behavior_monitor_tools import BehaviorAuditTool

        tool = BehaviorAuditTool()
        mock_audit = {
            "property_id": "123",
            "overview": {"check_type": "traffic_overview", "sessions": 0, "bounce_rate": 0, "active_users": 0, "engagement_rate": 0},
            "exit_pages": {"check_type": "exit_pages", "problem_pages": 0, "exit_pages": []},
            "conversion": {"check_type": "conversion_rate", "current_rate": 0, "rate_change_pct": 0, "conversion_event": "purchase"},
            "funnel": {"check_type": "funnel_dropoff", "bottleneck_step": None},
            "total_issues": 0,
            "high_issues": 0,
        }

        with patch("aria.tools.mcp.behavior_monitor_tools.run_behavior_audit", return_value=mock_audit):
            result = await tool.execute({"property_id": "123", "access_token": "direct-token"})
            assert result.success

    @pytest.mark.asyncio
    async def test_llm_format(self):
        from aria.tools.mcp.behavior_monitor_tools import BehaviorAuditTool
        tool = BehaviorAuditTool()
        llm = tool.get_definition().to_llm_tool()
        assert llm["type"] == "function"
        assert llm["function"]["name"] == "behavior_audit"
        assert "property_id" in llm["function"]["parameters"]["required"]


# ============================================================
# 8. AlertType + AlertManager
# ============================================================


class TestBehaviorAlertTypes:
    """사용자 행동 AlertType"""

    def test_types_exist(self):
        from aria.alerts.alert_types import AlertType
        assert AlertType.USER_BOUNCE_RATE.value == "user_bounce_rate"
        assert AlertType.USER_CONVERSION_DROP.value == "user_conversion_drop"
        assert AlertType.USER_FUNNEL_BOTTLENECK.value == "user_funnel_bottleneck"

    def test_emoji_mappings(self):
        from aria.alerts.alert_types import ALERT_EMOJI, AlertType
        assert AlertType.USER_BOUNCE_RATE in ALERT_EMOJI
        assert AlertType.USER_CONVERSION_DROP in ALERT_EMOJI
        assert AlertType.USER_FUNNEL_BOTTLENECK in ALERT_EMOJI

    def test_cooldown_mappings(self):
        from aria.alerts.alert_types import DEFAULT_COOLDOWNS, AlertType
        assert DEFAULT_COOLDOWNS[AlertType.USER_BOUNCE_RATE] == 86400
        assert DEFAULT_COOLDOWNS[AlertType.USER_CONVERSION_DROP] == 86400
        assert DEFAULT_COOLDOWNS[AlertType.USER_FUNNEL_BOTTLENECK] == 86400


class TestAlertManagerBehavior:
    """AlertManager 사용자 행동 알림"""

    def _make_manager(self):
        from aria.alerts.alert_manager import AlertManager
        return AlertManager(bot_token="test", chat_id="123", enabled=True)

    @pytest.mark.asyncio
    async def test_bounce_below_threshold(self):
        mgr = self._make_manager()
        result = await mgr.check_user_bounce_rate(None, 50.0, 100)
        assert result is None

    @pytest.mark.asyncio
    async def test_bounce_warning(self):
        mgr = self._make_manager()
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_user_bounce_rate("Testorum", 75.0, 500)
            assert result is not None
            assert result.level.value == "warning"

    @pytest.mark.asyncio
    async def test_conversion_drop_critical(self):
        mgr = self._make_manager()
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_user_conversion_drop(
                "Testorum", 1.0, 5.0, -80.0, "purchase",
            )
            assert result is not None
            assert result.level.value == "critical"

    @pytest.mark.asyncio
    async def test_funnel_bottleneck(self):
        mgr = self._make_manager()
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_user_funnel_bottleneck(
                None, "purchase", 75.0, "click", 400, 100,
            )
            assert result is not None
            assert result.level.value == "critical"

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(enabled=False)
        assert await mgr.check_user_bounce_rate(None, 99.0, 100) is None
        assert await mgr.check_user_conversion_drop(None, 0, 5, -99.0) is None
        assert await mgr.check_user_funnel_bottleneck(None, "x", 99.0, "y", 100, 1) is None
