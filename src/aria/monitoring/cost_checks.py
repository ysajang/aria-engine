"""ARIA Engine - Cost Optimization Monitoring Checks

서비스별 비용/사용량 추적 + 절감 제안 (ToolExecutor + cron 공용)
- check_vercel_usage: Vercel 대역폭/함수/빌드 사용량
- check_supabase_usage: Supabase DB/스토리지/대역폭 사용량
- check_api_costs: ARIA LLM API 비용 현황 + 추세
- run_cost_audit: 위 3가지 통합 + 절감 제안 생성

인증:
- Vercel: VERCEL_TOKEN (Bearer)
- Supabase: SUPABASE_ACCESS_TOKEN (Personal Access Token)
- ARIA: 내부 비용 추적 데이터 (직접 전달)

설계 원칙:
- Exit-Safe: API 읽기 전용
- 항상 활성화 (ProductFeature.COST_OPTIMIZE: None → 무조건)
- 보수적 추정: 절감 효과는 하한값 사용
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

VERCEL_API_BASE = "https://api.vercel.com"
SUPABASE_MGMT_BASE = "https://api.supabase.com"

# === Plan Limits (Free Tier) ===
VERCEL_FREE_LIMITS = {
    "bandwidth_gb": 100,
    "function_invocations": 100_000,
    "build_minutes": 6000,
    "image_optimizations": 1000,
}

SUPABASE_FREE_LIMITS = {
    "db_size_gb": 0.5,
    "storage_gb": 1.0,
    "bandwidth_gb": 5.0,
    "edge_invocations": 500_000,
}

# === Thresholds ===
DEFAULT_USAGE_WARNING = 70.0   # 사용량 70% → WARNING
DEFAULT_USAGE_CRITICAL = 90.0  # 사용량 90% → CRITICAL


def _usage_severity(
    used: float,
    limit: float,
    warning_pct: float = DEFAULT_USAGE_WARNING,
    critical_pct: float = DEFAULT_USAGE_CRITICAL,
) -> str | None:
    """사용량 비율 기반 심각도"""
    if limit <= 0:
        return None
    pct = (used / limit) * 100
    if pct >= critical_pct:
        return "high"
    if pct >= warning_pct:
        return "medium"
    return None


# ============================================================
# 1. Vercel Usage
# ============================================================


async def check_vercel_usage(
    vercel_token: str,
    team_id: str | None = None,
    period_days: int = 30,
    plan_limits: dict[str, float] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Vercel 사용량 체크

    Args:
        vercel_token: Vercel Auth Token (Bearer)
        team_id: Vercel Team ID (개인 계정이면 None)
        plan_limits: 플랜별 제한 (미지정 시 Free Tier)

    Returns:
        {provider, bandwidth_gb, function_invocations, build_minutes,
         usage_pct, issues, recommendations, checked_at}
    """
    limits = plan_limits or VERCEL_FREE_LIMITS

    result: dict[str, Any] = {
        "provider": "vercel",
        "check_type": "usage",
        "bandwidth_gb": 0.0,
        "function_invocations": 0,
        "build_minutes": 0,
        "image_optimizations": 0,
        "usage_pct": {},
        "plan_limits": limits,
        "period_days": period_days,
        "issues": [],
        "recommendations": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    headers = {"Authorization": f"Bearer {vercel_token}"}
    params: dict[str, str] = {}
    if team_id:
        params["teamId"] = team_id

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Vercel Usage API
            resp = await client.get(
                f"{VERCEL_API_BASE}/v1/usage",
                headers=headers,
                params=params,
            )

            if resp.status_code == 200:
                data = resp.json()
                usage = data.get("usage", data)

                bw = usage.get("bandwidth", {})
                result["bandwidth_gb"] = round(bw.get("used", 0) / (1024**3), 2) if isinstance(bw, dict) else 0

                fn = usage.get("serverlessFunctionExecution", {})
                result["function_invocations"] = fn.get("used", 0) if isinstance(fn, dict) else 0

                builds = usage.get("buildExecution", {})
                result["build_minutes"] = builds.get("used", 0) if isinstance(builds, dict) else 0

                img = usage.get("imageOptimization", {})
                result["image_optimizations"] = img.get("used", 0) if isinstance(img, dict) else 0

            elif resp.status_code == 401:
                result["issues"].append({
                    "type": "auth_failed",
                    "severity": "high",
                    "message": "Vercel API 인증 실패 (VERCEL_TOKEN 확인)",
                })
                return result
            elif resp.status_code == 403:
                result["issues"].append({
                    "type": "permission_denied",
                    "severity": "high",
                    "message": "Vercel API 권한 부족",
                })
                return result
            else:
                result["issues"].append({
                    "type": "api_error",
                    "severity": "medium",
                    "message": f"Vercel API HTTP {resp.status_code}",
                })
                return result

        # 사용량 비율 계산 + 이슈 생성
        checks = [
            ("bandwidth_gb", result["bandwidth_gb"], limits.get("bandwidth_gb", 0)),
            ("function_invocations", result["function_invocations"], limits.get("function_invocations", 0)),
            ("build_minutes", result["build_minutes"], limits.get("build_minutes", 0)),
        ]

        for name, used, limit in checks:
            if limit > 0:
                pct = round((used / limit) * 100, 1)
                result["usage_pct"][name] = pct

                severity = _usage_severity(used, limit)
                if severity:
                    result["issues"].append({
                        "type": f"{name}_high",
                        "severity": severity,
                        "message": f"Vercel {name}: {pct}% 사용 ({used}/{limit})",
                    })

        # 절감 제안
        if result["bandwidth_gb"] > limits.get("bandwidth_gb", 100) * 0.5:
            result["recommendations"].append(
                "CDN/이미지 최적화로 대역폭 절감 가능 (WebP/AVIF 변환 + lazy loading)"
            )
        if result["build_minutes"] > limits.get("build_minutes", 6000) * 0.5:
            result["recommendations"].append(
                "Turborepo 캐시 또는 빌드 스킵 조건 설정으로 빌드 시간 절감"
            )

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout", "severity": "medium",
            "message": f"Vercel API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error", "severity": "medium",
            "message": f"Vercel API 오류: {str(e)[:200]}",
        })

    return result


# ============================================================
# 2. Supabase Usage
# ============================================================


async def check_supabase_usage(
    access_token: str,
    project_ref: str,
    plan_limits: dict[str, float] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Supabase 프로젝트 사용량 체크

    Returns:
        {provider, db_size_gb, storage_gb, bandwidth_gb,
         edge_invocations, usage_pct, issues, recommendations, checked_at}
    """
    limits = plan_limits or SUPABASE_FREE_LIMITS

    result: dict[str, Any] = {
        "provider": "supabase",
        "check_type": "usage",
        "project_ref": project_ref,
        "db_size_gb": 0.0,
        "storage_gb": 0.0,
        "bandwidth_gb": 0.0,
        "edge_invocations": 0,
        "usage_pct": {},
        "plan_limits": limits,
        "issues": [],
        "recommendations": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{SUPABASE_MGMT_BASE}/v1/projects/{project_ref}/usage",
                headers=headers,
            )

            if resp.status_code == 200:
                data = resp.json()
                usage = data if isinstance(data, dict) else {}

                result["db_size_gb"] = round(usage.get("db_size", 0) / (1024**3), 3)
                result["storage_gb"] = round(usage.get("storage_size", 0) / (1024**3), 3)
                result["bandwidth_gb"] = round(usage.get("bandwidth", 0) / (1024**3), 3)
                result["edge_invocations"] = usage.get("edge_function_invocations", 0)

            elif resp.status_code == 401:
                result["issues"].append({
                    "type": "auth_failed", "severity": "high",
                    "message": "Supabase Management API 인증 실패 (SUPABASE_ACCESS_TOKEN 확인)",
                })
                return result
            elif resp.status_code == 404:
                result["issues"].append({
                    "type": "project_not_found", "severity": "high",
                    "message": f"Supabase 프로젝트 '{project_ref}' 미발견",
                })
                return result
            else:
                result["issues"].append({
                    "type": "api_error", "severity": "medium",
                    "message": f"Supabase API HTTP {resp.status_code}",
                })
                return result

        # 사용량 비율 + 이슈
        checks = [
            ("db_size_gb", result["db_size_gb"], limits.get("db_size_gb", 0)),
            ("storage_gb", result["storage_gb"], limits.get("storage_gb", 0)),
            ("bandwidth_gb", result["bandwidth_gb"], limits.get("bandwidth_gb", 0)),
            ("edge_invocations", result["edge_invocations"], limits.get("edge_invocations", 0)),
        ]

        for name, used, limit in checks:
            if limit > 0:
                pct = round((used / limit) * 100, 1)
                result["usage_pct"][name] = pct

                severity = _usage_severity(used, limit)
                if severity:
                    result["issues"].append({
                        "type": f"{name}_high",
                        "severity": severity,
                        "message": f"Supabase {name}: {pct}% 사용 ({used}/{limit})",
                    })

        # 절감 제안
        if result["db_size_gb"] > limits.get("db_size_gb", 0.5) * 0.6:
            result["recommendations"].append(
                "오래된 로그/이벤트 테이블 정리 또는 파티셔닝으로 DB 사이즈 절감"
            )
        if result["storage_gb"] > limits.get("storage_gb", 1.0) * 0.6:
            result["recommendations"].append(
                "미사용 스토리지 파일 정리 + 이미지 압축(WebP) 적용"
            )

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout", "severity": "medium",
            "message": f"Supabase API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error", "severity": "medium",
            "message": f"Supabase API 오류: {str(e)[:200]}",
        })

    return result


# ============================================================
# 3. API Costs (ARIA 내부 비용 데이터)
# ============================================================


async def check_api_costs(
    aria_base_url: str = "http://localhost:8100",
    api_key: str | None = None,
    daily_limit: float = 10.0,
    monthly_limit: float = 300.0,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """ARIA LLM API 비용 현황 (/v1/cost 엔드포인트)

    Returns:
        {provider, daily_cost, monthly_cost, daily_pct, monthly_pct,
         total_requests, total_cached_tokens, issues, recommendations, checked_at}
    """
    result: dict[str, Any] = {
        "provider": "aria_llm",
        "check_type": "api_costs",
        "daily_cost": 0.0,
        "monthly_cost": 0.0,
        "daily_pct": 0.0,
        "monthly_pct": 0.0,
        "daily_limit": daily_limit,
        "monthly_limit": monthly_limit,
        "total_requests": 0,
        "total_cached_tokens": 0,
        "issues": [],
        "recommendations": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{aria_base_url}/v1/cost",
                headers=headers,
            )

            if resp.status_code == 200:
                data = resp.json()
                result["daily_cost"] = data.get("daily_cost_usd", 0.0)
                result["monthly_cost"] = data.get("monthly_cost_usd", 0.0)
                result["total_requests"] = data.get("total_requests", 0)
                result["total_cached_tokens"] = data.get("total_cached_tokens", 0)

                # 비율 계산
                if daily_limit > 0:
                    result["daily_pct"] = round((result["daily_cost"] / daily_limit) * 100, 1)
                if monthly_limit > 0:
                    result["monthly_pct"] = round((result["monthly_cost"] / monthly_limit) * 100, 1)

                # 이슈 감지
                severity = _usage_severity(result["daily_cost"], daily_limit)
                if severity:
                    result["issues"].append({
                        "type": "daily_cost_high",
                        "severity": severity,
                        "message": (
                            f"일 비용 ${result['daily_cost']:.2f} "
                            f"({result['daily_pct']:.0f}% of ${daily_limit})"
                        ),
                    })

                severity = _usage_severity(result["monthly_cost"], monthly_limit)
                if severity:
                    result["issues"].append({
                        "type": "monthly_cost_high",
                        "severity": severity,
                        "message": (
                            f"월 비용 ${result['monthly_cost']:.2f} "
                            f"({result['monthly_pct']:.0f}% of ${monthly_limit})"
                        ),
                    })

                # 캐시 효율 체크
                if result["total_cached_tokens"] == 0 and result["total_requests"] > 10:
                    result["recommendations"].append(
                        "Prompt Caching 미작동 — 시스템 프롬프트 2048+ 토큰 확인"
                    )

            elif resp.status_code == 401:
                result["issues"].append({
                    "type": "auth_failed", "severity": "medium",
                    "message": "ARIA /v1/cost 인증 실패",
                })
            else:
                result["issues"].append({
                    "type": "api_error", "severity": "medium",
                    "message": f"ARIA /v1/cost HTTP {resp.status_code}",
                })

    except httpx.ConnectError:
        result["issues"].append({
            "type": "connection_error", "severity": "medium",
            "message": f"ARIA 서버 연결 실패 ({aria_base_url})",
        })
    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout", "severity": "medium",
            "message": f"ARIA API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "api_error", "severity": "medium",
            "message": f"ARIA API 오류: {str(e)[:200]}",
        })

    return result


# ============================================================
# 4. 통합 감사 + 절감 제안
# ============================================================


async def run_cost_audit(
    vercel_token: str | None = None,
    vercel_team_id: str | None = None,
    supabase_token: str | None = None,
    supabase_project_ref: str | None = None,
    aria_base_url: str = "http://localhost:8100",
    aria_api_key: str | None = None,
    daily_limit: float = 10.0,
    monthly_limit: float = 300.0,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """비용 종합 감사

    Returns:
        {vercel, supabase, api_costs, total_issues, high_issues,
         all_recommendations, elapsed_ms}
    """
    start = time.monotonic()

    audit: dict[str, Any] = {
        "vercel": {},
        "supabase": {},
        "api_costs": {},
        "total_issues": 0,
        "high_issues": 0,
        "all_issues": [],
        "all_recommendations": [],
        "elapsed_ms": 0,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    # Vercel
    if vercel_token:
        audit["vercel"] = await check_vercel_usage(
            vercel_token, vercel_team_id, timeout=timeout,
        )
    else:
        audit["vercel"] = {
            "provider": "vercel", "note": "VERCEL_TOKEN 미설정", "issues": [],
            "recommendations": [],
        }

    # Supabase
    if supabase_token and supabase_project_ref:
        audit["supabase"] = await check_supabase_usage(
            supabase_token, supabase_project_ref, timeout=timeout,
        )
    else:
        audit["supabase"] = {
            "provider": "supabase", "note": "SUPABASE_ACCESS_TOKEN 또는 project_ref 미설정",
            "issues": [], "recommendations": [],
        }

    # ARIA API Costs
    audit["api_costs"] = await check_api_costs(
        aria_base_url, aria_api_key, daily_limit, monthly_limit, timeout=timeout,
    )

    # 통합
    all_issues = (
        audit["vercel"].get("issues", [])
        + audit["supabase"].get("issues", [])
        + audit["api_costs"].get("issues", [])
    )
    all_recs = (
        audit["vercel"].get("recommendations", [])
        + audit["supabase"].get("recommendations", [])
        + audit["api_costs"].get("recommendations", [])
    )
    audit["all_issues"] = all_issues
    audit["total_issues"] = len(all_issues)
    audit["high_issues"] = len([i for i in all_issues if i.get("severity") == "high"])
    audit["all_recommendations"] = all_recs
    audit["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)

    return audit
