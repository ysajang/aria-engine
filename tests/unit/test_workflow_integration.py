"""ARIA Engine - Workflow Integration Tests (Step 3)

API 엔드포인트 + 텔레그램 핸들러 + cron 스크립트 테스트

실행: ARIA_ENV_FILE="" pytest tests/unit/test_workflow_integration.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.telegram.client import ARIAClient
from aria.telegram.handlers import ARIAHandlers


# ============================================================
# ARIAClient — 워크플로우 메서드 존재 확인
# ============================================================


class TestARIAClientWorkflowMethods:
    def test_has_list_workflows(self):
        client = ARIAClient("http://localhost:8100")
        assert hasattr(client, "list_workflows")
        assert callable(client.list_workflows)

    def test_has_execute_workflow(self):
        client = ARIAClient("http://localhost:8100")
        assert hasattr(client, "execute_workflow")
        assert callable(client.execute_workflow)


# ============================================================
# ARIAHandlers — 워크플로우 핸들러 존재 확인
# ============================================================


class TestARIAHandlersWorkflowCommands:
    @pytest.fixture
    def handlers(self):
        client = AsyncMock(spec=ARIAClient)
        return ARIAHandlers(
            aria_client=client,
            allowed_chat_id="12345",
        )

    def test_has_workflows_command(self, handlers):
        assert hasattr(handlers, "workflows_command")

    def test_has_marketing_command(self, handlers):
        assert hasattr(handlers, "marketing_command")

    def test_has_admin_command(self, handlers):
        assert hasattr(handlers, "admin_command")

    @pytest.mark.asyncio
    async def test_workflows_unauthorized(self, handlers):
        """인증 실패 시 무시"""
        update = MagicMock()
        update.effective_chat.id = 99999  # 다른 chat_id
        context = MagicMock()

        await handlers.workflows_command(update, context)
        # 클라이언트 호출 없어야 함
        handlers.client.list_workflows.assert_not_called()

    @pytest.mark.asyncio
    async def test_workflows_list(self, handlers):
        """워크플로우 목록 조회"""
        handlers.client.list_workflows.return_value = {
            "workflows": [
                {"workflow_id": "kpi-briefing", "name": "KPI", "description": "주간 KPI", "category": "admin"},
            ],
            "total": 1,
        }

        update = MagicMock()
        update.effective_chat.id = 12345
        update.message = AsyncMock()
        context = MagicMock()
        context.args = []

        await handlers.workflows_command(update, context)
        handlers.client.list_workflows.assert_called_once_with(None)
        update.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_marketing_no_args_shows_list(self, handlers):
        """인자 없이 /marketing → 목록 표시"""
        handlers.client.list_workflows.return_value = {
            "workflows": [
                {"workflow_id": "competitor-monitor", "description": "경쟁사", "category": "marketing"},
            ],
            "total": 1,
        }

        update = MagicMock()
        update.effective_chat.id = 12345
        update.message = AsyncMock()
        context = MagicMock()
        context.args = []

        await handlers.marketing_command(update, context)
        handlers.client.list_workflows.assert_called_with("marketing")

    @pytest.mark.asyncio
    async def test_admin_execute_workflow(self, handlers):
        """/admin kpi-briefing → 워크플로우 실행"""
        handlers.client.execute_workflow.return_value = {
            "workflow_id": "kpi-briefing",
            "status": "completed",
            "steps_completed": 2,
            "steps_total": 2,
            "duration_ms": 100,
            "report": "📋 KPI 리포트...",
        }

        update = MagicMock()
        update.effective_chat.id = 12345
        update.message = AsyncMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["kpi-briefing"]

        await handlers.admin_command(update, context)
        handlers.client.execute_workflow.assert_called_once_with("kpi-briefing", {})

    @pytest.mark.asyncio
    async def test_admin_schedule_with_text(self, handlers):
        """/admin schedule 내일 3시 미팅 → text 전달"""
        handlers.client.execute_workflow.return_value = {
            "workflow_id": "schedule-manager",
            "status": "completed",
            "steps_completed": 3,
            "steps_total": 3,
            "duration_ms": 50,
            "report": "📅 일정 등록 완료",
        }

        update = MagicMock()
        update.effective_chat.id = 12345
        update.message = AsyncMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["schedule-manager", "내일", "3시", "미팅"]

        await handlers.admin_command(update, context)
        handlers.client.execute_workflow.assert_called_once_with(
            "schedule-manager",
            {"text": "내일 3시 미팅"},
        )


# ============================================================
# Cron 스크립트 — 모듈 로드 확인
# ============================================================


class TestCronScript:
    def test_script_importable(self):
        """cron 스크립트가 import 가능"""
        import importlib
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "aria_workflow_cron",
            "scripts/aria_workflow_cron.py",
        )
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert hasattr(module, "main")


# ============================================================
# Bot — 핸들러 등록 확인
# ============================================================


class TestBotHandlerRegistration:
    def test_workflow_handlers_in_create_bot_code(self):
        """bot.py에 워크플로우 핸들러 등록 코드 존재"""
        import inspect
        from aria.telegram import bot

        source = inspect.getsource(bot.create_bot)
        assert "workflows" in source
        assert "marketing" in source
        assert "admin" in source
