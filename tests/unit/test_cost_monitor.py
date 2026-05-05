"""ARIA Engine - Cost Optimization Monitor Tests (Phase 3.5 Step 8)

비용 최적화 모니터링 테스트
- cost_checks.py (Vercel / Supabase / API costs)
- CostAuditTool ToolExecutor
- AlertType / AlertManager 인프라 비용 알림

총 테스트: ~35개
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_response(data: Any, status: int = 200) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = data
    mock.text = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
    return mock


# ============================================================
# 1. Utility
# ============================================================


class TestUsageSeverity:
    """_usage_severity"""

    def test_normal(self):
        from aria.monitoring.cost_checks import _usage_severity
        assert _usage_severity(30, 100) is None

    def test_warning(self):
        from aria.monitoring.cost_checks import _usage_severity
        assert _usage_severity(75, 100) == "medium"

    def test_critical(self):
        from aria.monitoring.cost_checks import _usage_severity
        assert _usage_severity(95, 100) == "high"

    def test_zero_limit(self):
        from aria.monitoring.cost_checks import _usage_severity
        assert _usage_severity(50, 0) is None

    def test_exact_boundary(self):
        from aria.monitoring.cost_checks import _usage_severity
        assert _usage_severity(70, 100) == "medium"
        assert _usage_severity(90, 100) == "high"


class TestPlanLimits:
    """Free tier 기본 한도값"""

    def test_vercel_limits(self):
        from aria.monitoring.cost_checks import VERCEL_FREE_LIMITS
        assert VERCEL_FREE_LIMITS["bandwidth_gb"] == 100
        assert VERCEL_FREE_LIMITS["function_invocations"] == 100_000

    def test_supabase_limits(self):
        from aria.monitoring.cost_checks import SUPABASE_FREE_LIMITS
        assert SUPABASE_FREE_LIMITS["db_size_gb"] == 0.5
        assert SUPABASE_FREE_LIMITS["storage_gb"] == 1.0


# ============================================================
# 2. Vercel Usage
# ============================================================


class TestVercelUsage:
    """check_vercel_usage"""

    @pytest.mark.asyncio
    async def test_auth_failure(self):
        from aria.monitoring.cost_checks import check_vercel_usage

        with patch("aria.monitoring.cost_checks.httpx.AsyncClient") as MC:
            client = AsyncMock()
            MC.return_value.__aenter__ = AsyncMock(return_value=client)
            MC.return_value.__aexit__ = AsyncMock(return_value=None)
            client.get.return_value = _mock_response({}, 401)

            result = await check_vercel_usage("bad-token")
            assert len(result["issues"]) == 1
            assert result["issues"][0]["type"] == "auth_failed"

    @pytest.mark.asyncio
    async def test_normal_usage(self):
        from aria.monitoring.cost_checks import check_vercel_usage

        usage_data = {
            "usage": {
                "bandwidth": {"used": 10 * 1024**3},  # 10GB
                "serverlessFunctionExecution": {"used": 5000},
                "buildExecution": {"used": 100},
                "imageOptimization": {"used": 50},
            },
        }

        with patch("aria.monitoring.cost_checks.httpx.AsyncClient") as MC:
            client = AsyncMock()
            MC.return_value.__aenter__ = AsyncMock(return_value=client)
            MC.return_value.__aexit__ = AsyncMock(return_value=None)
            client.get.return_value = _mock_response(usage_data)

            result = await check_vercel_usage("good-token")
            assert result["bandwidth_gb"] == 10.0
            assert result["function_invocations"] == 5000
            assert len(result["issues"]) == 0  # 10% usage

    @pytest.mark.asyncio
    async def test_high_bandwidth(self):
        from aria.monitoring.cost_checks import check_vercel_usage

        usage_data = {
            "usage": {
                "bandwidth": {"used": 95 * 1024**3},  # 95GB of 100GB
                "serverlessFunctionExecution": {"used": 0},
                "buildExecution": {"used": 0},
                "imageOptimization": {"used": 0},
            },
        }

        with patch("aria.monitoring.cost_checks.httpx.AsyncClient") as MC:
            client = AsyncMock()
            MC.return_value.__aenter__ = AsyncMock(return_value=client)
            MC.return_value.__aexit__ = AsyncMock(return_value=None)
            client.get.return_value = _mock_response(usage_data)

            result = await check_vercel_usage("token")
            assert result["usage_pct"]["bandwidth_gb"] == 95.0
            assert any(i["severity"] == "high" for i in result["issues"])

    @pytest.mark.asyncio
    async def test_recommendations_generated(self):
        from aria.monitoring.cost_checks import check_vercel_usage

        usage_data = {
            "usage": {
                "bandwidth": {"used": 60 * 1024**3},  # 60GB > 50% of 100GB
                "serverlessFunctionExecution": {"used": 0},
                "buildExecution": {"used": 4000},  # > 50% of 6000
                "imageOptimization": {"used": 0},
            },
        }

        with patch("aria.monitoring.cost_checks.httpx.AsyncClient") as MC:
            client = AsyncMock()
            MC.return_value.__aenter__ = AsyncMock(return_value=client)
            MC.return_value.__aexit__ = AsyncMock(return_value=None)
            client.get.return_value = _mock_response(usage_data)

            result = await check_vercel_usage("token")
            assert len(result["recommendations"]) == 2


# ============================================================
# 3. Supabase Usage
# ============================================================


class TestSupabaseUsage:
    """check_supabase_usage"""

    @pytest.mark.asyncio
    async def test_project_not_found(self):
        from aria.monitoring.cost_checks import check_supabase_usage

        with patch("aria.monitoring.cost_checks.httpx.AsyncClient") as MC:
            client = AsyncMock()
            MC.return_value.__aenter__ = AsyncMock(return_value=client)
            MC.return_value.__aexit__ = AsyncMock(return_value=None)
            client.get.return_value = _mock_response({}, 404)

            result = await check_supabase_usage("token", "bad-ref")
            assert result["issues"][0]["type"] == "project_not_found"

    @pytest.mark.asyncio
    async def test_normal_usage(self):
        from aria.monitoring.cost_checks import check_supabase_usage

        data = {
            "db_size": int(0.1 * 1024**3),
            "storage_size": int(0.2 * 1024**3),
            "bandwidth": int(0.5 * 1024**3),
            "edge_function_invocations": 1000,
        }

        with patch("aria.monitoring.cost_checks.httpx.AsyncClient") as MC:
            client = AsyncMock()
            MC.return_value.__aenter__ = AsyncMock(return_value=client)
            MC.return_value.__aexit__ = AsyncMock(return_value=None)
            client.get.return_value = _mock_response(data)

            result = await check_supabase_usage("token", "ref123")
            assert result["db_size_gb"] == 0.1
            assert len(result["issues"]) == 0


# ============================================================
# 4. API Costs
# ============================================================


class TestApiCosts:
    """check_api_costs"""

    @pytest.mark.asyncio
    async def test_normal_costs(self):
        from aria.monitoring.cost_checks import check_api_costs

        data = {
            "daily_cost_usd": 2.0,
            "monthly_cost_usd": 50.0,
            "total_requests": 100,
            "total_cached_tokens": 5000,
        }

        with patch("aria.monitoring.cost_checks.httpx.AsyncClient") as MC:
            client = AsyncMock()
            MC.return_value.__aenter__ = AsyncMock(return_value=client)
            MC.return_value.__aexit__ = AsyncMock(return_value=None)
            client.get.return_value = _mock_response(data)

            result = await check_api_costs("http://localhost:8100")
            assert result["daily_cost"] == 2.0
            assert result["monthly_cost"] == 50.0
            assert len(result["issues"]) == 0

    @pytest.mark.asyncio
    async def test_high_daily_cost(self):
        from aria.monitoring.cost_checks import check_api_costs

        data = {
            "daily_cost_usd": 9.5,
            "monthly_cost_usd": 280.0,
            "total_requests": 500,
            "total_cached_tokens": 10000,
        }

        with patch("aria.monitoring.cost_checks.httpx.AsyncClient") as MC:
            client = AsyncMock()
            MC.return_value.__aenter__ = AsyncMock(return_value=client)
            MC.return_value.__aexit__ = AsyncMock(return_value=None)
            client.get.return_value = _mock_response(data)

            result = await check_api_costs("http://localhost:8100")
            assert result["daily_pct"] == 95.0
            assert any(i["type"] == "daily_cost_high" for i in result["issues"])

    @pytest.mark.asyncio
    async def test_cache_recommendation(self):
        from aria.monitoring.cost_checks import check_api_costs

        data = {
            "daily_cost_usd": 1.0,
            "monthly_cost_usd": 20.0,
            "total_requests": 50,
            "total_cached_tokens": 0,
        }

        with patch("aria.monitoring.cost_checks.httpx.AsyncClient") as MC:
            client = AsyncMock()
            MC.return_value.__aenter__ = AsyncMock(return_value=client)
            MC.return_value.__aexit__ = AsyncMock(return_value=None)
            client.get.return_value = _mock_response(data)

            result = await check_api_costs("http://localhost:8100")
            assert any("Prompt Caching" in r for r in result["recommendations"])

    @pytest.mark.asyncio
    async def test_connection_error(self):
        import httpx
        from aria.monitoring.cost_checks import check_api_costs

        with patch("aria.monitoring.cost_checks.httpx.AsyncClient") as MC:
            client = AsyncMock()
            MC.return_value.__aenter__ = AsyncMock(return_value=client)
            MC.return_value.__aexit__ = AsyncMock(return_value=None)
            client.get.side_effect = httpx.ConnectError("refused")

            result = await check_api_costs("http://localhost:8100")
            assert result["issues"][0]["type"] == "connection_error"


# ============================================================
# 5. run_cost_audit
# ============================================================


class TestCostAudit:
    """run_cost_audit 통합"""

    @pytest.mark.asyncio
    async def test_no_tokens_set(self):
        from aria.monitoring.cost_checks import run_cost_audit

        with patch("aria.monitoring.cost_checks.check_api_costs") as mock_api:
            mock_api.return_value = {"check_type": "api_costs", "issues": [], "recommendations": []}

            result = await run_cost_audit()
            assert result["vercel"]["note"]
            assert result["supabase"]["note"]
            assert result["elapsed_ms"] >= 0

    @pytest.mark.asyncio
    async def test_aggregates_all(self):
        from aria.monitoring.cost_checks import run_cost_audit

        with patch("aria.monitoring.cost_checks.check_vercel_usage") as mv, \
             patch("aria.monitoring.cost_checks.check_supabase_usage") as ms, \
             patch("aria.monitoring.cost_checks.check_api_costs") as ma:
            mv.return_value = {"issues": [{"type": "x", "severity": "high", "message": "t"}], "recommendations": ["r1"]}
            ms.return_value = {"issues": [], "recommendations": ["r2"]}
            ma.return_value = {"issues": [], "recommendations": []}

            result = await run_cost_audit(
                vercel_token="t", supabase_token="t", supabase_project_ref="ref",
            )
            assert result["total_issues"] == 1
            assert result["high_issues"] == 1
            assert len(result["all_recommendations"]) == 2


# ============================================================
# 6. CostAuditTool
# ============================================================


class TestCostAuditTool:
    """CostAuditTool ToolExecutor"""

    def test_definition(self):
        from aria.tools.mcp.cost_monitor_tools import CostAuditTool
        tool = CostAuditTool()
        defn = tool.get_definition()
        assert defn.name == "cost_audit"
        assert defn.safety_hint.value == "read_only"

    @pytest.mark.asyncio
    async def test_successful_audit(self):
        from aria.tools.mcp.cost_monitor_tools import CostAuditTool

        mock_audit = {
            "vercel": {"note": "no token", "issues": [], "recommendations": []},
            "supabase": {"note": "no token", "issues": [], "recommendations": []},
            "api_costs": {"check_type": "api_costs", "daily_cost": 1.0, "monthly_cost": 20.0,
                          "daily_pct": 10.0, "monthly_pct": 6.7, "issues": [], "recommendations": []},
            "total_issues": 0, "high_issues": 0,
            "all_recommendations": [],
        }

        with patch("aria.tools.mcp.cost_monitor_tools.run_cost_audit", return_value=mock_audit):
            tool = CostAuditTool()
            result = await tool.execute({})
            assert result.success
            assert result.output["total_issues"] == 0

    @pytest.mark.asyncio
    async def test_llm_format(self):
        from aria.tools.mcp.cost_monitor_tools import CostAuditTool
        tool = CostAuditTool()
        llm = tool.get_definition().to_llm_tool()
        assert llm["type"] == "function"
        assert llm["function"]["name"] == "cost_audit"


# ============================================================
# 7. AlertType + AlertManager
# ============================================================


class TestInfraCostAlertType:
    """INFRA_COST_HIGH"""

    def test_type_exists(self):
        from aria.alerts.alert_types import AlertType
        assert AlertType.INFRA_COST_HIGH.value == "infra_cost_high"

    def test_emoji(self):
        from aria.alerts.alert_types import ALERT_EMOJI, AlertType
        assert AlertType.INFRA_COST_HIGH in ALERT_EMOJI

    def test_cooldown(self):
        from aria.alerts.alert_types import DEFAULT_COOLDOWNS, AlertType
        assert DEFAULT_COOLDOWNS[AlertType.INFRA_COST_HIGH] == 86400


class TestAlertManagerInfraCost:
    """AlertManager infra cost"""

    def _mgr(self):
        from aria.alerts.alert_manager import AlertManager
        return AlertManager(bot_token="t", chat_id="1", enabled=True)

    @pytest.mark.asyncio
    async def test_below_threshold(self):
        result = await self._mgr().check_infra_cost_high("vercel", None, "bandwidth_gb", 50.0, 50, 100)
        assert result is None

    @pytest.mark.asyncio
    async def test_warning(self):
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as ms:
            ms.return_value = {"ok": True}
            result = await self._mgr().check_infra_cost_high("vercel", "Testorum", "bandwidth_gb", 75.0, 75, 100)
            assert result is not None
            assert result.level.value == "warning"

    @pytest.mark.asyncio
    async def test_critical(self):
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as ms:
            ms.return_value = {"ok": True}
            result = await self._mgr().check_infra_cost_high("supabase", None, "db_size_gb", 95.0, 0.475, 0.5)
            assert result is not None
            assert result.level.value == "critical"

    @pytest.mark.asyncio
    async def test_disabled(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(enabled=False)
        assert await mgr.check_infra_cost_high("x", None, "y", 99.0, 99, 100) is None
