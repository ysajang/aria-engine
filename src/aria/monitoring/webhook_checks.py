"""ARIA Engine - Webhook Reconciliation Checks

결제 웹훅 누락 감지 로직 (ToolExecutor + cron 공용)
- reconcile_lemonsqueezy: LS API 주문 목록 vs DB purchases 대조
- reconcile_stripe: Stripe API 결제 목록 vs DB 대조
- run_webhook_reconciliation: 제품별 통합 실행

설계 원칙:
- AI API 호출 0 — 순수 HTTP API 호출 + 비교 로직
- 결제 provider API (read-only) + Supabase DB 조회만
- 누락 건 발견 시 issues 배열에 추가 → AlertManager 연동
- 자동 복구(retry webhook)는 하지 않음 → 알림만 (Human-in-the-Loop)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

# === API Base URLs ===
LEMONSQUEEZY_API_BASE = "https://api.lemonsqueezy.com/v1"
STRIPE_API_BASE = "https://api.stripe.com/v1"

# === Default Settings ===
DEFAULT_LOOKBACK_MINUTES = 30   # 최근 N분 내 주문 확인
DEFAULT_GRACE_MINUTES = 5       # 웹훅 도착 유예 시간 (이 시간 이내면 정상 대기)
DEFAULT_TIMEOUT = 15.0


async def reconcile_lemonsqueezy(
    api_key: str,
    store_id: str,
    supabase_url: str,
    supabase_service_key: str,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """LemonSqueezy 주문 vs DB purchases 대조

    1. LS API에서 최근 N분 내 주문 조회
    2. Supabase purchases 테이블에서 ls_order_id로 매칭
    3. 매칭 안 되는 건 = 웹훅 누락 의심

    Returns:
        {provider, ls_orders, db_matches, missing, grace_period, issues, severity}
    """
    result: dict[str, Any] = {
        "provider": "lemonsqueezy",
        "store_id": store_id,
        "lookback_minutes": lookback_minutes,
        "ls_orders": 0,
        "db_matches": 0,
        "missing": [],
        "grace_period": [],
        "issues": [],
        "severity": "none",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=lookback_minutes)
    grace_cutoff = now - timedelta(minutes=grace_minutes)

    try:
        # 1. LS API에서 최근 주문 조회
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{LEMONSQUEEZY_API_BASE}/orders",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/vnd.api+json",
                },
                params={
                    "filter[store_id]": store_id,
                    "sort": "-created_at",
                    "page[size]": 50,
                },
            )

            if resp.status_code != 200:
                result["issues"].append({
                    "severity": "medium",
                    "message": f"LemonSqueezy API 호출 실패: HTTP {resp.status_code}",
                })
                result["severity"] = "medium"
                return result

            ls_data = resp.json()
            orders = ls_data.get("data", [])

        # 최근 N분 내 주문만 필터
        recent_orders: list[dict[str, Any]] = []
        for order in orders:
            created_at = order.get("attributes", {}).get("created_at", "")
            if created_at and created_at >= since.isoformat():
                recent_orders.append({
                    "order_id": order.get("id", ""),
                    "created_at": created_at,
                    "total": order.get("attributes", {}).get("total", 0),
                    "status": order.get("attributes", {}).get("status", ""),
                    "user_email": order.get("attributes", {}).get("user_email", ""),
                })

        result["ls_orders"] = len(recent_orders)

        if not recent_orders:
            return result

        # 2. Supabase에서 ls_order_id 매칭 확인
        order_ids = [o["order_id"] for o in recent_orders]

        async with httpx.AsyncClient(timeout=timeout) as client:
            # PostgREST 쿼리: purchases 테이블에서 ls_order_id IN (...)
            resp = await client.get(
                f"{supabase_url}/rest/v1/purchases",
                headers={
                    "apikey": supabase_service_key,
                    "Authorization": f"Bearer {supabase_service_key}",
                },
                params={
                    "select": "ls_order_id",
                    "ls_order_id": f"in.({','.join(order_ids)})",
                },
            )

            if resp.status_code == 200:
                db_orders = resp.json()
                matched_ids = {row.get("ls_order_id") for row in db_orders}
            else:
                logger.warning(
                    "webhook_recon_db_error",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                matched_ids = set()
                result["issues"].append({
                    "severity": "low",
                    "message": f"Supabase 조회 실패: HTTP {resp.status_code}",
                })

        result["db_matches"] = len(matched_ids)

        # 3. 누락 건 분류
        for order in recent_orders:
            oid = order["order_id"]
            if oid in matched_ids:
                continue

            created = order["created_at"]
            if created >= grace_cutoff.isoformat():
                # 유예 기간 내 → 아직 웹훅 도착 대기
                result["grace_period"].append(order)
            else:
                # 유예 기간 초과 → 누락 의심
                result["missing"].append(order)

        if result["missing"]:
            count = len(result["missing"])
            result["severity"] = "high" if count >= 3 else "medium"
            result["issues"].append({
                "severity": result["severity"],
                "message": (
                    f"웹훅 누락 의심 {count}건 (유예 {grace_minutes}분 초과) — "
                    f"주문 ID: {', '.join(o['order_id'] for o in result['missing'][:5])}"
                ),
            })

    except httpx.TimeoutException:
        result["issues"].append({
            "severity": "medium",
            "message": "LemonSqueezy API 타임아웃",
        })
        result["severity"] = "medium"
    except Exception as e:
        logger.error("webhook_recon_error", error=str(e))
        result["issues"].append({
            "severity": "low",
            "message": f"Reconciliation 실행 실패: {type(e).__name__}",
        })

    return result


async def reconcile_stripe(
    api_key: str,
    supabase_url: str,
    supabase_service_key: str,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Stripe 결제 vs DB 대조

    Returns:
        {provider, stripe_charges, db_matches, missing, grace_period, issues, severity}
    """
    result: dict[str, Any] = {
        "provider": "stripe",
        "lookback_minutes": lookback_minutes,
        "stripe_charges": 0,
        "db_matches": 0,
        "missing": [],
        "grace_period": [],
        "issues": [],
        "severity": "none",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=lookback_minutes)
    grace_cutoff = now - timedelta(minutes=grace_minutes)
    since_ts = int(since.timestamp())

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{STRIPE_API_BASE}/charges",
                headers={"Authorization": f"Bearer {api_key}"},
                params={
                    "created[gte]": since_ts,
                    "limit": 50,
                },
            )

            if resp.status_code != 200:
                result["issues"].append({
                    "severity": "medium",
                    "message": f"Stripe API 호출 실패: HTTP {resp.status_code}",
                })
                result["severity"] = "medium"
                return result

            charges_data = resp.json()
            charges = charges_data.get("data", [])

        # 성공한 결제만 필터
        recent_charges: list[dict[str, Any]] = []
        for charge in charges:
            if charge.get("status") != "succeeded":
                continue

            created_ts = charge.get("created", 0)
            created_iso = datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat()
            recent_charges.append({
                "charge_id": charge.get("id", ""),
                "created_at": created_iso,
                "amount": charge.get("amount", 0),
                "currency": charge.get("currency", ""),
                "email": charge.get("billing_details", {}).get("email", ""),
            })

        result["stripe_charges"] = len(recent_charges)

        if not recent_charges:
            return result

        # Supabase 조회 (stripe_charge_id 필드 가정)
        charge_ids = [c["charge_id"] for c in recent_charges]

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{supabase_url}/rest/v1/purchases",
                headers={
                    "apikey": supabase_service_key,
                    "Authorization": f"Bearer {supabase_service_key}",
                },
                params={
                    "select": "stripe_charge_id",
                    "stripe_charge_id": f"in.({','.join(charge_ids)})",
                },
            )

            matched_ids = set()
            if resp.status_code == 200:
                db_rows = resp.json()
                matched_ids = {row.get("stripe_charge_id") for row in db_rows}

        result["db_matches"] = len(matched_ids)

        for charge in recent_charges:
            cid = charge["charge_id"]
            if cid in matched_ids:
                continue

            if charge["created_at"] >= grace_cutoff.isoformat():
                result["grace_period"].append(charge)
            else:
                result["missing"].append(charge)

        if result["missing"]:
            count = len(result["missing"])
            result["severity"] = "high" if count >= 3 else "medium"
            result["issues"].append({
                "severity": result["severity"],
                "message": (
                    f"웹훅 누락 의심 {count}건 — "
                    f"charge ID: {', '.join(c['charge_id'] for c in result['missing'][:5])}"
                ),
            })

    except httpx.TimeoutException:
        result["issues"].append({
            "severity": "medium",
            "message": "Stripe API 타임아웃",
        })
        result["severity"] = "medium"
    except Exception as e:
        logger.error("stripe_recon_error", error=str(e))
        result["issues"].append({
            "severity": "low",
            "message": f"Reconciliation 실행 실패: {type(e).__name__}",
        })

    return result


async def run_webhook_reconciliation(
    provider: str,
    api_key: str,
    supabase_url: str,
    supabase_service_key: str,
    store_id: str | None = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
) -> dict[str, Any]:
    """통합 reconciliation 실행"""
    if provider == "lemonsqueezy":
        if not store_id:
            return {
                "error": "LemonSqueezy reconciliation에는 store_id 필수",
                "severity": "none",
                "issues": [],
            }
        return await reconcile_lemonsqueezy(
            api_key=api_key,
            store_id=store_id,
            supabase_url=supabase_url,
            supabase_service_key=supabase_service_key,
            lookback_minutes=lookback_minutes,
        )
    elif provider == "stripe":
        return await reconcile_stripe(
            api_key=api_key,
            supabase_url=supabase_url,
            supabase_service_key=supabase_service_key,
            lookback_minutes=lookback_minutes,
        )
    else:
        return {
            "error": f"지원하지 않는 결제 프로바이더: {provider}",
            "severity": "none",
            "issues": [],
        }
