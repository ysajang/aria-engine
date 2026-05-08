"""ARIA Engine - Workflow Definitions Tests

워크플로우 정의 빌드 + setup 통합 테스트:
- 각 빌더가 올바른 (WorkflowDefinition, functions) 반환
- 일정 파싱 정규식 (한국어 날짜/시간)
- setup_workflows 통합
- 워크플로우 실행 (mock 서비스)

실행: ARIA_ENV_FILE="" pytest tests/unit/test_workflow_definitions.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.workflows.types import (
    StepType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowStatus,
)
from aria.workflows.definitions import ALL_BUILDERS
from aria.workflows.definitions.competitor_monitor import build_competitor_monitor
from aria.workflows.definitions.viral_analysis import build_viral_analysis
from aria.workflows.definitions.schedule_manager import (
    build_schedule_manager,
    _parse_korean_datetime,
)
from aria.workflows.definitions.kpi_briefing import build_kpi_briefing
from aria.workflows.definitions.invoice_generator import build_invoice_generator
from aria.workflows.definitions.tax_summary import build_tax_summary
from aria.workflows.setup import setup_workflows


# ============================================================
# Builder 검증 — 모든 빌더가 올바른 형식 반환
# ============================================================


class TestAllBuilders:
    def test_all_builders_return_correct_format(self):
        """모든 빌더가 (WorkflowDefinition, dict) 반환"""
        for builder in ALL_BUILDERS:
            defn, funcs = builder()
            assert isinstance(defn, WorkflowDefinition), f"{builder.__name__} definition 타입 오류"
            assert isinstance(funcs, dict), f"{builder.__name__} functions 타입 오류"
            assert len(defn.steps) > 0, f"{builder.__name__} steps 비어있음"

    def test_all_builders_count(self):
        assert len(ALL_BUILDERS) == 10

    def test_no_duplicate_workflow_ids(self):
        ids = []
        for builder in ALL_BUILDERS:
            defn, _ = builder()
            ids.append(defn.workflow_id)
        assert len(ids) == len(set(ids)), f"중복 workflow_id: {ids}"

    def test_no_duplicate_function_names(self):
        """전체 빌더에서 함수명 중복 없음"""
        all_func_names = []
        for builder in ALL_BUILDERS:
            _, funcs = builder()
            all_func_names.extend(funcs.keys())
        assert len(all_func_names) == len(set(all_func_names)), f"중복 함수명: {all_func_names}"


# ============================================================
# #3 경쟁사 모니터링
# ============================================================


class TestCompetitorMonitor:
    def test_build(self):
        defn, funcs = build_competitor_monitor()
        assert defn.workflow_id == "competitor-monitor"
        assert defn.category == "marketing"
        assert len(defn.steps) == 3
        assert "competitor_search" in funcs
        assert "competitor_detect_changes" in funcs

    @pytest.mark.asyncio
    async def test_search_without_tools(self):
        """ToolRegistry 없이 실행 → 빈 결과"""
        _, funcs = build_competitor_monitor()
        ctx = WorkflowContext("test")
        result = await funcs["competitor_search"](ctx)
        assert result == []

    @pytest.mark.asyncio
    async def test_detect_changes_empty(self):
        """변화 없으면 빈 리스트"""
        _, funcs = build_competitor_monitor()
        ctx = WorkflowContext("test", initial_data={"search_results": []})
        result = await funcs["competitor_detect_changes"](ctx)
        assert result == []


# ============================================================
# #4 바이럴 분석
# ============================================================


class TestViralAnalysis:
    def test_build(self):
        defn, funcs = build_viral_analysis()
        assert defn.workflow_id == "viral-analysis"
        assert defn.category == "marketing"
        assert defn.scope == "testorum"
        assert "viral_aggregate" in funcs
        assert "viral_calculate" in funcs

    @pytest.mark.asyncio
    async def test_aggregate_no_store(self):
        """EventStore 없이 실행 → 기본값"""
        _, funcs = build_viral_analysis()
        ctx = WorkflowContext("test")
        result = await funcs["viral_aggregate"](ctx)
        assert result["completed"] == 0
        assert result["shared"] == 0

    @pytest.mark.asyncio
    async def test_calculate_metrics(self):
        """지표 계산 검증"""
        _, funcs = build_viral_analysis()
        ctx = WorkflowContext("test", initial_data={
            "raw_events": {
                "completed": 100,
                "shared": 25,
                "dropped": 30,
                "events": [],
            },
        })
        result = await funcs["viral_calculate"](ctx)
        assert result["stats"]["share_rate"] == 0.25
        assert result["stats"]["completed"] == 100


# ============================================================
# #8 일정 관리 — 정규식 파싱 테스트
# ============================================================


class TestScheduleParsing:
    def test_tomorrow_3pm(self):
        result = _parse_korean_datetime("내일 3시 미팅")
        assert result["parsed"] is True
        assert result["title"] == "미팅"

    def test_tomorrow_pm(self):
        result = _parse_korean_datetime("내일 오후 2시 미팅")
        assert result["parsed"] is True
        # 오후 2시 = 14시
        start = datetime.fromisoformat(result["start"])
        assert start.hour == 5 or start.hour == 14  # UTC or KST depending

    def test_day_after_tomorrow(self):
        result = _parse_korean_datetime("모레 10시 회의")
        assert result["parsed"] is True
        assert result["title"] == "회의"

    def test_date_slash(self):
        result = _parse_korean_datetime("5/10 14:00 팀 미팅")
        assert result["parsed"] is True
        assert "미팅" in result["title"]

    def test_iso_date(self):
        result = _parse_korean_datetime("2026-05-10 15:00 회의")
        assert result["parsed"] is True

    def test_next_week_tuesday(self):
        result = _parse_korean_datetime("다음주 화요일 10시 미팅")
        assert result["parsed"] is True
        assert result["title"] == "미팅"

    def test_hour_minute(self):
        result = _parse_korean_datetime("내일 3시 30분 점심")
        assert result["parsed"] is True
        start = datetime.fromisoformat(result["start"])
        # 30분이 들어있어야 함
        assert start.minute == 30

    def test_no_time_fails(self):
        result = _parse_korean_datetime("내일 미팅")
        assert result["parsed"] is False

    def test_empty_string(self):
        result = _parse_korean_datetime("")
        assert result["parsed"] is False

    def test_end_time_1hour_default(self):
        result = _parse_korean_datetime("내일 3시 미팅")
        assert result["parsed"] is True
        start = datetime.fromisoformat(result["start"])
        end = datetime.fromisoformat(result["end"])
        diff = end - start
        assert diff.total_seconds() == 3600  # 1시간


class TestScheduleManager:
    def test_build(self):
        defn, funcs = build_schedule_manager()
        assert defn.workflow_id == "schedule-manager"
        assert defn.category == "admin"
        assert "schedule_parse" in funcs

    @pytest.mark.asyncio
    async def test_parse_valid(self):
        _, funcs = build_schedule_manager()
        ctx = WorkflowContext("test", initial_data={"text": "내일 3시 미팅"})
        result = await funcs["schedule_parse"](ctx)
        assert result["parsed"] is True
        assert result["title"] == "미팅"

    @pytest.mark.asyncio
    async def test_parse_invalid(self):
        _, funcs = build_schedule_manager()
        ctx = WorkflowContext("test", initial_data={"text": "뭔가"})
        with pytest.raises(ValueError, match="파싱 실패"):
            await funcs["schedule_parse"](ctx)

    @pytest.mark.asyncio
    async def test_parse_empty(self):
        _, funcs = build_schedule_manager()
        ctx = WorkflowContext("test")
        with pytest.raises(ValueError, match="텍스트가 필요"):
            await funcs["schedule_parse"](ctx)


# ============================================================
# #9 주간 KPI 브리핑
# ============================================================


class TestKPIBriefing:
    def test_build(self):
        defn, funcs = build_kpi_briefing()
        assert defn.workflow_id == "kpi-briefing"
        assert defn.category == "admin"
        assert "kpi_collect" in funcs

    @pytest.mark.asyncio
    async def test_collect_no_services(self):
        """서비스 없이 실행 → 기본값"""
        _, funcs = build_kpi_briefing()
        ctx = WorkflowContext("test")
        result = await funcs["kpi_collect"](ctx)
        assert "products" in result
        assert len(result["products"]) == 3  # testorum, talksim, autotube
        assert result["daily_avg"] == 0.0


# ============================================================
# #10 인보이스 자동 생성
# ============================================================


class TestInvoiceGenerator:
    def test_build(self):
        defn, funcs = build_invoice_generator()
        assert defn.workflow_id == "invoice-generator"
        assert defn.category == "admin"
        assert "invoice_prepare" in funcs

    @pytest.mark.asyncio
    async def test_prepare_valid(self):
        _, funcs = build_invoice_generator()
        ctx = WorkflowContext("test", initial_data={
            "recipient": {"name": "고객사", "email": "client@test.com"},
            "items": [{"description": "웹 개발", "quantity": 1, "unit_price": 500000}],
        })
        result = await funcs["invoice_prepare"](ctx)
        assert result["total"] == 500000
        assert result["tax"] == 50000
        assert result["grand_total"] == 550000
        assert "INV-" in result["invoice_number"]

    @pytest.mark.asyncio
    async def test_prepare_no_tax(self):
        _, funcs = build_invoice_generator()
        ctx = WorkflowContext("test", initial_data={
            "recipient": {"name": "고객사", "email": "c@t.com"},
            "items": [{"description": "작업", "quantity": 1, "unit_price": 100000}],
            "include_tax": False,
        })
        result = await funcs["invoice_prepare"](ctx)
        assert result["tax"] == 0
        assert result["grand_total"] == 100000

    @pytest.mark.asyncio
    async def test_prepare_missing_data(self):
        _, funcs = build_invoice_generator()
        ctx = WorkflowContext("test")
        with pytest.raises(ValueError, match="recipient와 items"):
            await funcs["invoice_prepare"](ctx)


# ============================================================
# #7 세금 자료 정리
# ============================================================


class TestTaxSummary:
    def test_build(self):
        defn, funcs = build_tax_summary()
        assert defn.workflow_id == "tax-summary"
        assert defn.category == "admin"
        assert "tax_categorize" in funcs

    @pytest.mark.asyncio
    async def test_categorize_no_store(self):
        """EventStore 없이 실행 → 빈 카테고리"""
        _, funcs = build_tax_summary()
        ctx = WorkflowContext("test")
        result = await funcs["tax_categorize"](ctx)
        assert result["total_revenue"] == 0
        assert result["total_expense"] == 0
        assert result["vat"] == 0


# ============================================================
# setup_workflows 통합
# ============================================================


class TestSetupWorkflows:
    def test_setup_without_services(self):
        """서비스 없이 setup → 모든 워크플로우 등록"""
        registry = setup_workflows()
        assert registry.count == 10

    def test_setup_categories(self):
        registry = setup_workflows()
        marketing = registry.list_by_category("marketing")
        admin = registry.list_by_category("admin")
        assert len(marketing) == 5  # competitor-monitor, viral-analysis + seo/sns/email
        assert len(admin) == 5  # kpi, schedule, invoice, tax, revenue

    def test_setup_workflow_ids(self):
        registry = setup_workflows()
        ids = sorted(registry.list_ids())
        expected = sorted([
            "competitor-monitor",
            "viral-analysis",
            "revenue-report",
            "tax-summary",
            "schedule-manager",
            "kpi-briefing",
            "invoice-generator",
            "seo-content",
            "sns-posting",
            "email-campaign",
        ])
        assert ids == expected

    @pytest.mark.asyncio
    async def test_execute_kpi_briefing(self):
        """KPI 브리핑 실행 (서비스 없이)"""
        registry = setup_workflows()
        result = await registry.execute("kpi-briefing")
        assert result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL)
        assert "report" in result.outputs

    @pytest.mark.asyncio
    async def test_execute_viral_analysis(self):
        """바이럴 분석 실행 (서비스 없이)"""
        registry = setup_workflows()
        result = await registry.execute("viral-analysis")
        assert result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL)

    @pytest.mark.asyncio
    async def test_execute_invoice_with_data(self):
        """인보이스 실행 (데이터 제공)"""
        registry = setup_workflows()
        result = await registry.execute("invoice-generator", initial_data={
            "recipient": {"name": "고객", "email": "c@t.com"},
            "items": [{"description": "작업", "quantity": 1, "unit_price": 300000}],
        })
        # create-draft는 ToolRegistry 없어서 skip되지만 나머지 성공
        assert result.outputs.get("invoice_data", {}).get("total") == 300000

    @pytest.mark.asyncio
    async def test_execute_schedule_with_text(self):
        """일정 관리 실행 (파싱 가능 텍스트)"""
        registry = setup_workflows()
        result = await registry.execute("schedule-manager", initial_data={
            "text": "내일 3시 미팅",
        })
        event_data = result.outputs.get("event_data", {})
        assert event_data.get("title") == "미팅"
        assert event_data.get("parsed") is True

    @pytest.mark.asyncio
    async def test_execute_tax_summary(self):
        """세금 자료 실행 (서비스 없이)"""
        registry = setup_workflows()
        result = await registry.execute("tax-summary")
        assert result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL)

    @pytest.mark.asyncio
    async def test_execute_competitor_monitor(self):
        """경쟁사 모니터링 실행 (도구 없이 → 빈 결과)"""
        registry = setup_workflows()
        result = await registry.execute("competitor-monitor")
        assert result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL)
