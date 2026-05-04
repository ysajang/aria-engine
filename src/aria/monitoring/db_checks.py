"""ARIA Engine - Supabase DB Monitoring Checks

Supabase 프로젝트 DB 감시 로직 (ToolExecutor + cron 스크립트 공용)
- check_db_health: 서비스 헬스 + DB 사이즈 + 커넥션 수
- check_slow_queries: pg_stat_statements 기반 슬로우 쿼리 감지
- check_rls_policies: RLS 미적용 public 테이블 감지
- check_db_lints: Management API DB 린트 (unindexed FK 등)
- run_db_audit: 위 4가지 통합 실행

인증:
- Management API: SUPABASE_ACCESS_TOKEN (Personal Access Token)
- PostgREST RPC: SUPABASE_SERVICE_ROLE_KEY (service_role)

모든 함수는 dict 반환 → ToolResult.output / EventInput.data 양쪽에 사용
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

# Supabase API base URLs
MANAGEMENT_API_BASE = "https://api.supabase.com"


# ============================================================
# 1. DB Health — 서비스 상태 + DB 사이즈 + 커넥션 수
# ============================================================


async def check_db_health(
    project_ref: str,
    service_role_key: str,
    access_token: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Supabase 프로젝트 DB 헬스체크

    검사 항목:
    - PostgREST 연결 가능 여부
    - DB 사이즈 (pg_database_size)
    - 활성 커넥션 수 (pg_stat_activity)
    - 데드 튜플 상위 테이블 (pg_stat_user_tables)

    Returns:
        {project_ref, status, db_size, db_size_bytes, active_connections,
         max_connections, top_bloated_tables, issues, checked_at}
    """
    result: dict[str, Any] = {
        "project_ref": project_ref,
        "status": "unknown",
        "db_size": "unknown",
        "db_size_bytes": 0,
        "active_connections": 0,
        "max_connections": 0,
        "top_bloated_tables": [],
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    supabase_url = f"https://{project_ref}.supabase.co"
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 1. DB 사이즈
            size_resp = await client.post(
                f"{supabase_url}/rest/v1/rpc/aria_db_size",
                headers=headers,
                json={},
            )
            if size_resp.status_code == 200:
                size_data = size_resp.json()
                if isinstance(size_data, list) and size_data:
                    result["db_size"] = size_data[0].get("size_pretty", "unknown")
                    result["db_size_bytes"] = size_data[0].get("size_bytes", 0)
                elif isinstance(size_data, dict):
                    result["db_size"] = size_data.get("size_pretty", "unknown")
                    result["db_size_bytes"] = size_data.get("size_bytes", 0)
                result["status"] = "healthy"
            elif size_resp.status_code == 404:
                # RPC 함수 미설치
                result["issues"].append({
                    "type": "rpc_missing",
                    "severity": "medium",
                    "message": "aria_db_size RPC 함수 미설치 (SQL 실행 필요)",
                })
                result["status"] = "degraded"
            else:
                result["issues"].append({
                    "type": "db_connection_error",
                    "severity": "high",
                    "message": f"PostgREST 연결 실패: HTTP {size_resp.status_code}",
                })
                result["status"] = "error"
                return result

            # 2. 활성 커넥션 수
            conn_resp = await client.post(
                f"{supabase_url}/rest/v1/rpc/aria_connection_stats",
                headers=headers,
                json={},
            )
            if conn_resp.status_code == 200:
                conn_data = conn_resp.json()
                if isinstance(conn_data, list) and conn_data:
                    result["active_connections"] = conn_data[0].get("active", 0)
                    result["max_connections"] = conn_data[0].get("max_conn", 0)
                elif isinstance(conn_data, dict):
                    result["active_connections"] = conn_data.get("active", 0)
                    result["max_connections"] = conn_data.get("max_conn", 0)

                # 커넥션 사용률 80% 이상 경고
                max_conn = result["max_connections"]
                active = result["active_connections"]
                if max_conn > 0 and active / max_conn > 0.8:
                    result["issues"].append({
                        "type": "connection_saturation",
                        "severity": "high",
                        "message": f"커넥션 사용률 {active}/{max_conn} ({active/max_conn:.0%})",
                    })

            # 3. 데드 튜플 상위 테이블
            bloat_resp = await client.post(
                f"{supabase_url}/rest/v1/rpc/aria_dead_tuples",
                headers=headers,
                json={},
            )
            if bloat_resp.status_code == 200:
                bloat_data = bloat_resp.json()
                if isinstance(bloat_data, list):
                    result["top_bloated_tables"] = bloat_data[:5]
                    # 10만 데드 튜플 이상인 테이블 경고
                    for table in bloat_data[:5]:
                        dead = table.get("dead_tuples", 0)
                        if dead >= 100000:
                            result["issues"].append({
                                "type": "dead_tuples",
                                "severity": "medium",
                                "message": (
                                    f"테이블 {table.get('table_name', '?')}: "
                                    f"데드 튜플 {dead:,}개 (VACUUM 필요)"
                                ),
                            })

    except httpx.TimeoutException:
        result["status"] = "timeout"
        result["issues"].append({
            "type": "timeout",
            "severity": "high",
            "message": f"DB 연결 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["status"] = "error"
        result["issues"].append({
            "type": "connection_error",
            "severity": "high",
            "message": str(e)[:200],
        })

    return result


# ============================================================
# 2. Slow Queries — pg_stat_statements 기반
# ============================================================


async def check_slow_queries(
    project_ref: str,
    service_role_key: str,
    min_mean_ms: float = 1000.0,
    min_calls: int = 10,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """슬로우 쿼리 감지 (pg_stat_statements)

    Args:
        min_mean_ms: 평균 실행시간 최소 기준 (ms)
        min_calls: 최소 호출 횟수 (노이즈 제거)

    Returns:
        {project_ref, slow_queries, total_found, issues, checked_at}
    """
    result: dict[str, Any] = {
        "project_ref": project_ref,
        "slow_queries": [],
        "total_found": 0,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    supabase_url = f"https://{project_ref}.supabase.co"
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{supabase_url}/rest/v1/rpc/aria_slow_queries",
                headers=headers,
                json={
                    "min_mean_ms": min_mean_ms,
                    "min_calls": min_calls,
                },
            )

            if resp.status_code == 200:
                queries = resp.json()
                if isinstance(queries, list):
                    result["slow_queries"] = queries[:10]  # 상위 10개
                    result["total_found"] = len(queries)

                    for q in queries[:5]:
                        mean_ms = q.get("mean_exec_time_ms", 0)
                        calls = q.get("calls", 0)
                        result["issues"].append({
                            "type": "slow_query",
                            "severity": "high" if mean_ms > 5000 else "medium",
                            "message": (
                                f"평균 {mean_ms:.0f}ms / {calls}회 호출: "
                                f"{q.get('query', '?')[:100]}"
                            ),
                        })
            elif resp.status_code == 404:
                result["issues"].append({
                    "type": "rpc_missing",
                    "severity": "medium",
                    "message": "aria_slow_queries RPC 함수 미설치",
                })

    except Exception as e:
        result["issues"].append({
            "type": "error",
            "severity": "high",
            "message": str(e)[:200],
        })

    return result


# ============================================================
# 3. RLS Policies — public 테이블 RLS 미적용 감지
# ============================================================


async def check_rls_policies(
    project_ref: str,
    service_role_key: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """RLS 미적용 public 테이블 감지

    Returns:
        {project_ref, unprotected_tables, total_tables, issues, checked_at}
    """
    result: dict[str, Any] = {
        "project_ref": project_ref,
        "unprotected_tables": [],
        "total_tables": 0,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    supabase_url = f"https://{project_ref}.supabase.co"
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{supabase_url}/rest/v1/rpc/aria_rls_check",
                headers=headers,
                json={},
            )

            if resp.status_code == 200:
                tables = resp.json()
                if isinstance(tables, list):
                    unprotected = [t for t in tables if not t.get("rls_enabled", True)]
                    result["unprotected_tables"] = unprotected
                    result["total_tables"] = len(tables)

                    for t in unprotected:
                        table_name = t.get("table_name", "?")
                        result["issues"].append({
                            "type": "rls_missing",
                            "severity": "high",
                            "message": f"테이블 '{table_name}': RLS 미적용 (보안 위험)",
                        })
            elif resp.status_code == 404:
                result["issues"].append({
                    "type": "rpc_missing",
                    "severity": "medium",
                    "message": "aria_rls_check RPC 함수 미설치",
                })

    except Exception as e:
        result["issues"].append({
            "type": "error",
            "severity": "high",
            "message": str(e)[:200],
        })

    return result


# ============================================================
# 4. DB Lints — Management API (unindexed FK 등)
# ============================================================


async def check_db_lints(
    project_ref: str,
    access_token: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Supabase Management API DB 린트

    unindexed_foreign_keys / unused_indexes 등 구조 문제 감지

    Args:
        access_token: Supabase Personal Access Token (sbp_...)

    Returns:
        {project_ref, lints, total_lints, error_count, warning_count, issues, checked_at}
    """
    result: dict[str, Any] = {
        "project_ref": project_ref,
        "lints": [],
        "total_lints": 0,
        "error_count": 0,
        "warning_count": 0,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{MANAGEMENT_API_BASE}/v1/projects/{project_ref}/database/lints",
                headers={
                    "Authorization": f"Bearer {access_token}",
                },
            )

            if resp.status_code == 200:
                data = resp.json()
                lints = data.get("lints", []) if isinstance(data, dict) else data
                if isinstance(lints, list):
                    result["lints"] = lints[:20]  # 상위 20개
                    result["total_lints"] = len(lints)

                    for lint in lints:
                        level = lint.get("level", "INFO")
                        if level == "ERROR":
                            result["error_count"] += 1
                        elif level == "WARN":
                            result["warning_count"] += 1

                        severity = "high" if level == "ERROR" else "medium" if level == "WARN" else "low"
                        result["issues"].append({
                            "type": f"lint_{lint.get('name', 'unknown')}",
                            "severity": severity,
                            "message": (
                                f"[{level}] {lint.get('title', '?')}: "
                                f"{lint.get('description', '')[:100]}"
                            ),
                        })
            elif resp.status_code == 401:
                result["issues"].append({
                    "type": "auth_failed",
                    "severity": "high",
                    "message": "Management API 인증 실패 (SUPABASE_ACCESS_TOKEN 확인)",
                })
            elif resp.status_code == 404:
                result["issues"].append({
                    "type": "endpoint_unavailable",
                    "severity": "low",
                    "message": "DB 린트 엔드포인트 미지원 (플랜 확인)",
                })
            else:
                result["issues"].append({
                    "type": "api_error",
                    "severity": "medium",
                    "message": f"Management API HTTP {resp.status_code}",
                })

    except Exception as e:
        result["issues"].append({
            "type": "error",
            "severity": "high",
            "message": str(e)[:200],
        })

    return result


# ============================================================
# 5. DB Audit (통합)
# ============================================================


async def run_db_audit(
    project_ref: str,
    service_role_key: str,
    access_token: str | None = None,
    min_slow_ms: float = 1000.0,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """DB 종합 감사 — health + slow queries + RLS + lints

    Returns:
        {project_ref, health, slow_queries, rls, lints, total_issues, high_issues, elapsed_ms}
    """
    start = time.monotonic()

    health = await check_db_health(project_ref, service_role_key, timeout=timeout)
    slow = await check_slow_queries(project_ref, service_role_key, min_mean_ms=min_slow_ms, timeout=timeout)
    rls = await check_rls_policies(project_ref, service_role_key, timeout=timeout)

    lints: dict[str, Any] = {"lints": [], "issues": []}
    if access_token:
        lints = await check_db_lints(project_ref, access_token, timeout=timeout)

    # 이슈 통합
    all_issues = (
        health.get("issues", [])
        + slow.get("issues", [])
        + rls.get("issues", [])
        + lints.get("issues", [])
    )
    high_issues = [i for i in all_issues if i.get("severity") == "high"]

    elapsed_ms = round((time.monotonic() - start) * 1000, 1)

    return {
        "project_ref": project_ref,
        "health": health,
        "slow_queries": slow,
        "rls": rls,
        "lints": lints,
        "total_issues": len(all_issues),
        "high_issues": len(high_issues),
        "all_issues": all_issues,
        "elapsed_ms": elapsed_ms,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
# SQL: RPC 함수 설치 스크립트
# ============================================================


ARIA_RPC_INSTALL_SQL = """
-- ARIA Engine DB 감시용 RPC 함수
-- Supabase SQL Editor에서 실행

-- 1. DB 사이즈
CREATE OR REPLACE FUNCTION aria_db_size()
RETURNS TABLE(size_pretty TEXT, size_bytes BIGINT) 
LANGUAGE sql SECURITY DEFINER
AS $$
  SELECT 
    pg_size_pretty(pg_database_size(current_database())) AS size_pretty,
    pg_database_size(current_database()) AS size_bytes;
$$;

-- 2. 커넥션 통계
CREATE OR REPLACE FUNCTION aria_connection_stats()
RETURNS TABLE(active INT, idle INT, max_conn INT)
LANGUAGE sql SECURITY DEFINER
AS $$
  SELECT 
    (SELECT count(*)::INT FROM pg_stat_activity WHERE state = 'active') AS active,
    (SELECT count(*)::INT FROM pg_stat_activity WHERE state = 'idle') AS idle,
    (SELECT setting::INT FROM pg_settings WHERE name = 'max_connections') AS max_conn;
$$;

-- 3. 슬로우 쿼리 (pg_stat_statements 필요)
CREATE OR REPLACE FUNCTION aria_slow_queries(
  min_mean_ms FLOAT DEFAULT 1000.0,
  min_calls INT DEFAULT 10
)
RETURNS TABLE(
  query TEXT, 
  calls BIGINT, 
  mean_exec_time_ms FLOAT, 
  total_exec_time_ms FLOAT,
  rows_per_call FLOAT
)
LANGUAGE sql SECURITY DEFINER
AS $$
  SELECT 
    query,
    calls,
    round(mean_exec_time::numeric, 2)::FLOAT AS mean_exec_time_ms,
    round(total_exec_time::numeric, 2)::FLOAT AS total_exec_time_ms,
    CASE WHEN calls > 0 THEN round((rows::numeric / calls), 2)::FLOAT ELSE 0 END AS rows_per_call
  FROM pg_stat_statements
  WHERE mean_exec_time >= min_mean_ms
    AND calls >= min_calls
    AND query NOT LIKE '%pg_stat%'
    AND query NOT LIKE '%aria_%'
  ORDER BY mean_exec_time DESC
  LIMIT 20;
$$;

-- 4. RLS 체크
CREATE OR REPLACE FUNCTION aria_rls_check()
RETURNS TABLE(table_name TEXT, rls_enabled BOOLEAN, has_policies BOOLEAN)
LANGUAGE sql SECURITY DEFINER
AS $$
  SELECT 
    t.tablename::TEXT AS table_name,
    t.rowsecurity AS rls_enabled,
    EXISTS(
      SELECT 1 FROM pg_policies p 
      WHERE p.schemaname = 'public' AND p.tablename = t.tablename
    ) AS has_policies
  FROM pg_tables t
  WHERE t.schemaname = 'public'
  ORDER BY t.tablename;
$$;

-- 5. 데드 튜플 (VACUUM 대상)
CREATE OR REPLACE FUNCTION aria_dead_tuples()
RETURNS TABLE(table_name TEXT, dead_tuples BIGINT, live_tuples BIGINT, last_vacuum TIMESTAMPTZ)
LANGUAGE sql SECURITY DEFINER
AS $$
  SELECT 
    schemaname || '.' || relname AS table_name,
    n_dead_tup AS dead_tuples,
    n_live_tup AS live_tuples,
    last_vacuum
  FROM pg_stat_user_tables
  WHERE n_dead_tup > 0
  ORDER BY n_dead_tup DESC
  LIMIT 10;
$$;

-- 권한: service_role만 호출 가능
REVOKE ALL ON FUNCTION aria_db_size() FROM PUBLIC;
REVOKE ALL ON FUNCTION aria_connection_stats() FROM PUBLIC;
REVOKE ALL ON FUNCTION aria_slow_queries(FLOAT, INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION aria_rls_check() FROM PUBLIC;
REVOKE ALL ON FUNCTION aria_dead_tuples() FROM PUBLIC;
"""
