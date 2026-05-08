"""ARIA Engine - Supabase MCP + Revenue Report Tests

실행: ARIA_ENV_FILE="" pytest tests/unit/test_supabase_mcp.py -v
"""

from __future__ import annotations

import pytest

from aria.mcp.supabase_servers import get_supabase_mcp_config, SUPABASE_MCP_BASE_URL
from aria.mcp.types import MCPAuthType
from aria.workflows.definitions.revenue_report import build_revenue_report
from aria.workflows.types import WorkflowContext, WorkflowStatus
from aria.workflows.setup import setup_workflows


# ============================================================
# Supabase MCP Config
# ============================================================


class TestSupabaseMCPConfig:
    def test_config_basic(self):
        config = get_supabase_mcp_config("abc123", project_name="testorum")
        assert config.name == "supabase_testorum"
        assert "project_ref=abc123" in config.url
        assert "read_only=true" in config.url
        assert config.auth_type == MCPAuthType.API_KEY
        assert config.tool_prefix == "mcp_sb_testorum_"

    def test_config_default_name(self):
        config = get_supabase_mcp_config("abc", project_name="supabase")
        assert config.name == "supabase_supabase"
        assert config.tool_prefix == "mcp_sb_supabase_"

    def test_config_read_only_default(self):
        config = get_supabase_mcp_config("test")
        assert "read_only=true" in config.url

    def test_config_read_only_false(self):
        config = get_supabase_mcp_config("test", read_only=False)
        assert "read_only" not in config.url

    def test_config_with_features(self):
        config = get_supabase_mcp_config("test", features="database")
        assert "features=database" in config.url

    def test_config_url_format(self):
        config = get_supabase_mcp_config("my-project-ref")
        assert config.url.startswith(SUPABASE_MCP_BASE_URL)


class TestSupabaseMCPMultiProject:
    def test_multi_configs(self):
        from aria.mcp.supabase_servers import get_supabase_mcp_configs
        projects = {"testorum": "ref1", "talksim": "ref2", "n9": "ref3"}
        configs = get_supabase_mcp_configs(projects)
        assert len(configs) == 3
        names = {c.name for c in configs}
        assert names == {"supabase_testorum", "supabase_talksim", "supabase_n9"}
        prefixes = {c.tool_prefix for c in configs}
        assert prefixes == {"mcp_sb_testorum_", "mcp_sb_talksim_", "mcp_sb_n9_"}


class TestSupabaseMCPConfigEnv:
    def test_config_class_exists(self):
        from aria.core.config import SupabaseMCPConfig
        cfg = SupabaseMCPConfig()
        assert cfg.enabled is True
        assert cfg.read_only is True
        assert cfg.access_token == ""
        assert cfg.project_ref == ""
        assert cfg.projects == ""

    def test_not_configured_without_token(self):
        from aria.core.config import SupabaseMCPConfig
        cfg = SupabaseMCPConfig()
        assert cfg.is_configured is False

    def test_parsed_projects_from_projects_str(self):
        from aria.core.config import SupabaseMCPConfig
        cfg = SupabaseMCPConfig(
            access_token="test",
            projects="testorum:ref1,talksim:ref2",
        )
        assert cfg.parsed_projects == {"testorum": "ref1", "talksim": "ref2"}
        assert cfg.is_configured is True

    def test_parsed_projects_fallback_to_ref(self):
        from aria.core.config import SupabaseMCPConfig
        cfg = SupabaseMCPConfig(
            access_token="test",
            project_ref="single-ref",
        )
        assert cfg.parsed_projects == {"default": "single-ref"}

    def test_aria_config_has_supabase(self):
        from aria.core.config import AriaConfig
        cfg = AriaConfig()
        assert hasattr(cfg, "supabase")
        assert cfg.supabase.read_only is True


# ============================================================
# Revenue Report Workflow
# ============================================================


class TestRevenueReport:
    def test_build(self):
        defn, funcs = build_revenue_report()
        assert defn.workflow_id == "revenue-report"
        assert defn.category == "admin"
        assert len(defn.steps) == 3
        assert "revenue_query" in funcs
        assert "revenue_calculate" in funcs

    @pytest.mark.asyncio
    async def test_query_no_tools(self):
        """ToolRegistry 없이 → 빈 결과"""
        _, funcs = build_revenue_report()
        ctx = WorkflowContext("test")
        result = await funcs["revenue_query"](ctx)
        assert result["revenue_rows"] == []
        assert result["expense_rows"] == []

    @pytest.mark.asyncio
    async def test_calculate_summary(self):
        """합계 계산"""
        _, funcs = build_revenue_report()
        ctx = WorkflowContext("test", initial_data={
            "revenue_rows": [
                {"category": "서비스", "amount": 500000, "count": 5},
                {"category": "광고", "amount": 100000, "count": 10},
            ],
            "expense_rows": [
                {"category": "서버", "amount": 50000, "count": 1},
            ],
        })
        result = await funcs["revenue_calculate"](ctx)
        assert result["total_revenue"] == 600000
        assert result["total_expense"] == 50000
        assert result["net_income"] == 550000
        assert result["margin_pct"] > 90

    @pytest.mark.asyncio
    async def test_calculate_zero_revenue(self):
        _, funcs = build_revenue_report()
        ctx = WorkflowContext("test", initial_data={
            "revenue_rows": [],
            "expense_rows": [],
        })
        result = await funcs["revenue_calculate"](ctx)
        assert result["total_revenue"] == 0
        assert result["margin_pct"] == 0.0


# ============================================================
# Setup 통합 — revenue-report 포함
# ============================================================


class TestSetupWithRevenue:
    def test_setup_includes_revenue(self):
        registry = setup_workflows()
        assert registry.count == 10  # 기존 6 + revenue-report + seo/sns/email
        assert registry.get("revenue-report") is not None

    def test_admin_category_count(self):
        registry = setup_workflows()
        admin = registry.list_by_category("admin")
        assert len(admin) == 5  # kpi + schedule + invoice + tax + revenue

    @pytest.mark.asyncio
    async def test_execute_revenue_no_tools(self):
        registry = setup_workflows()
        result = await registry.execute("revenue-report")
        # query는 빈 결과 반환 → 리포트는 "매출 기록 없음"
        assert result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL)
        report = result.outputs.get("report", "")
        assert "매출 기록 없음" in report or "매출" in report
