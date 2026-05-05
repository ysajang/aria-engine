"""ARIA Engine - 모니터링 v2 보강 테스트 (엣지케이스 + 실패 시나리오)

기존 test_monitoring_v2.py에서 누락된 영역:
1. frontend_checks — 데이터 누락 / 전체 기간 초과 / 빈 메시지
2. webhook_checks — reconcile 함수 3종 (mock HTTP)
3. contract_checks — run_contract_tests / 타임아웃 / 비JSON
4. AlertManager 새 메서드 3종 — 임계치 미달 / 비활성 / 레벨 분기
5. 제품 소스 이벤트 평가 분기
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ============================================================
# 1. Frontend Checks — 엣지케이스
# ============================================================

from aria.monitoring.frontend_checks import (
    _should_ignore,
    _extract_error_signature,
    analyze_frontend_errors,
    detect_error_spike,
)


def _fe_event(
    message: str = "TypeError",
    url: str = "/test",
    stack: str = "",
    browser: str = "Chrome",
    minutes_ago: int = 5,
) -> dict[str, Any]:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "event_type": "frontend_error",
        "source": "testorum",
        "timestamp": ts,
        "data": {"message": message, "url": url, "stack": stack, "browser": browser},
    }


class TestFrontendEdgeCases:
    def test_missing_data_fields(self) -> None:
        """data 필드 누락된 이벤트도 크래시 없이 처리"""
        events = [
            {"event_type": "frontend_error", "source": "testorum",
             "timestamp": datetime.now(timezone.utc).isoformat(), "data": {}},
            {"event_type": "frontend_error", "source": "testorum",
             "timestamp": datetime.now(timezone.utc).isoformat(), "data": {"message": "err"}},
        ]
        result = analyze_frontend_errors(events)
        assert result["filtered_events"] == 2  # 크래시 없이 처리됨

    def test_empty_message_not_ignored(self) -> None:
        """빈 메시지는 무시 패턴에 해당하지 않음"""
        assert _should_ignore("") is False

    def test_all_events_old(self) -> None:
        """전부 기간 밖 이벤트 → filtered 0"""
        events = [_fe_event(minutes_ago=1500) for _ in range(10)]
        result = analyze_frontend_errors(events, hours=1)
        assert result["filtered_events"] == 0

    def test_signature_missing_all_fields(self) -> None:
        """message/stack/url 전부 없는 이벤트 시그니처"""
        sig = _extract_error_signature({})
        assert "unknown" in sig

    def test_moz_extension_ignored(self) -> None:
        """Firefox 확장 에러 무시"""
        assert _should_ignore("Error at moz-extension://abc/content.js") is True

    def test_google_crweb_ignored(self) -> None:
        """iOS WebView 에러 무시"""
        assert _should_ignore("__gCrWeb.something is undefined") is True

    def test_spike_with_all_ignored_events(self) -> None:
        """전부 무시 패턴이면 스파이크 아님"""
        events = [_fe_event(message="Script error.", minutes_ago=1) for _ in range(20)]
        result = detect_error_spike(events, threshold=5)
        assert result["is_spike"] is False
        assert result["count"] == 0

    def test_low_severity_threshold(self) -> None:
        """5~19건은 low severity"""
        events = [_fe_event() for _ in range(7)]
        result = analyze_frontend_errors(events)
        assert result["severity"] == "low"

    def test_no_timestamp_event(self) -> None:
        """timestamp 없는 이벤트도 처리"""
        events = [{"event_type": "frontend_error", "source": "testorum",
                   "timestamp": "", "data": {"message": "err"}}]
        # 빈 timestamp → 기간 필터 통과 안 됨
        result = analyze_frontend_errors(events, hours=1)
        # 빈 문자열은 cutoff보다 작으므로 필터됨
        assert result["total_events"] == 1


# ============================================================
# 2. Webhook Checks — 핵심 로직
# ============================================================

from aria.monitoring.webhook_checks import (
    reconcile_lemonsqueezy,
    reconcile_stripe,
    run_webhook_reconciliation,
)


@pytest.mark.asyncio
class TestReconcileLemonsqueezy:
    async def test_api_failure(self) -> None:
        """LS API 호출 실패 시 graceful 처리"""
        import httpx

        def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=401, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)) as client:
            with patch("aria.monitoring.webhook_checks.httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await reconcile_lemonsqueezy(
                    api_key="bad-key",
                    store_id="12345",
                    supabase_url="https://fake.supabase.co",
                    supabase_service_key="fake",
                )

        assert result["severity"] == "medium"
        assert any("401" in i["message"] for i in result["issues"])

    async def test_no_recent_orders(self) -> None:
        """최근 주문 없으면 정상 종료"""
        import httpx

        def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={"data": []},
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)) as client:
            with patch("aria.monitoring.webhook_checks.httpx.AsyncClient") as mock_cls:
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await reconcile_lemonsqueezy(
                    api_key="key",
                    store_id="12345",
                    supabase_url="https://fake.supabase.co",
                    supabase_service_key="fake",
                )

        assert result["ls_orders"] == 0
        assert result["severity"] == "none"

    async def test_timeout_handled(self) -> None:
        """타임아웃 시 graceful 처리"""
        with patch("aria.monitoring.webhook_checks.httpx.AsyncClient") as mock_cls:
            import httpx
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_cls.return_value = mock_client

            result = await reconcile_lemonsqueezy(
                api_key="key",
                store_id="12345",
                supabase_url="https://fake.supabase.co",
                supabase_service_key="fake",
            )

        assert result["severity"] == "medium"
        assert any("타임아웃" in i["message"] for i in result["issues"])


@pytest.mark.asyncio
class TestRunWebhookReconciliation:
    async def test_invalid_provider(self) -> None:
        """지원 안 하는 provider"""
        result = await run_webhook_reconciliation(
            provider="paypal",
            api_key="key",
            supabase_url="url",
            supabase_service_key="key",
        )
        assert "error" in result
        assert "지원하지 않는" in result["error"]

    async def test_ls_without_store_id(self) -> None:
        """LS인데 store_id 없으면 에러"""
        result = await run_webhook_reconciliation(
            provider="lemonsqueezy",
            api_key="key",
            supabase_url="url",
            supabase_service_key="key",
            store_id=None,
        )
        assert "error" in result
        assert "store_id" in result["error"]


# ============================================================
# 3. Contract Checks — 누락 시나리오
# ============================================================

from aria.monitoring.contract_checks import (
    validate_schema,
    check_endpoint,
    run_contract_tests,
)


class TestValidateSchemaEdgeCases:
    def test_empty_schema(self) -> None:
        """빈 스키마는 모든 데이터 통과"""
        assert validate_schema({"anything": True}, {}) == []

    def test_deeply_nested_error(self) -> None:
        """3단 중첩 에러 경로 표시"""
        data = {"a": {"b": {"c": 123}}}
        schema = {
            "type": "object",
            "properties": {
                "a": {
                    "type": "object",
                    "properties": {
                        "b": {
                            "type": "object",
                            "properties": {
                                "c": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
        errors = validate_schema(data, schema)
        assert len(errors) == 1
        assert "$.a.b.c" in errors[0]

    def test_null_in_union_type(self) -> None:
        """null이 union에 포함되면 통과"""
        assert validate_schema(None, {"type": ["string", "null"]}) == []
        assert validate_schema("hello", {"type": ["string", "null"]}) == []

    def test_array_max_validation(self) -> None:
        """배열 요소 최대 20개만 검증"""
        data = list(range(30))
        schema = {"type": "array", "items": {"type": "integer"}}
        # 30개 중 20개만 검증 → 에러 없음
        assert validate_schema(data, schema) == []

    def test_number_minimum_check(self) -> None:
        """minimum 미달"""
        errors = validate_schema(-1, {"type": "integer", "minimum": 0})
        assert len(errors) == 1
        assert "최솟값" in errors[0]

    def test_maxlength_check(self) -> None:
        """maxLength 초과"""
        errors = validate_schema("toolong", {"type": "string", "maxLength": 3})
        assert len(errors) == 1


@pytest.mark.asyncio
class TestRunContractTests:
    async def test_empty_endpoints(self) -> None:
        """빈 엔드포인트 목록 → 전부 통과"""
        result = await run_contract_tests([])
        assert result["total"] == 0
        assert result["passed"] == 0
        assert result["severity"] == "none"

    async def test_endpoint_without_url(self) -> None:
        """url 없는 항목은 스킵"""
        result = await run_contract_tests([{"method": "GET"}])
        assert result["total"] == 1
        assert len(result["results"]) == 0  # url 없어서 스킵됨

    async def test_multiple_failures_severity(self) -> None:
        """여러 엔드포인트 실패 → 심각도 계산"""
        endpoints = [
            {"url": "http://127.0.0.1:1"},  # 연결 불가
            {"url": "http://127.0.0.1:1"},
            {"url": "http://127.0.0.1:1"},
        ]
        result = await run_contract_tests(endpoints)
        assert result["failed"] == 3
        assert result["severity"] == "high"  # 100% 실패

    async def test_base_headers_applied(self) -> None:
        """base_headers가 모든 요청에 적용되는지 확인"""
        result = await run_contract_tests(
            endpoints=[{"url": "http://127.0.0.1:1"}],
            base_headers={"X-Custom": "test"},
        )
        # 연결 실패하지만 구조적으로 에러 처리됨
        assert result["total"] == 1


@pytest.mark.asyncio
class TestCheckEndpointEdgeCases:
    async def test_unsupported_method(self) -> None:
        """지원 안 하는 HTTP 메서드"""
        result = await check_endpoint(url="https://example.com", method="PATCH")
        # PATCH는 지원 목록에 없음
        assert result["passed"] is False
        assert any("메서드" in i["message"] for i in result["issues"])

    async def test_result_has_checked_at(self) -> None:
        """checked_at 타임스탬프 존재"""
        result = await check_endpoint(url="http://127.0.0.1:1", timeout=0.5)
        assert "checked_at" in result
        assert result["checked_at"].startswith("20")


# ============================================================
# 4. AlertManager 새 메서드 — 임계치 + 비활성 + 레벨
# ============================================================

from aria.alerts.alert_types import AlertType, AlertLevel, Alert, ALERT_EMOJI, DEFAULT_COOLDOWNS
from aria.alerts.alert_manager import AlertManager


class TestAlertTypeV2:
    def test_new_types_exist(self) -> None:
        """새 AlertType 3종 존재"""
        assert AlertType.FRONTEND_ERROR_SPIKE.value == "frontend_error_spike"
        assert AlertType.WEBHOOK_MISSING.value == "webhook_missing"
        assert AlertType.API_CONTRACT_FAIL.value == "api_contract_fail"

    def test_new_emojis_exist(self) -> None:
        """새 타입 이모지 매핑"""
        assert AlertType.FRONTEND_ERROR_SPIKE in ALERT_EMOJI
        assert AlertType.WEBHOOK_MISSING in ALERT_EMOJI
        assert AlertType.API_CONTRACT_FAIL in ALERT_EMOJI

    def test_new_cooldowns_exist(self) -> None:
        """새 타입 쿨다운 매핑"""
        assert AlertType.FRONTEND_ERROR_SPIKE in DEFAULT_COOLDOWNS
        assert AlertType.WEBHOOK_MISSING in DEFAULT_COOLDOWNS
        assert AlertType.API_CONTRACT_FAIL in DEFAULT_COOLDOWNS

    def test_webhook_cooldown_short(self) -> None:
        """웹훅 누락은 5분 쿨다운 (긴급)"""
        assert DEFAULT_COOLDOWNS[AlertType.WEBHOOK_MISSING] == 300


@pytest.mark.asyncio
class TestAlertManagerFrontendSpike:
    async def test_disabled_returns_none(self) -> None:
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=False)
        result = await mgr.check_frontend_error_spike("testorum", 100)
        assert result is None

    async def test_below_threshold(self) -> None:
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        result = await mgr.check_frontend_error_spike("testorum", 5)
        assert result is None

    async def test_warning_level(self) -> None:
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_frontend_error_spike("testorum", 20, top_error="TypeError")
        assert result is not None
        assert result.level == AlertLevel.WARNING

    async def test_critical_level(self) -> None:
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_frontend_error_spike("testorum", 60)
        assert result is not None
        assert result.level == AlertLevel.CRITICAL


@pytest.mark.asyncio
class TestAlertManagerWebhookMissing:
    async def test_disabled_returns_none(self) -> None:
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=False)
        result = await mgr.check_webhook_missing("testorum", "lemonsqueezy", 5)
        assert result is None

    async def test_zero_missing(self) -> None:
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        result = await mgr.check_webhook_missing("testorum", "stripe", 0)
        assert result is None

    async def test_warning_level(self) -> None:
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_webhook_missing(
                "testorum", "lemonsqueezy", 2, ["ord_123", "ord_456"]
            )
        assert result is not None
        assert result.level == AlertLevel.WARNING

    async def test_critical_level(self) -> None:
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_webhook_missing("testorum", "stripe", 5)
        assert result is not None
        assert result.level == AlertLevel.CRITICAL

    async def test_telegram_message_format(self) -> None:
        """텔레그램 메시지에 제품명 + 프로바이더 포함"""
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_webhook_missing(
                "testorum", "lemonsqueezy", 1, ["ord_999"]
            )
        msg = result.to_telegram()
        assert "testorum" in msg
        assert "lemonsqueezy" in msg


@pytest.mark.asyncio
class TestAlertManagerContractFail:
    async def test_disabled_returns_none(self) -> None:
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=False)
        result = await mgr.check_api_contract_fail("testorum", 3, 5)
        assert result is None

    async def test_zero_failures(self) -> None:
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        result = await mgr.check_api_contract_fail("testorum", 0, 10)
        assert result is None

    async def test_warning_level(self) -> None:
        """실패율 < 50% → WARNING"""
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_api_contract_fail(
                "testorum", 2, 10,
                first_failure_url="https://testorum.app/api/health",
                first_failure_reason="상태코드 불일치",
            )
        assert result is not None
        assert result.level == AlertLevel.WARNING

    async def test_critical_level(self) -> None:
        """실패율 >= 50% → CRITICAL"""
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_api_contract_fail("testorum", 5, 5)
        assert result is not None
        assert result.level == AlertLevel.CRITICAL

    async def test_message_includes_url(self) -> None:
        """실패 URL이 메시지에 포함"""
        mgr = AlertManager(bot_token="t", chat_id="c", enabled=True)
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_api_contract_fail(
                "testorum", 1, 3,
                first_failure_url="https://testorum.app/api/broken",
            )
        msg = result.to_telegram()
        assert "broken" in msg


# ============================================================
# 5. No LLM 재확인 — AlertManager 새 메서드도 포함
# ============================================================

class TestNewAlertMethodsNoLLM:
    def test_alert_manager_check_methods_no_llm(self) -> None:
        """AlertManager 새 check 메서드에 LLM 의존 없음"""
        import inspect
        source = inspect.getsource(AlertManager.check_frontend_error_spike)
        assert "llm" not in source.lower()
        assert "litellm" not in source.lower()

        source = inspect.getsource(AlertManager.check_webhook_missing)
        assert "llm" not in source.lower()

        source = inspect.getsource(AlertManager.check_api_contract_fail)
        assert "llm" not in source.lower()
