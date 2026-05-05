"""ARIA Engine - Weekly Report Tests (Phase 3.5 Step 9)

주간 리포트 테스트
- weekly_report.py 핵심 로직 (빌드/포맷/분할)
- WeeklyReportTool ToolExecutor
- 텔레그램 전송 (mock)

총 테스트: ~30개
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


# ============================================================
# 1. Utility Functions
# ============================================================


class TestStatusEmoji:
    """_status_emoji"""

    def test_no_issues(self):
        from aria.monitoring.weekly_report import _status_emoji
        assert _status_emoji([]) == "🟢"

    def test_medium_issues(self):
        from aria.monitoring.weekly_report import _status_emoji
        assert _status_emoji([{"severity": "medium"}]) == "🟡"

    def test_high_issues(self):
        from aria.monitoring.weekly_report import _status_emoji
        assert _status_emoji([{"severity": "high"}]) == "🔴"

    def test_mixed_uses_highest(self):
        from aria.monitoring.weekly_report import _status_emoji
        assert _status_emoji([
            {"severity": "low"},
            {"severity": "medium"},
            {"severity": "high"},
        ]) == "🔴"


class TestFormatSection:
    """_format_section"""

    def test_basic_section(self):
        from aria.monitoring.weekly_report import _format_section
        result = _format_section("테스트", {"count": 42, "issues": []}, [
            ("count", "건수", "int"),
        ])
        assert "테스트" in result
        assert "42" in result
        assert "🟢" in result

    def test_section_with_issues(self):
        from aria.monitoring.weekly_report import _format_section
        result = _format_section("SEO", {
            "issues": [{"severity": "high", "message": "robots.txt 문제"}],
        }, [])
        assert "🔴" in result
        assert "robots.txt" in result

    def test_section_with_recommendations(self):
        from aria.monitoring.weekly_report import _format_section
        result = _format_section("비용", {
            "issues": [],
            "recommendations": ["CDN 최적화 필요"],
        }, [])
        assert "💡" in result
        assert "CDN" in result

    def test_pct_format(self):
        from aria.monitoring.weekly_report import _format_section
        result = _format_section("행동", {"rate": 45.678, "issues": []}, [
            ("rate", "이탈률", "pct"),
        ])
        assert "45.7%" in result

    def test_dollar_format(self):
        from aria.monitoring.weekly_report import _format_section
        result = _format_section("비용", {"cost": 12.5, "issues": []}, [
            ("cost", "월 비용", "dollar"),
        ])
        assert "$12.50" in result


# ============================================================
# 2. build_weekly_report
# ============================================================


class TestBuildWeeklyReport:
    """build_weekly_report"""

    def test_empty_report(self):
        from aria.monitoring.weekly_report import build_weekly_report
        report = build_weekly_report("Testorum", "2026-05-01 ~ 05-07")
        assert report["product_name"] == "Testorum"
        assert report["total_issues"] == 0
        assert "주간 제품 리포트" in report["markdown"]
        assert len(report["sections"]) == 0

    def test_seo_section(self):
        from aria.monitoring.weekly_report import build_weekly_report
        seo = {
            "total_pages": 10,
            "healthy_pages": 8,
            "issues": [{"severity": "medium", "message": "missing meta"}],
            "recommendations": [],
        }
        report = build_weekly_report("Test", "w1", seo=seo)
        assert "seo" in report["sections"]
        assert report["total_issues"] == 1
        assert "SEO" in report["markdown"]

    def test_payment_section(self):
        from aria.monitoring.weekly_report import build_weekly_report
        payment = {
            "refunds": {"refund_rate": 3.5},
            "failures": {"failure_rate": 1.2},
            "churn": {"churn_rate": 4.0},
            "all_issues": [],
        }
        report = build_weekly_report("Test", "w1", payment=payment)
        assert "payment" in report["sections"]
        assert "3.5%" in report["markdown"]

    def test_behavior_section(self):
        from aria.monitoring.weekly_report import build_weekly_report
        behavior = {
            "overview": {"sessions": 1500, "bounce_rate": 45.0},
            "conversion": {"current_rate": 5.2},
            "all_issues": [],
        }
        report = build_weekly_report("Test", "w1", behavior=behavior)
        assert "behavior" in report["sections"]
        assert "1,500" in report["markdown"]

    def test_cost_section(self):
        from aria.monitoring.weekly_report import build_weekly_report
        cost = {
            "api_costs": {"monthly_cost": 50.0, "monthly_pct": 16.7},
            "all_issues": [],
            "all_recommendations": ["Prompt Caching 활성화"],
        }
        report = build_weekly_report("Test", "w1", cost=cost)
        assert "cost" in report["sections"]
        assert "$50.00" in report["markdown"]
        assert report["total_recommendations"] == 1

    def test_all_sections(self):
        from aria.monitoring.weekly_report import build_weekly_report
        report = build_weekly_report(
            "Testorum", "w1",
            seo={"issues": [], "total_pages": 5, "healthy_pages": 5},
            db={"issues": [], "table_count": 10, "total_rows": 5000, "db_size_mb": 50},
            dep={"issues": [{"severity": "high", "message": "vuln"}], "total_deps": 20, "vulnerable_count": 1},
            payment={"refunds": {"refund_rate": 0}, "failures": {"failure_rate": 0}, "churn": {"churn_rate": 0}, "all_issues": []},
            behavior={"overview": {"sessions": 100, "bounce_rate": 30}, "conversion": {"current_rate": 3}, "all_issues": []},
            cost={"api_costs": {"monthly_cost": 10, "monthly_pct": 3}, "all_issues": [], "all_recommendations": []},
        )
        assert len(report["sections"]) == 6
        assert report["total_issues"] == 1
        assert report["high_issues"] == 1

    def test_custom_sections(self):
        from aria.monitoring.weekly_report import build_weekly_report
        report = build_weekly_report(
            "Test", "w1",
            custom_sections=[{"title": "릴리즈 노트", "content": "v2.0 출시"}],
        )
        assert "릴리즈 노트" in report["markdown"]
        assert any("custom:" in s for s in report["sections"])

    def test_total_summary(self):
        from aria.monitoring.weekly_report import build_weekly_report
        report = build_weekly_report(
            "Test", "w1",
            seo={"issues": [{"severity": "high", "message": "x"}, {"severity": "medium", "message": "y"}]},
            db={"issues": [{"severity": "low", "message": "z"}]},
        )
        assert report["total_issues"] == 3
        assert report["high_issues"] == 1
        assert "총평" in report["markdown"]


# ============================================================
# 3. send_report_telegram
# ============================================================


class TestSendReportTelegram:
    """send_report_telegram"""

    @pytest.mark.asyncio
    async def test_short_message(self):
        from aria.monitoring.weekly_report import send_report_telegram
        with patch("aria.telegram.notifier.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await send_report_telegram("short msg", "token", "123")
            assert result["success"]
            assert result["messages_sent"] == 1
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_long_message_split(self):
        from aria.monitoring.weekly_report import send_report_telegram
        # 5000자 초과 메시지
        long_msg = "\n".join([f"Line {i}: " + "x" * 50 for i in range(100)])
        assert len(long_msg) > 4096

        with patch("aria.telegram.notifier.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await send_report_telegram(long_msg, "token", "123")
            assert result["success"]
            assert result["messages_sent"] >= 2

    @pytest.mark.asyncio
    async def test_send_failure(self):
        from aria.monitoring.weekly_report import send_report_telegram
        with patch("aria.telegram.notifier.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": False, "error": "bad request"}
            result = await send_report_telegram("msg", "token", "123")
            assert not result["success"]
            assert len(result["errors"]) == 1

    @pytest.mark.asyncio
    async def test_send_exception(self):
        from aria.monitoring.weekly_report import send_report_telegram
        with patch("aria.telegram.notifier.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = Exception("network error")
            result = await send_report_telegram("msg", "token", "123")
            assert not result["success"]


# ============================================================
# 4. generate_report
# ============================================================


class TestGenerateReport:
    """generate_report"""

    @pytest.mark.asyncio
    async def test_empty_checks(self):
        from aria.monitoring.weekly_report import generate_report
        report = await generate_report("Test", "w1", {})
        assert report["product_name"] == "Test"
        assert report["elapsed_ms"] >= 0
        assert len(report["sections"]) == 0

    @pytest.mark.asyncio
    async def test_with_checks(self):
        from aria.monitoring.weekly_report import generate_report
        checks = {
            "seo": {"issues": [], "total_pages": 5, "healthy_pages": 5},
            "cost": {"api_costs": {"monthly_cost": 20, "monthly_pct": 7}, "all_issues": [], "all_recommendations": []},
        }
        report = await generate_report("Testorum", "2026-W18", checks)
        assert len(report["sections"]) == 2
        assert "seo" in report["sections"]
        assert "cost" in report["sections"]


# ============================================================
# 5. WeeklyReportTool
# ============================================================


class TestWeeklyReportTool:
    """WeeklyReportTool ToolExecutor"""

    def test_definition(self):
        from aria.tools.mcp.report_tools import WeeklyReportTool
        tool = WeeklyReportTool()
        defn = tool.get_definition()
        assert defn.name == "weekly_report"
        assert defn.safety_hint.value == "read_only"
        assert len(defn.parameters) == 4

    @pytest.mark.asyncio
    async def test_missing_product_name(self):
        from aria.tools.mcp.report_tools import WeeklyReportTool
        tool = WeeklyReportTool()
        result = await tool.execute({"product_name": "", "period_label": "w1"})
        assert not result.success
        assert "비어있습니다" in result.error

    @pytest.mark.asyncio
    async def test_missing_period_label(self):
        from aria.tools.mcp.report_tools import WeeklyReportTool
        tool = WeeklyReportTool()
        result = await tool.execute({"product_name": "Test", "period_label": ""})
        assert not result.success

    @pytest.mark.asyncio
    async def test_basic_report(self):
        from aria.tools.mcp.report_tools import WeeklyReportTool
        tool = WeeklyReportTool()
        result = await tool.execute({
            "product_name": "Testorum",
            "period_label": "2026-W18",
            "checks": {"seo": {"issues": [], "total_pages": 3, "healthy_pages": 3}},
        })
        assert result.success
        assert result.output["product_name"] == "Testorum"
        assert "seo" in result.output["sections"]

    @pytest.mark.asyncio
    async def test_telegram_no_tokens(self):
        from aria.tools.mcp.report_tools import WeeklyReportTool
        tool = WeeklyReportTool()
        with patch.dict("os.environ", {}, clear=True):
            result = await tool.execute({
                "product_name": "Test",
                "period_label": "w1",
                "send_telegram": True,
            })
            assert result.success
            assert not result.output["telegram"]["success"]

    @pytest.mark.asyncio
    async def test_telegram_success(self):
        from aria.tools.mcp.report_tools import WeeklyReportTool
        tool = WeeklyReportTool()
        with patch.dict("os.environ", {
            "ARIA_TELEGRAM_BOT_TOKEN": "bot123",
            "ARIA_TELEGRAM_CHAT_ID": "456",
        }), patch("aria.telegram.notifier.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await tool.execute({
                "product_name": "Test",
                "period_label": "w1",
                "send_telegram": True,
            })
            assert result.success
            assert result.output["telegram"]["success"]

    @pytest.mark.asyncio
    async def test_llm_format(self):
        from aria.tools.mcp.report_tools import WeeklyReportTool
        tool = WeeklyReportTool()
        llm = tool.get_definition().to_llm_tool()
        assert llm["type"] == "function"
        assert llm["function"]["name"] == "weekly_report"
        assert "product_name" in llm["function"]["parameters"]["required"]
