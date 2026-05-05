"""ARIA Engine - 추가 모니터링 4종 테스트

테스트 대상:
1. frontend_checks.py — 프론트엔드 에러 분석 (AI 미사용)
2. webhook_checks.py — 웹훅 reconciliation (AI 미사용)
3. contract_checks.py — API contract testing (AI 미사용)
4. 도구 래퍼 3종 — ToolExecutor 인터페이스 검증
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# === Frontend Checks Tests ===

from aria.monitoring.frontend_checks import (
    _should_ignore,
    _extract_error_signature,
    _normalize_stack,
    analyze_frontend_errors,
    detect_error_spike,
)


def _make_frontend_event(
    message: str = "TypeError: Cannot read property 'x' of undefined",
    url: str = "https://testorum.app/tests/t01",
    stack: str = "at Component.render (app-abc123.js:42:15)",
    browser: str = "Chrome 120",
    minutes_ago: int = 5,
) -> dict[str, Any]:
    """테스트용 프론트엔드 에러 이벤트 생성"""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "event_type": "frontend_error",
        "source": "testorum",
        "timestamp": ts,
        "data": {
            "message": message,
            "url": url,
            "stack": stack,
            "browser": browser,
        },
    }


class TestShouldIgnore:
    def test_ignores_chrome_extension(self) -> None:
        assert _should_ignore("chrome-extension://abc/content.js error") is True

    def test_ignores_resize_observer(self) -> None:
        assert _should_ignore("ResizeObserver loop limit exceeded") is True

    def test_ignores_script_error(self) -> None:
        assert _should_ignore("Script error.") is True

    def test_ignores_adsense(self) -> None:
        assert _should_ignore("adsbygoogle is not defined") is True

    def test_keeps_real_error(self) -> None:
        assert _should_ignore("TypeError: Cannot read property 'x'") is False

    def test_keeps_custom_error(self) -> None:
        assert _should_ignore("PaywallGate: credit deduction failed") is False


class TestNormalizeStack:
    def test_removes_hash(self) -> None:
        result = _normalize_stack("at fn (app-a1b2c3d4.js:10:5)")
        assert "[hash]" in result
        assert "a1b2c3d4" not in result

    def test_removes_line_numbers(self) -> None:
        result = _normalize_stack("at fn (app.js:42:15)")
        assert ":*:*" in result
        assert ":42:15" not in result

    def test_truncates_long_stack(self) -> None:
        long_stack = "a" * 1000
        result = _normalize_stack(long_stack)
        assert len(result) <= 500


class TestExtractErrorSignature:
    def test_with_stack(self) -> None:
        error = {"message": "TypeError", "stack": "at fn (app.js:1:2)\nat main (index.js:3:4)"}
        sig = _extract_error_signature(error)
        assert "TypeError" in sig
        assert "app" in sig

    def test_without_stack(self) -> None:
        error = {"message": "ReferenceError", "url": "/tests/t01"}
        sig = _extract_error_signature(error)
        assert "ReferenceError" in sig
        assert "/tests/t01" in sig

    def test_same_error_same_signature(self) -> None:
        e1 = {"message": "TypeError", "stack": "at fn (app.js:10:5)"}
        e2 = {"message": "TypeError", "stack": "at fn (app.js:20:3)"}
        # 라인 번호만 다르면 같은 시그니처
        assert _extract_error_signature(e1) == _extract_error_signature(e2)


class TestAnalyzeFrontendErrors:
    def test_empty_events(self) -> None:
        result = analyze_frontend_errors([])
        assert result["total_events"] == 0
        assert result["filtered_events"] == 0
        assert result["severity"] == "none"

    def test_filters_ignored_errors(self) -> None:
        events = [
            _make_frontend_event(message="Script error."),
            _make_frontend_event(message="Real error"),
        ]
        result = analyze_frontend_errors(events)
        assert result["ignored_events"] == 1
        assert result["filtered_events"] == 1

    def test_groups_similar_errors(self) -> None:
        events = [
            _make_frontend_event(message="TypeError", stack="at fn (app.js:10:5)"),
            _make_frontend_event(message="TypeError", stack="at fn (app.js:20:3)"),
            _make_frontend_event(message="ReferenceError", stack="at other (lib.js:1:1)"),
        ]
        result = analyze_frontend_errors(events)
        assert len(result["groups"]) == 2
        # TypeError 그룹이 2건으로 1위
        assert result["groups"][0]["count"] == 2

    def test_high_severity(self) -> None:
        events = [_make_frontend_event() for _ in range(55)]
        result = analyze_frontend_errors(events)
        assert result["severity"] == "high"

    def test_medium_severity(self) -> None:
        events = [_make_frontend_event() for _ in range(25)]
        result = analyze_frontend_errors(events)
        assert result["severity"] == "medium"

    def test_top_urls_tracked(self) -> None:
        events = [
            _make_frontend_event(url="https://testorum.app/tests/t01"),
            _make_frontend_event(url="https://testorum.app/tests/t01"),
            _make_frontend_event(url="https://testorum.app/profile"),
        ]
        result = analyze_frontend_errors(events)
        assert len(result["top_urls"]) >= 1
        assert result["top_urls"][0]["url"] == "https://testorum.app/tests/t01"

    def test_dominant_error_detected(self) -> None:
        events = [
            _make_frontend_event(message="Same error", stack="at same (a.js:1:1)"),
        ] * 8 + [
            _make_frontend_event(message="Other error", stack="at other (b.js:1:1)"),
        ] * 2
        result = analyze_frontend_errors(events)
        # 같은 에러가 80% 차지 → issue 발생
        dominant_issues = [i for i in result["issues"] if "단일 에러" in i["message"]]
        assert len(dominant_issues) == 1


class TestDetectErrorSpike:
    def test_no_spike(self) -> None:
        events = [_make_frontend_event(minutes_ago=1) for _ in range(3)]
        result = detect_error_spike(events, threshold=10)
        assert result["is_spike"] is False

    def test_spike_detected(self) -> None:
        events = [_make_frontend_event(minutes_ago=1) for _ in range(15)]
        result = detect_error_spike(events, threshold=10)
        assert result["is_spike"] is True
        assert result["severity"] == "medium"

    def test_high_spike(self) -> None:
        events = [_make_frontend_event(minutes_ago=1) for _ in range(35)]
        result = detect_error_spike(events, threshold=10)
        assert result["severity"] == "high"

    def test_old_events_excluded(self) -> None:
        events = [_make_frontend_event(minutes_ago=60) for _ in range(50)]
        result = detect_error_spike(events, window_minutes=10, threshold=5)
        assert result["is_spike"] is False


# === Contract Checks Tests ===

from aria.monitoring.contract_checks import validate_schema, check_endpoint


class TestValidateSchema:
    def test_valid_string(self) -> None:
        errors = validate_schema("hello", {"type": "string"})
        assert errors == []

    def test_invalid_type(self) -> None:
        errors = validate_schema(42, {"type": "string"})
        assert len(errors) == 1
        assert "타입 불일치" in errors[0]

    def test_required_fields(self) -> None:
        data = {"name": "test"}
        schema = {"type": "object", "required": ["name", "email"]}
        errors = validate_schema(data, schema)
        assert len(errors) == 1
        assert "email" in errors[0]

    def test_nested_object(self) -> None:
        data = {"user": {"name": 123}}
        schema = {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                },
            },
        }
        errors = validate_schema(data, schema)
        assert len(errors) == 1
        assert "$.user.name" in errors[0]

    def test_array_items(self) -> None:
        data = [1, "two", 3]
        schema = {"type": "array", "items": {"type": "integer"}}
        errors = validate_schema(data, schema)
        assert len(errors) == 1
        assert "[1]" in errors[0]

    def test_enum_valid(self) -> None:
        errors = validate_schema("active", {"type": "string", "enum": ["active", "paused"]})
        assert errors == []

    def test_enum_invalid(self) -> None:
        errors = validate_schema("deleted", {"type": "string", "enum": ["active", "paused"]})
        assert len(errors) == 1

    def test_string_length(self) -> None:
        errors = validate_schema("ab", {"type": "string", "minLength": 3})
        assert len(errors) == 1

    def test_number_range(self) -> None:
        errors = validate_schema(150, {"type": "integer", "maximum": 100})
        assert len(errors) == 1

    def test_union_type(self) -> None:
        errors = validate_schema(None, {"type": ["string", "null"]})
        assert errors == []

    def test_boolean_not_integer(self) -> None:
        errors = validate_schema(True, {"type": "integer"})
        assert len(errors) == 1

    def test_full_valid_object(self) -> None:
        data = {
            "status": "ok",
            "count": 5,
            "items": [{"id": "a"}, {"id": "b"}],
        }
        schema = {
            "type": "object",
            "required": ["status", "count"],
            "properties": {
                "status": {"type": "string", "enum": ["ok", "error"]},
                "count": {"type": "integer", "minimum": 0},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {"id": {"type": "string"}},
                    },
                },
            },
        }
        errors = validate_schema(data, schema)
        assert errors == []


@pytest.mark.asyncio
class TestCheckEndpoint:
    async def test_connection_error_handled(self) -> None:
        """연결 불가 시 에러 처리"""
        result = await check_endpoint(
            url="http://127.0.0.1:1",  # 연결 불가 포트
            timeout=1.0,
        )
        assert result["passed"] is False
        assert len(result["issues"]) > 0

    async def test_result_structure(self) -> None:
        """결과 구조 검증 (네트워크 무관)"""
        result = await check_endpoint(
            url="http://127.0.0.1:1",
            timeout=0.5,
        )
        assert "url" in result
        assert "method" in result
        assert "passed" in result
        assert "status_code" in result
        assert "response_time_ms" in result
        assert "schema_errors" in result
        assert "issues" in result
        assert "checked_at" in result


# === Tool Executor Tests ===

from aria.tools.mcp.frontend_monitor_tools import FrontendErrorAnalyzeTool
from aria.tools.mcp.webhook_monitor_tools import WebhookReconciliationTool
from aria.tools.mcp.contract_test_tools import ApiContractTestTool


class TestFrontendErrorAnalyzeToolDefinition:
    def test_tool_definition(self) -> None:
        tool = FrontendErrorAnalyzeTool()
        defn = tool.get_definition()
        assert defn.name == "frontend_error_analyze"
        assert defn.safety_hint == SafetyLevelHint.READ_ONLY
        param_names = [p.name for p in defn.parameters]
        assert "product_id" in param_names

    @pytest.mark.asyncio
    async def test_missing_product_id(self) -> None:
        tool = FrontendErrorAnalyzeTool()
        result = await tool.execute({})
        assert result.success is False
        assert "product_id" in (result.error or "")

    @pytest.mark.asyncio
    async def test_missing_event_store(self) -> None:
        tool = FrontendErrorAnalyzeTool(event_store=None)
        result = await tool.execute({"product_id": "testorum"})
        assert result.success is False


class TestWebhookReconciliationToolDefinition:
    def test_tool_definition(self) -> None:
        tool = WebhookReconciliationTool()
        defn = tool.get_definition()
        assert defn.name == "webhook_reconciliation"
        assert defn.safety_hint == SafetyLevelHint.READ_ONLY
        param_names = [p.name for p in defn.parameters]
        assert "provider" in param_names
        assert "api_key" in param_names

    @pytest.mark.asyncio
    async def test_missing_params(self) -> None:
        tool = WebhookReconciliationTool()
        result = await tool.execute({"provider": "lemonsqueezy"})
        assert result.success is False


class TestApiContractTestToolDefinition:
    def test_tool_definition(self) -> None:
        tool = ApiContractTestTool()
        defn = tool.get_definition()
        assert defn.name == "api_contract_test"
        assert defn.safety_hint == SafetyLevelHint.READ_ONLY
        param_names = [p.name for p in defn.parameters]
        assert "url" in param_names

    @pytest.mark.asyncio
    async def test_missing_url(self) -> None:
        tool = ApiContractTestTool()
        result = await tool.execute({})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_invalid_schema_json(self) -> None:
        tool = ApiContractTestTool()
        result = await tool.execute({
            "url": "https://example.com",
            "expected_schema": "not json",
        })
        assert result.success is False
        assert "JSON" in (result.error or "")

    @pytest.mark.asyncio
    async def test_llm_tool_format(self) -> None:
        """도구 정의가 LLM function calling 포맷으로 변환되는지 확인"""
        tool = ApiContractTestTool()
        defn = tool.get_definition()
        llm_format = defn.to_llm_tool()
        assert llm_format["type"] == "function"
        assert llm_format["function"]["name"] == "api_contract_test"
        assert "parameters" in llm_format["function"]


# === Webhook Checks Logic Tests ===

from aria.monitoring.webhook_checks import (
    DEFAULT_LOOKBACK_MINUTES,
    DEFAULT_GRACE_MINUTES,
)


class TestWebhookChecksDefaults:
    def test_lookback_default(self) -> None:
        assert DEFAULT_LOOKBACK_MINUTES == 30

    def test_grace_default(self) -> None:
        assert DEFAULT_GRACE_MINUTES == 5


# === Integration-style Tests (no real HTTP) ===

from aria.tools.tool_types import SafetyLevelHint


class TestAllToolsNoLLMDependency:
    """모든 모니터링 도구가 LLM import 없이 동작하는지 확인"""

    def test_frontend_no_llm_import(self) -> None:
        import inspect
        source = inspect.getsource(FrontendErrorAnalyzeTool)
        assert "llm_provider" not in source
        assert "LLMProvider" not in source
        assert "litellm" not in source

    def test_webhook_no_llm_import(self) -> None:
        import inspect
        source = inspect.getsource(WebhookReconciliationTool)
        assert "llm_provider" not in source
        assert "LLMProvider" not in source

    def test_contract_no_llm_import(self) -> None:
        import inspect
        source = inspect.getsource(ApiContractTestTool)
        assert "llm_provider" not in source
        assert "LLMProvider" not in source

    def test_frontend_checks_no_llm(self) -> None:
        import inspect
        from aria.monitoring import frontend_checks
        source = inspect.getsource(frontend_checks)
        assert "llm_provider" not in source
        assert "litellm" not in source

    def test_webhook_checks_no_llm(self) -> None:
        import inspect
        from aria.monitoring import webhook_checks
        source = inspect.getsource(webhook_checks)
        assert "llm_provider" not in source
        assert "litellm" not in source

    def test_contract_checks_no_llm(self) -> None:
        import inspect
        from aria.monitoring import contract_checks
        source = inspect.getsource(contract_checks)
        assert "llm_provider" not in source
        assert "litellm" not in source
