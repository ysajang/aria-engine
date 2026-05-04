"""ARIA Engine - Payment Monitor Tests (Phase 3.5 Step 6)

결제 이상 감지 테스트
- payment_checks.py 핵심 로직 (Stripe / LemonSqueezy / Toss)
- PaymentAuditTool ToolExecutor
- AlertType / AlertManager 결제 알림
- _evaluate_monitoring_event 이벤트 hook

총 테스트: ~45개
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ============================================================
# 1. payment_checks — 유틸리티 + 임계치 로직
# ============================================================


class TestSeverityForRate:
    """_severity_for_rate 유틸리티"""

    def test_normal_returns_none(self):
        from aria.monitoring.payment_checks import _severity_for_rate
        assert _severity_for_rate(2.0, 5.0, 10.0) is None

    def test_warning_threshold(self):
        from aria.monitoring.payment_checks import _severity_for_rate
        assert _severity_for_rate(5.0, 5.0, 10.0) == "medium"
        assert _severity_for_rate(7.5, 5.0, 10.0) == "medium"

    def test_critical_threshold(self):
        from aria.monitoring.payment_checks import _severity_for_rate
        assert _severity_for_rate(10.0, 5.0, 10.0) == "high"
        assert _severity_for_rate(15.0, 5.0, 10.0) == "high"

    def test_zero_rate(self):
        from aria.monitoring.payment_checks import _severity_for_rate
        assert _severity_for_rate(0.0, 5.0, 10.0) is None

    def test_exact_boundaries(self):
        from aria.monitoring.payment_checks import _severity_for_rate
        assert _severity_for_rate(4.99, 5.0, 10.0) is None
        assert _severity_for_rate(5.0, 5.0, 10.0) == "medium"
        assert _severity_for_rate(9.99, 5.0, 10.0) == "medium"
        assert _severity_for_rate(10.0, 5.0, 10.0) == "high"


class TestDefaultThresholds:
    """기본 임계치 상수"""

    def test_refund_thresholds(self):
        from aria.monitoring.payment_checks import (
            DEFAULT_REFUND_RATE_WARNING,
            DEFAULT_REFUND_RATE_CRITICAL,
        )
        assert DEFAULT_REFUND_RATE_WARNING == 5.0
        assert DEFAULT_REFUND_RATE_CRITICAL == 10.0

    def test_failure_thresholds(self):
        from aria.monitoring.payment_checks import (
            DEFAULT_FAILURE_RATE_WARNING,
            DEFAULT_FAILURE_RATE_CRITICAL,
        )
        assert DEFAULT_FAILURE_RATE_WARNING == 3.0
        assert DEFAULT_FAILURE_RATE_CRITICAL == 8.0

    def test_churn_thresholds(self):
        from aria.monitoring.payment_checks import (
            DEFAULT_CHURN_RATE_WARNING,
            DEFAULT_CHURN_RATE_CRITICAL,
        )
        assert DEFAULT_CHURN_RATE_WARNING == 5.0
        assert DEFAULT_CHURN_RATE_CRITICAL == 10.0


# ============================================================
# 2. Stripe Checks (mocked httpx)
# ============================================================


def _mock_stripe_response(data: list[dict], status: int = 200):
    """Stripe API mock 응답 생성"""
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.json.return_value = {"data": data}
    mock_resp.text = json.dumps({"data": data})
    return mock_resp


class TestStripeRefunds:
    """check_stripe_refunds"""

    @pytest.mark.asyncio
    async def test_no_charges_zero_rate(self):
        from aria.monitoring.payment_checks import check_stripe_refunds

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.return_value = _mock_stripe_response([])

            result = await check_stripe_refunds("sk_test_xxx", period_days=7)
            assert result["provider"] == "stripe"
            assert result["refund_rate"] == 0.0
            assert result["total_charges"] == 0
            assert len(result["issues"]) == 0

    @pytest.mark.asyncio
    async def test_normal_refund_rate(self):
        from aria.monitoring.payment_checks import check_stripe_refunds

        charges = [
            {"status": "succeeded", "amount": 1000, "currency": "usd"}
            for _ in range(100)
        ]
        refunds = [{"amount": 1000} for _ in range(2)]

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.side_effect = [
                _mock_stripe_response(charges),
                _mock_stripe_response(refunds),
            ]

            result = await check_stripe_refunds("sk_test_xxx")
            assert result["refund_rate"] == 2.0
            assert len(result["issues"]) == 0

    @pytest.mark.asyncio
    async def test_high_refund_rate_warning(self):
        from aria.monitoring.payment_checks import check_stripe_refunds

        charges = [{"status": "succeeded", "amount": 1000} for _ in range(20)]
        refunds = [{"amount": 1000} for _ in range(2)]  # 10%

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.side_effect = [
                _mock_stripe_response(charges),
                _mock_stripe_response(refunds),
            ]

            result = await check_stripe_refunds("sk_test_xxx")
            assert result["refund_rate"] == 10.0
            assert len(result["issues"]) == 1
            assert result["issues"][0]["severity"] == "high"
            assert result["issues"][0]["type"] == "refund_rate_high"

    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        import httpx
        from aria.monitoring.payment_checks import check_stripe_refunds

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.side_effect = httpx.TimeoutException("timeout")

            result = await check_stripe_refunds("sk_test_xxx")
            assert len(result["issues"]) == 1
            assert result["issues"][0]["type"] == "timeout"


class TestStripeFailures:
    """check_stripe_failures"""

    @pytest.mark.asyncio
    async def test_no_failures_zero_rate(self):
        from aria.monitoring.payment_checks import check_stripe_failures

        charges = [{"status": "succeeded"} for _ in range(50)]

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.return_value = _mock_stripe_response(charges)

            result = await check_stripe_failures("sk_test_xxx")
            assert result["failure_rate"] == 0.0
            assert result["total_failures"] == 0

    @pytest.mark.asyncio
    async def test_high_failure_rate(self):
        from aria.monitoring.payment_checks import check_stripe_failures

        charges = [{"status": "succeeded"} for _ in range(90)]
        charges += [
            {"status": "failed", "outcome": {"reason": "card_declined"}}
            for _ in range(10)
        ]

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.return_value = _mock_stripe_response(charges)

            result = await check_stripe_failures("sk_test_xxx")
            assert result["failure_rate"] == 10.0
            assert result["total_failures"] == 10
            assert len(result["issues"]) == 1
            assert result["issues"][0]["severity"] == "high"

    @pytest.mark.asyncio
    async def test_failure_reason_aggregation(self):
        from aria.monitoring.payment_checks import check_stripe_failures

        charges = [{"status": "succeeded"} for _ in range(80)]
        charges += [
            {"status": "failed", "outcome": {"reason": "card_declined"}}
            for _ in range(12)
        ]
        charges += [
            {"status": "failed", "outcome": {"reason": "insufficient_funds"}}
            for _ in range(8)
        ]

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.return_value = _mock_stripe_response(charges)

            result = await check_stripe_failures("sk_test_xxx")
            reasons = result["top_failure_reasons"]
            assert len(reasons) == 2
            assert reasons[0]["reason"] == "card_declined"
            assert reasons[0]["count"] == 12


class TestStripeChurn:
    """check_stripe_churn"""

    @pytest.mark.asyncio
    async def test_no_churn(self):
        from aria.monitoring.payment_checks import check_stripe_churn

        active = [{"status": "active"} for _ in range(50)]

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.side_effect = [
                _mock_stripe_response(active),
                _mock_stripe_response([]),
            ]

            result = await check_stripe_churn("sk_test_xxx")
            assert result["churn_rate"] == 0.0
            assert result["active_subscriptions"] == 50

    @pytest.mark.asyncio
    async def test_high_churn_rate(self):
        from aria.monitoring.payment_checks import check_stripe_churn

        active = [{"status": "active"} for _ in range(80)]
        canceled = [
            {"status": "canceled", "cancellation_details": {"reason": "too_expensive"}}
            for _ in range(20)
        ]

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.side_effect = [
                _mock_stripe_response(active),
                _mock_stripe_response(canceled),
            ]

            result = await check_stripe_churn("sk_test_xxx")
            assert result["churn_rate"] == 20.0
            assert len(result["issues"]) == 1
            assert result["issues"][0]["severity"] == "high"
            assert result["top_cancel_reasons"][0]["reason"] == "too_expensive"


# ============================================================
# 3. LemonSqueezy Checks
# ============================================================


def _mock_ls_response(data: list[dict], status: int = 200):
    """LemonSqueezy JSON:API mock 응답"""
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.json.return_value = {"data": data}
    return mock_resp


class TestLemonSqueezyRefunds:
    """check_lemonsqueezy_refunds"""

    @pytest.mark.asyncio
    async def test_no_refunds(self):
        from aria.monitoring.payment_checks import check_lemonsqueezy_refunds

        now_iso = datetime.now(timezone.utc).isoformat()
        orders = [
            {"attributes": {"status": "paid", "total": 2999, "created_at": now_iso}}
            for _ in range(10)
        ]

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.return_value = _mock_ls_response(orders)

            result = await check_lemonsqueezy_refunds("lmsq_xxx")
            assert result["provider"] == "lemonsqueezy"
            assert result["refund_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_high_refund_rate(self):
        from aria.monitoring.payment_checks import check_lemonsqueezy_refunds

        now_iso = datetime.now(timezone.utc).isoformat()
        orders = [
            {"attributes": {"status": "paid", "total": 2999, "created_at": now_iso}}
            for _ in range(8)
        ]
        orders += [
            {"attributes": {"status": "refunded", "total": 2999, "created_at": now_iso}}
            for _ in range(2)
        ]

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.get.return_value = _mock_ls_response(orders)

            result = await check_lemonsqueezy_refunds("lmsq_xxx")
            assert result["refund_rate"] == 20.0
            assert len(result["issues"]) == 1


# ============================================================
# 4. Toss Payments Checks
# ============================================================


class TestTossRefunds:
    """check_toss_refunds"""

    @pytest.mark.asyncio
    async def test_no_cancels(self):
        from aria.monitoring.payment_checks import check_toss_refunds

        txns = [{"status": "DONE", "totalAmount": 50000} for _ in range(20)]

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            resp_mock = MagicMock()
            resp_mock.status_code = 200
            resp_mock.json.return_value = txns
            mock_client.get.return_value = resp_mock

            result = await check_toss_refunds("sk_test_xxx")
            assert result["provider"] == "toss"
            assert result["refund_rate"] == 0.0
            assert result["total_cancels"] == 0

    @pytest.mark.asyncio
    async def test_high_cancel_rate(self):
        from aria.monitoring.payment_checks import check_toss_refunds

        txns = [{"status": "DONE", "totalAmount": 50000} for _ in range(8)]
        txns += [{"status": "CANCELED", "totalAmount": 50000} for _ in range(2)]

        with patch("aria.monitoring.payment_checks.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            resp_mock = MagicMock()
            resp_mock.status_code = 200
            resp_mock.json.return_value = txns
            mock_client.get.return_value = resp_mock

            result = await check_toss_refunds("sk_test_xxx")
            assert result["refund_rate"] == 20.0
            assert len(result["issues"]) == 1
            assert result["issues"][0]["severity"] == "high"


# ============================================================
# 5. run_payment_audit (통합)
# ============================================================


class TestPaymentAudit:
    """run_payment_audit 통합"""

    @pytest.mark.asyncio
    async def test_unsupported_provider(self):
        from aria.monitoring.payment_checks import run_payment_audit

        result = await run_payment_audit("paypal", "key")
        assert result["total_issues"] == 1
        assert result["high_issues"] == 1
        assert "unsupported_provider" in result["all_issues"][0]["type"]

    @pytest.mark.asyncio
    async def test_stripe_audit_aggregates_issues(self):
        from aria.monitoring.payment_checks import run_payment_audit

        with patch("aria.monitoring.payment_checks.check_stripe_refunds") as mock_refund, \
             patch("aria.monitoring.payment_checks.check_stripe_failures") as mock_fail, \
             patch("aria.monitoring.payment_checks.check_stripe_churn") as mock_churn:

            mock_refund.return_value = {
                "refund_rate": 12.0, "issues": [{"type": "refund_rate_high", "severity": "high", "message": "test"}],
            }
            mock_fail.return_value = {
                "failure_rate": 0.5, "issues": [],
            }
            mock_churn.return_value = {
                "churn_rate": 2.0, "issues": [],
            }

            result = await run_payment_audit("stripe", "sk_test_xxx")
            assert result["provider"] == "stripe"
            assert result["total_issues"] == 1
            assert result["high_issues"] == 1

    @pytest.mark.asyncio
    async def test_lemonsqueezy_no_failure_api(self):
        from aria.monitoring.payment_checks import run_payment_audit

        with patch("aria.monitoring.payment_checks.check_lemonsqueezy_refunds") as mock_refund, \
             patch("aria.monitoring.payment_checks.check_lemonsqueezy_churn") as mock_churn:

            mock_refund.return_value = {"refund_rate": 0.0, "issues": []}
            mock_churn.return_value = {"churn_rate": 0.0, "issues": []}

            result = await run_payment_audit("lemonsqueezy", "lmsq_xxx")
            assert result["failures"]["note"]  # LemonSqueezy는 failure API 없음 설명

    @pytest.mark.asyncio
    async def test_toss_limited_apis(self):
        from aria.monitoring.payment_checks import run_payment_audit

        with patch("aria.monitoring.payment_checks.check_toss_refunds") as mock_refund:
            mock_refund.return_value = {"refund_rate": 0.0, "issues": []}

            result = await run_payment_audit("toss", "sk_test_xxx")
            assert result["failures"]["note"]
            assert result["churn"]["note"]

    @pytest.mark.asyncio
    async def test_elapsed_ms_tracked(self):
        from aria.monitoring.payment_checks import run_payment_audit

        with patch("aria.monitoring.payment_checks.check_stripe_refunds") as mock_r, \
             patch("aria.monitoring.payment_checks.check_stripe_failures") as mock_f, \
             patch("aria.monitoring.payment_checks.check_stripe_churn") as mock_c:
            mock_r.return_value = {"issues": []}
            mock_f.return_value = {"issues": []}
            mock_c.return_value = {"issues": []}

            result = await run_payment_audit("stripe", "sk_test_xxx")
            assert "elapsed_ms" in result
            assert result["elapsed_ms"] >= 0


# ============================================================
# 6. PaymentAuditTool (ToolExecutor)
# ============================================================


class TestPaymentAuditTool:
    """PaymentAuditTool ToolExecutor"""

    def test_definition(self):
        from aria.tools.mcp.payment_monitor_tools import PaymentAuditTool
        tool = PaymentAuditTool()
        defn = tool.get_definition()
        assert defn.name == "payment_audit"
        assert defn.safety_hint.value == "read_only"
        assert len(defn.parameters) == 5

    @pytest.mark.asyncio
    async def test_missing_provider(self):
        from aria.tools.mcp.payment_monitor_tools import PaymentAuditTool
        tool = PaymentAuditTool()
        result = await tool.execute({"provider": ""})
        assert not result.success
        assert "비어있습니다" in result.error

    @pytest.mark.asyncio
    async def test_unsupported_provider(self):
        from aria.tools.mcp.payment_monitor_tools import PaymentAuditTool
        tool = PaymentAuditTool()
        result = await tool.execute({"provider": "paypal"})
        assert not result.success
        assert "지원하지 않는" in result.error

    @pytest.mark.asyncio
    async def test_missing_api_key(self):
        from aria.tools.mcp.payment_monitor_tools import PaymentAuditTool
        tool = PaymentAuditTool()
        with patch.dict("os.environ", {}, clear=True):
            result = await tool.execute({"provider": "stripe"})
            assert not result.success
            assert "STRIPE_SECRET_KEY" in result.error

    @pytest.mark.asyncio
    async def test_successful_audit(self):
        from aria.tools.mcp.payment_monitor_tools import PaymentAuditTool

        tool = PaymentAuditTool()
        mock_audit = {
            "provider": "stripe",
            "refunds": {"check_type": "refund_rate", "refund_rate": 2.0, "total_charges": 100, "total_refunds": 2},
            "failures": {"check_type": "failure_rate", "failure_rate": 1.0, "total_attempts": 100, "total_failures": 1, "top_failure_reasons": []},
            "churn": {"check_type": "churn_rate", "churn_rate": 3.0, "active_subscriptions": 90, "canceled_in_period": 3, "top_cancel_reasons": []},
            "total_issues": 0,
            "high_issues": 0,
            "all_issues": [],
        }

        with patch("aria.tools.mcp.payment_monitor_tools.run_payment_audit", return_value=mock_audit):
            result = await tool.execute({"provider": "stripe", "api_key": "sk_test_xxx"})
            assert result.success
            assert result.output["provider"] == "stripe"
            assert result.output["total_issues"] == 0

    @pytest.mark.asyncio
    async def test_audit_with_issues(self):
        from aria.tools.mcp.payment_monitor_tools import PaymentAuditTool

        tool = PaymentAuditTool()
        mock_audit = {
            "provider": "stripe",
            "refunds": {"check_type": "refund_rate", "refund_rate": 12.0, "total_charges": 100, "total_refunds": 12},
            "failures": {"check_type": "failure_rate", "failure_rate": 9.0, "total_attempts": 100, "total_failures": 9, "top_failure_reasons": [{"reason": "card_declined", "count": 7}]},
            "churn": {"check_type": "churn_rate", "churn_rate": 0.0, "active_subscriptions": 50, "canceled_in_period": 0, "top_cancel_reasons": []},
            "total_issues": 2,
            "high_issues": 2,
            "all_issues": [{}, {}],
        }

        with patch("aria.tools.mcp.payment_monitor_tools.run_payment_audit", return_value=mock_audit):
            result = await tool.execute({"provider": "stripe", "api_key": "sk_test_xxx"})
            assert result.success
            assert result.output["total_issues"] == 2
            assert result.output["high_issues"] == 2
            assert result.output["refunds"]["refund_rate"] == 12.0

    @pytest.mark.asyncio
    async def test_env_key_fallback(self):
        from aria.tools.mcp.payment_monitor_tools import PaymentAuditTool

        tool = PaymentAuditTool()
        mock_audit = {
            "provider": "stripe",
            "refunds": {"check_type": "refund_rate", "refund_rate": 0.0, "total_charges": 0, "total_refunds": 0},
            "failures": {"check_type": "failure_rate", "failure_rate": 0.0, "total_attempts": 0, "total_failures": 0, "top_failure_reasons": []},
            "churn": {"check_type": "churn_rate", "churn_rate": 0.0, "active_subscriptions": 0, "canceled_in_period": 0, "top_cancel_reasons": []},
            "total_issues": 0,
            "high_issues": 0,
            "all_issues": [],
        }

        with patch.dict("os.environ", {"STRIPE_SECRET_KEY": "sk_test_env"}), \
             patch("aria.tools.mcp.payment_monitor_tools.run_payment_audit", return_value=mock_audit) as mock_fn:
            result = await tool.execute({"provider": "stripe"})
            assert result.success
            mock_fn.assert_called_once()
            assert mock_fn.call_args.kwargs["api_key"] == "sk_test_env"

    @pytest.mark.asyncio
    async def test_llm_format(self):
        """ToolDefinition → LLM function calling 포맷 변환"""
        from aria.tools.mcp.payment_monitor_tools import PaymentAuditTool
        tool = PaymentAuditTool()
        defn = tool.get_definition()
        llm_format = defn.to_llm_tool()
        assert llm_format["type"] == "function"
        assert llm_format["function"]["name"] == "payment_audit"
        params = llm_format["function"]["parameters"]
        assert "provider" in params["properties"]
        assert "provider" in params["required"]


# ============================================================
# 7. AlertType + AlertManager 결제 알림
# ============================================================


class TestPaymentAlertTypes:
    """결제 관련 AlertType 추가 확인"""

    def test_alert_types_exist(self):
        from aria.alerts.alert_types import AlertType
        assert AlertType.PAYMENT_REFUND_SPIKE.value == "payment_refund_spike"
        assert AlertType.PAYMENT_FAILURE_RATE.value == "payment_failure_rate"
        assert AlertType.SUBSCRIPTION_CHURN.value == "subscription_churn"

    def test_emoji_mappings(self):
        from aria.alerts.alert_types import ALERT_EMOJI, AlertType
        assert AlertType.PAYMENT_REFUND_SPIKE in ALERT_EMOJI
        assert AlertType.PAYMENT_FAILURE_RATE in ALERT_EMOJI
        assert AlertType.SUBSCRIPTION_CHURN in ALERT_EMOJI

    def test_cooldown_mappings(self):
        from aria.alerts.alert_types import DEFAULT_COOLDOWNS, AlertType
        assert AlertType.PAYMENT_REFUND_SPIKE in DEFAULT_COOLDOWNS
        assert DEFAULT_COOLDOWNS[AlertType.PAYMENT_REFUND_SPIKE] == 3600
        assert DEFAULT_COOLDOWNS[AlertType.PAYMENT_FAILURE_RATE] == 1800
        assert DEFAULT_COOLDOWNS[AlertType.SUBSCRIPTION_CHURN] == 86400


class TestAlertManagerPayment:
    """AlertManager 결제 알림 메서드"""

    def _make_manager(self):
        from aria.alerts.alert_manager import AlertManager
        return AlertManager(
            bot_token="test-token",
            chat_id="123456",
            enabled=True,
        )

    @pytest.mark.asyncio
    async def test_refund_spike_below_threshold(self):
        mgr = self._make_manager()
        result = await mgr.check_payment_refund_spike(
            provider="stripe", product_label=None,
            refund_rate=3.0, total_charges=100, total_refunds=3,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_refund_spike_warning(self):
        mgr = self._make_manager()
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_payment_refund_spike(
                provider="stripe", product_label="Testorum",
                refund_rate=7.0, total_charges=100, total_refunds=7,
            )
            assert result is not None
            assert result.level.value == "warning"
            assert "Testorum" in result.title

    @pytest.mark.asyncio
    async def test_refund_spike_critical(self):
        mgr = self._make_manager()
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_payment_refund_spike(
                provider="stripe", product_label=None,
                refund_rate=15.0, total_charges=100, total_refunds=15,
            )
            assert result is not None
            assert result.level.value == "critical"

    @pytest.mark.asyncio
    async def test_failure_rate_below_threshold(self):
        mgr = self._make_manager()
        result = await mgr.check_payment_failure_rate(
            provider="stripe", product_label=None,
            failure_rate=1.0, total_attempts=100, total_failures=1,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_failure_rate_with_reasons(self):
        mgr = self._make_manager()
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_payment_failure_rate(
                provider="stripe", product_label="Testorum",
                failure_rate=9.0, total_attempts=100, total_failures=9,
                top_reasons=[{"reason": "card_declined", "count": 7}],
            )
            assert result is not None
            assert result.level.value == "critical"
            assert "card_declined" in result.message

    @pytest.mark.asyncio
    async def test_churn_below_threshold(self):
        mgr = self._make_manager()
        result = await mgr.check_subscription_churn(
            provider="stripe", product_label=None,
            churn_rate=2.0, active_subscriptions=100, canceled_in_period=2,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_churn_warning(self):
        mgr = self._make_manager()
        with patch("aria.alerts.alert_manager.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {"ok": True}
            result = await mgr.check_subscription_churn(
                provider="stripe", product_label="Mystel",
                churn_rate=7.0, active_subscriptions=90, canceled_in_period=7,
                top_reasons=[{"reason": "too_expensive", "count": 5}],
            )
            assert result is not None
            assert result.level.value == "warning"
            assert "Mystel" in result.title
            assert "too_expensive" in result.message

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(enabled=False)
        assert await mgr.check_payment_refund_spike(
            "stripe", None, 99.0, 100, 99,
        ) is None
        assert await mgr.check_payment_failure_rate(
            "stripe", None, 99.0, 100, 99,
        ) is None
        assert await mgr.check_subscription_churn(
            "stripe", None, 99.0, 100, 99,
        ) is None


class TestAlertTelegram:
    """Alert.to_telegram() 결제 알림 포맷"""

    def test_refund_alert_telegram(self):
        from aria.alerts.alert_types import Alert, AlertLevel, AlertType
        alert = Alert(
            alert_type=AlertType.PAYMENT_REFUND_SPIKE,
            level=AlertLevel.CRITICAL,
            title="환불율 급증 — [Testorum] stripe",
            message="환불율: 12.0%\n환불: 12건 / 전체: 100건",
            data={"provider": "stripe", "refund_rate": 12.0},
        )
        msg = alert.to_telegram()
        assert "💸" in msg
        assert "*[긴급]*" in msg
        assert "환불율 급증" in msg


# ============================================================
# 8. Provider ENV Key Mapping
# ============================================================


class TestProviderEnvKeys:
    """PROVIDER_ENV_KEYS 매핑"""

    def test_all_providers_mapped(self):
        from aria.tools.mcp.payment_monitor_tools import PROVIDER_ENV_KEYS
        assert "stripe" in PROVIDER_ENV_KEYS
        assert "lemonsqueezy" in PROVIDER_ENV_KEYS
        assert "toss" in PROVIDER_ENV_KEYS
        assert PROVIDER_ENV_KEYS["stripe"] == "STRIPE_SECRET_KEY"
        assert PROVIDER_ENV_KEYS["lemonsqueezy"] == "LEMONSQUEEZY_API_KEY"
        assert PROVIDER_ENV_KEYS["toss"] == "TOSS_SECRET_KEY"
