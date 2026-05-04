"""ARIA Engine - Payment Anomaly Monitoring Checks

결제 이상 감지 로직 (ToolExecutor + cron 스크립트 공용)
- check_refund_rate: 환불율 감지 (기간별 환불 건수/금액 비율)
- check_payment_failures: 결제 실패율 감시
- check_churn_rate: 구독 이탈률 변화 추적
- run_payment_audit: 위 3가지 통합 실행

지원 프로바이더:
- Stripe: API Key 기반 (sk_live_* / sk_test_*)
- LemonSqueezy: API Key 기반 (Bearer)
- Toss Payments: Secret Key 기반 (Basic Auth)

설계 원칙:
- Exit-Safe: 프로바이더 API pull만 (웹훅 리스너 금지)
- 모든 함수는 dict 반환 → ToolResult.output / EventInput.data 양쪽 사용
- 임계치 초과 시 issues 배열에 추가 → AlertManager 연동
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

# === Provider API Base URLs ===
STRIPE_API_BASE = "https://api.stripe.com/v1"
LEMONSQUEEZY_API_BASE = "https://api.lemonsqueezy.com/v1"
TOSS_API_BASE = "https://api.tosspayments.com/v1"

# === Default Thresholds ===
DEFAULT_REFUND_RATE_WARNING = 5.0   # 환불율 5% 이상 WARNING
DEFAULT_REFUND_RATE_CRITICAL = 10.0  # 환불율 10% 이상 CRITICAL
DEFAULT_FAILURE_RATE_WARNING = 3.0   # 실패율 3% 이상 WARNING
DEFAULT_FAILURE_RATE_CRITICAL = 8.0  # 실패율 8% 이상 CRITICAL
DEFAULT_CHURN_RATE_WARNING = 5.0    # 이탈률 5% 이상 WARNING
DEFAULT_CHURN_RATE_CRITICAL = 10.0   # 이탈률 10% 이상 CRITICAL


def _severity_for_rate(
    rate: float,
    warning_threshold: float,
    critical_threshold: float,
) -> str | None:
    """비율 기반 심각도 판단 (None이면 정상)"""
    if rate >= critical_threshold:
        return "high"
    if rate >= warning_threshold:
        return "medium"
    return None


# ============================================================
# 1. Stripe
# ============================================================


async def _stripe_list(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Stripe List API 호출 (auto-pagination 없이 단일 페이지)"""
    resp = await client.get(
        f"{STRIPE_API_BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {api_key}"},
        params=params or {},
    )
    if resp.status_code != 200:
        logger.warning(
            "stripe_api_error",
            endpoint=endpoint,
            status=resp.status_code,
            body=resp.text[:200],
        )
        return []
    data = resp.json()
    return data.get("data", []) if isinstance(data, dict) else []


async def check_stripe_refunds(
    api_key: str,
    period_days: int = 7,
    refund_warning: float = DEFAULT_REFUND_RATE_WARNING,
    refund_critical: float = DEFAULT_REFUND_RATE_CRITICAL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Stripe 환불율 체크

    Args:
        api_key: Stripe Secret Key (sk_live_* / sk_test_*)
        period_days: 검사 기간 (일)

    Returns:
        {provider, total_charges, total_refunds, refund_rate, refund_amount,
         charge_amount, currency, issues, checked_at}
    """
    result: dict[str, Any] = {
        "provider": "stripe",
        "check_type": "refund_rate",
        "total_charges": 0,
        "total_refunds": 0,
        "refund_rate": 0.0,
        "refund_amount": 0,
        "charge_amount": 0,
        "currency": "usd",
        "period_days": period_days,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    created_gte = int(time.time()) - (period_days * 86400)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 성공 결제 조회
            charges = await _stripe_list(
                client, "charges", api_key,
                {"limit": "100", "created[gte]": str(created_gte)},
            )
            succeeded = [c for c in charges if c.get("status") == "succeeded"]
            result["total_charges"] = len(succeeded)
            result["charge_amount"] = sum(c.get("amount", 0) for c in succeeded)

            if succeeded:
                result["currency"] = succeeded[0].get("currency", "usd")

            # 환불 조회
            refunds = await _stripe_list(
                client, "refunds", api_key,
                {"limit": "100", "created[gte]": str(created_gte)},
            )
            result["total_refunds"] = len(refunds)
            result["refund_amount"] = sum(r.get("amount", 0) for r in refunds)

            # 환불율 계산 (건수 기준)
            if result["total_charges"] > 0:
                rate = (result["total_refunds"] / result["total_charges"]) * 100
                result["refund_rate"] = round(rate, 2)

                severity = _severity_for_rate(rate, refund_warning, refund_critical)
                if severity:
                    result["issues"].append({
                        "type": "refund_rate_high",
                        "severity": severity,
                        "message": (
                            f"환불율 {rate:.1f}% "
                            f"({result['total_refunds']}/{result['total_charges']}건 / "
                            f"{period_days}일)"
                        ),
                    })

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "medium",
            "message": f"Stripe API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error",
            "severity": "medium",
            "message": f"Stripe API 오류: {str(e)[:200]}",
        })

    return result


async def check_stripe_failures(
    api_key: str,
    period_days: int = 7,
    failure_warning: float = DEFAULT_FAILURE_RATE_WARNING,
    failure_critical: float = DEFAULT_FAILURE_RATE_CRITICAL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Stripe 결제 실패율 체크

    Returns:
        {provider, total_attempts, total_failures, failure_rate,
         top_failure_reasons, issues, checked_at}
    """
    result: dict[str, Any] = {
        "provider": "stripe",
        "check_type": "failure_rate",
        "total_attempts": 0,
        "total_failures": 0,
        "failure_rate": 0.0,
        "top_failure_reasons": [],
        "period_days": period_days,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    created_gte = int(time.time()) - (period_days * 86400)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 전체 결제 시도 (succeeded + failed)
            charges = await _stripe_list(
                client, "charges", api_key,
                {"limit": "100", "created[gte]": str(created_gte)},
            )
            result["total_attempts"] = len(charges)
            failed = [c for c in charges if c.get("status") == "failed"]
            result["total_failures"] = len(failed)

            # 실패율 계산
            if result["total_attempts"] > 0:
                rate = (result["total_failures"] / result["total_attempts"]) * 100
                result["failure_rate"] = round(rate, 2)

                severity = _severity_for_rate(rate, failure_warning, failure_critical)
                if severity:
                    result["issues"].append({
                        "type": "failure_rate_high",
                        "severity": severity,
                        "message": (
                            f"결제 실패율 {rate:.1f}% "
                            f"({result['total_failures']}/{result['total_attempts']}건 / "
                            f"{period_days}일)"
                        ),
                    })

            # 실패 원인 집계
            reasons: dict[str, int] = {}
            for c in failed:
                outcome = c.get("outcome", {})
                reason = outcome.get("reason", c.get("failure_code", "unknown"))
                reasons[reason] = reasons.get(reason, 0) + 1

            result["top_failure_reasons"] = [
                {"reason": r, "count": cnt}
                for r, cnt in sorted(reasons.items(), key=lambda x: -x[1])[:5]
            ]

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "medium",
            "message": f"Stripe API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error",
            "severity": "medium",
            "message": f"Stripe API 오류: {str(e)[:200]}",
        })

    return result


async def check_stripe_churn(
    api_key: str,
    period_days: int = 30,
    churn_warning: float = DEFAULT_CHURN_RATE_WARNING,
    churn_critical: float = DEFAULT_CHURN_RATE_CRITICAL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Stripe 구독 이탈률 체크

    Returns:
        {provider, active_subscriptions, canceled_in_period, churn_rate,
         top_cancel_reasons, issues, checked_at}
    """
    result: dict[str, Any] = {
        "provider": "stripe",
        "check_type": "churn_rate",
        "active_subscriptions": 0,
        "canceled_in_period": 0,
        "churn_rate": 0.0,
        "top_cancel_reasons": [],
        "period_days": period_days,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    created_gte = int(time.time()) - (period_days * 86400)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 현재 활성 구독 수
            active_subs = await _stripe_list(
                client, "subscriptions", api_key,
                {"limit": "100", "status": "active"},
            )
            result["active_subscriptions"] = len(active_subs)

            # 기간 내 취소 구독
            canceled_subs = await _stripe_list(
                client, "subscriptions", api_key,
                {"limit": "100", "status": "canceled", "created[gte]": str(created_gte)},
            )
            result["canceled_in_period"] = len(canceled_subs)

            # 이탈률: 취소 / (활성 + 취소)
            total_base = result["active_subscriptions"] + result["canceled_in_period"]
            if total_base > 0:
                rate = (result["canceled_in_period"] / total_base) * 100
                result["churn_rate"] = round(rate, 2)

                severity = _severity_for_rate(rate, churn_warning, churn_critical)
                if severity:
                    result["issues"].append({
                        "type": "churn_rate_high",
                        "severity": severity,
                        "message": (
                            f"구독 이탈률 {rate:.1f}% "
                            f"({result['canceled_in_period']}건 취소 / "
                            f"{total_base}건 전체 / {period_days}일)"
                        ),
                    })

            # 취소 사유 집계
            reasons: dict[str, int] = {}
            for sub in canceled_subs:
                reason = sub.get("cancellation_details", {}).get("reason", "unknown")
                reasons[reason] = reasons.get(reason, 0) + 1

            result["top_cancel_reasons"] = [
                {"reason": r, "count": cnt}
                for r, cnt in sorted(reasons.items(), key=lambda x: -x[1])[:5]
            ]

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "medium",
            "message": f"Stripe API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error",
            "severity": "medium",
            "message": f"Stripe API 오류: {str(e)[:200]}",
        })

    return result


# ============================================================
# 2. LemonSqueezy
# ============================================================


async def _lemonsqueezy_list(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """LemonSqueezy JSON:API List 호출"""
    resp = await client.get(
        f"{LEMONSQUEEZY_API_BASE}/{endpoint}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/vnd.api+json",
        },
        params=params or {},
    )
    if resp.status_code != 200:
        logger.warning(
            "lemonsqueezy_api_error",
            endpoint=endpoint,
            status=resp.status_code,
        )
        return []
    data = resp.json()
    return data.get("data", []) if isinstance(data, dict) else []


async def check_lemonsqueezy_refunds(
    api_key: str,
    store_id: str | None = None,
    period_days: int = 7,
    refund_warning: float = DEFAULT_REFUND_RATE_WARNING,
    refund_critical: float = DEFAULT_REFUND_RATE_CRITICAL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """LemonSqueezy 환불율 체크

    LemonSqueezy는 orders에 refunded 상태가 포함됨.

    Returns:
        {provider, total_orders, total_refunds, refund_rate, issues, checked_at}
    """
    result: dict[str, Any] = {
        "provider": "lemonsqueezy",
        "check_type": "refund_rate",
        "total_orders": 0,
        "total_refunds": 0,
        "refund_rate": 0.0,
        "refund_amount": 0,
        "order_amount": 0,
        "period_days": period_days,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            params: dict[str, str] = {"page[size]": "100"}
            if store_id:
                params["filter[store_id]"] = store_id

            orders = await _lemonsqueezy_list(client, "orders", api_key, params)

            # 기간 필터 (created_at 기준)
            cutoff = datetime.now(timezone.utc).timestamp() - (period_days * 86400)
            recent_orders = []
            for o in orders:
                attrs = o.get("attributes", {})
                created = attrs.get("created_at", "")
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if dt.timestamp() >= cutoff:
                        recent_orders.append(attrs)
                except (ValueError, AttributeError):
                    continue

            result["total_orders"] = len(recent_orders)
            refunded = [o for o in recent_orders if o.get("status") == "refunded"]
            result["total_refunds"] = len(refunded)
            result["order_amount"] = sum(
                o.get("total", 0) for o in recent_orders
            )
            result["refund_amount"] = sum(
                o.get("total", 0) for o in refunded
            )

            if result["total_orders"] > 0:
                rate = (result["total_refunds"] / result["total_orders"]) * 100
                result["refund_rate"] = round(rate, 2)

                severity = _severity_for_rate(rate, refund_warning, refund_critical)
                if severity:
                    result["issues"].append({
                        "type": "refund_rate_high",
                        "severity": severity,
                        "message": (
                            f"환불율 {rate:.1f}% "
                            f"({result['total_refunds']}/{result['total_orders']}건 / "
                            f"{period_days}일)"
                        ),
                    })

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "medium",
            "message": f"LemonSqueezy API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error",
            "severity": "medium",
            "message": f"LemonSqueezy API 오류: {str(e)[:200]}",
        })

    return result


async def check_lemonsqueezy_churn(
    api_key: str,
    store_id: str | None = None,
    period_days: int = 30,
    churn_warning: float = DEFAULT_CHURN_RATE_WARNING,
    churn_critical: float = DEFAULT_CHURN_RATE_CRITICAL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """LemonSqueezy 구독 이탈률 체크

    Returns:
        {provider, active_subscriptions, canceled_in_period, churn_rate, issues, checked_at}
    """
    result: dict[str, Any] = {
        "provider": "lemonsqueezy",
        "check_type": "churn_rate",
        "active_subscriptions": 0,
        "canceled_in_period": 0,
        "churn_rate": 0.0,
        "period_days": period_days,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            params: dict[str, str] = {"page[size]": "100"}
            if store_id:
                params["filter[store_id]"] = store_id

            # 활성 구독
            active_params = {**params, "filter[status]": "active"}
            active = await _lemonsqueezy_list(
                client, "subscriptions", api_key, active_params,
            )
            result["active_subscriptions"] = len(active)

            # 취소 구독 (cancelled 상태)
            canceled_params = {**params, "filter[status]": "cancelled"}
            canceled_all = await _lemonsqueezy_list(
                client, "subscriptions", api_key, canceled_params,
            )

            # 기간 필터
            cutoff = datetime.now(timezone.utc).timestamp() - (period_days * 86400)
            canceled_recent = []
            for sub in canceled_all:
                attrs = sub.get("attributes", {})
                ends_at = attrs.get("ends_at", "")
                try:
                    dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                    if dt.timestamp() >= cutoff:
                        canceled_recent.append(attrs)
                except (ValueError, AttributeError):
                    continue

            result["canceled_in_period"] = len(canceled_recent)

            total_base = result["active_subscriptions"] + result["canceled_in_period"]
            if total_base > 0:
                rate = (result["canceled_in_period"] / total_base) * 100
                result["churn_rate"] = round(rate, 2)

                severity = _severity_for_rate(rate, churn_warning, churn_critical)
                if severity:
                    result["issues"].append({
                        "type": "churn_rate_high",
                        "severity": severity,
                        "message": (
                            f"구독 이탈률 {rate:.1f}% "
                            f"({result['canceled_in_period']}건 취소 / "
                            f"{total_base}건 전체 / {period_days}일)"
                        ),
                    })

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "medium",
            "message": f"LemonSqueezy API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error",
            "severity": "medium",
            "message": f"LemonSqueezy API 오류: {str(e)[:200]}",
        })

    return result


# ============================================================
# 3. Toss Payments
# ============================================================


async def _toss_request(
    client: httpx.AsyncClient,
    endpoint: str,
    secret_key: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Toss Payments API 호출 (Basic Auth)"""
    auth_str = base64.b64encode(f"{secret_key}:".encode()).decode()
    resp = await client.get(
        f"{TOSS_API_BASE}/{endpoint}",
        headers={"Authorization": f"Basic {auth_str}"},
        params=params or {},
    )
    if resp.status_code != 200:
        logger.warning(
            "toss_api_error",
            endpoint=endpoint,
            status=resp.status_code,
        )
        return {}
    return resp.json()


async def check_toss_refunds(
    secret_key: str,
    period_days: int = 7,
    refund_warning: float = DEFAULT_REFUND_RATE_WARNING,
    refund_critical: float = DEFAULT_REFUND_RATE_CRITICAL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Toss Payments 환불율 체크

    Toss는 transactions API로 승인/취소 내역을 조회.

    Returns:
        {provider, total_payments, total_cancels, refund_rate, issues, checked_at}
    """
    result: dict[str, Any] = {
        "provider": "toss",
        "check_type": "refund_rate",
        "total_payments": 0,
        "total_cancels": 0,
        "refund_rate": 0.0,
        "cancel_amount": 0,
        "payment_amount": 0,
        "period_days": period_days,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    start_date = datetime.fromtimestamp(
        time.time() - (period_days * 86400), tz=timezone.utc,
    ).strftime("%Y-%m-%dT00:00:00")
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59")

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await _toss_request(
                client, "transactions", secret_key,
                {
                    "startDate": start_date,
                    "endDate": end_date,
                    "limit": "100",
                },
            )

            transactions = data if isinstance(data, list) else data.get("data", [])
            if not isinstance(transactions, list):
                transactions = []

            done_txns = [
                t for t in transactions if t.get("status") == "DONE"
            ]
            canceled_txns = [
                t for t in transactions
                if t.get("status") in ("CANCELED", "PARTIAL_CANCELED")
            ]

            result["total_payments"] = len(done_txns) + len(canceled_txns)
            result["total_cancels"] = len(canceled_txns)
            result["payment_amount"] = sum(
                t.get("totalAmount", t.get("amount", 0)) for t in done_txns
            )
            result["cancel_amount"] = sum(
                t.get("totalAmount", t.get("amount", 0)) for t in canceled_txns
            )

            if result["total_payments"] > 0:
                rate = (result["total_cancels"] / result["total_payments"]) * 100
                result["refund_rate"] = round(rate, 2)

                severity = _severity_for_rate(rate, refund_warning, refund_critical)
                if severity:
                    result["issues"].append({
                        "type": "refund_rate_high",
                        "severity": severity,
                        "message": (
                            f"취소/환불율 {rate:.1f}% "
                            f"({result['total_cancels']}/{result['total_payments']}건 / "
                            f"{period_days}일)"
                        ),
                    })

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "medium",
            "message": f"Toss API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error",
            "severity": "medium",
            "message": f"Toss API 오류: {str(e)[:200]}",
        })

    return result


# ============================================================
# 4. 통합 감사 (프로바이더 자동 라우팅)
# ============================================================


async def run_payment_audit(
    provider: str,
    api_key: str,
    store_id: str | None = None,
    period_days: int = 7,
    churn_period_days: int = 30,
    refund_warning: float = DEFAULT_REFUND_RATE_WARNING,
    refund_critical: float = DEFAULT_REFUND_RATE_CRITICAL,
    failure_warning: float = DEFAULT_FAILURE_RATE_WARNING,
    failure_critical: float = DEFAULT_FAILURE_RATE_CRITICAL,
    churn_warning: float = DEFAULT_CHURN_RATE_WARNING,
    churn_critical: float = DEFAULT_CHURN_RATE_CRITICAL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """결제 종합 감사 — 프로바이더별 라우팅

    Args:
        provider: stripe / lemonsqueezy / toss
        api_key: 프로바이더별 API 키/시크릿 키
        store_id: LemonSqueezy 전용 스토어 ID (선택)
        period_days: 환불/실패 검사 기간
        churn_period_days: 이탈률 검사 기간

    Returns:
        {provider, refunds, failures, churn, total_issues, high_issues, elapsed_ms}
    """
    start = time.monotonic()
    provider = provider.lower().strip()

    audit: dict[str, Any] = {
        "provider": provider,
        "refunds": {},
        "failures": {},
        "churn": {},
        "total_issues": 0,
        "high_issues": 0,
        "all_issues": [],
        "elapsed_ms": 0,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    if provider == "stripe":
        audit["refunds"] = await check_stripe_refunds(
            api_key, period_days, refund_warning, refund_critical, timeout,
        )
        audit["failures"] = await check_stripe_failures(
            api_key, period_days, failure_warning, failure_critical, timeout,
        )
        audit["churn"] = await check_stripe_churn(
            api_key, churn_period_days, churn_warning, churn_critical, timeout,
        )

    elif provider == "lemonsqueezy":
        audit["refunds"] = await check_lemonsqueezy_refunds(
            api_key, store_id, period_days, refund_warning, refund_critical, timeout,
        )
        # LemonSqueezy는 별도 failure API 없음 (주문 실패는 결제게이트웨이 책임)
        audit["failures"] = {
            "provider": "lemonsqueezy",
            "check_type": "failure_rate",
            "note": "LemonSqueezy는 결제 실패를 별도 API로 노출하지 않음",
            "issues": [],
        }
        audit["churn"] = await check_lemonsqueezy_churn(
            api_key, store_id, churn_period_days, churn_warning, churn_critical, timeout,
        )

    elif provider == "toss":
        audit["refunds"] = await check_toss_refunds(
            api_key, period_days, refund_warning, refund_critical, timeout,
        )
        # Toss는 결제 실패 내역 별도 조회 (paymentKey 기반 개별 조회만 가능)
        audit["failures"] = {
            "provider": "toss",
            "check_type": "failure_rate",
            "note": "Toss는 결제 실패를 transactions 목록에서 직접 집계 불가 (webhook 방식 권장)",
            "issues": [],
        }
        # Toss는 구독 결제 API 별도 (billings) — 기본 미지원
        audit["churn"] = {
            "provider": "toss",
            "check_type": "churn_rate",
            "note": "Toss 구독 이탈률은 billings API 별도 구현 필요",
            "issues": [],
        }

    else:
        audit["all_issues"] = [{
            "type": "unsupported_provider",
            "severity": "high",
            "message": f"지원하지 않는 결제 프로바이더: '{provider}'. 허용: stripe/lemonsqueezy/toss",
        }]
        audit["total_issues"] = 1
        audit["high_issues"] = 1
        audit["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)
        return audit

    # 이슈 통합
    all_issues = (
        audit["refunds"].get("issues", [])
        + audit["failures"].get("issues", [])
        + audit["churn"].get("issues", [])
    )
    audit["all_issues"] = all_issues
    audit["total_issues"] = len(all_issues)
    audit["high_issues"] = len([i for i in all_issues if i.get("severity") == "high"])
    audit["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)

    return audit
