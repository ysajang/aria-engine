"""ARIA Engine - DB Monitoring Tests

Product Connector Step 4 테스트
- check_db_health: 구조 검증 + 에러 핸들링
- check_slow_queries: 구조 검증
- check_rls_policies: 구조 검증
- check_db_lints: 구조 검증
- run_db_audit: 통합 구조
- DbAuditTool: ToolExecutor
- AlertType.DB_ISSUE: 알림
- ARIA_RPC_INSTALL_SQL: SQL 존재 확인
"""

from __future__ import annotations

import pytest

from aria.monitoring.db_checks import (
    ARIA_RPC_INSTALL_SQL,
    check_db_health,
    check_db_lints,
    check_rls_policies,
    check_slow_queries,
    run_db_audit,
)
from aria.tools.mcp.db_monitor_tools import DbAuditTool
from aria.alerts.alert_types import AlertType, ALERT_EMOJI, DEFAULT_COOLDOWNS


# === 접근 불가 설정 (실제 Supabase 연결 없이 테스트) ===
FAKE_REF = "fake-project-ref-12345"
FAKE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.fake-service-role-key"
FAKE_TOKEN = "sbp_fake_access_token_1234567890"


# ===================================================================
# 1. check_db_health 구조 테스트
# ===================================================================


class TestDbHealthResult:
    """check_db_health 반환 구조 검증"""

    @pytest.mark.asyncio
    async def test_result_structure(self):
        result = await check_db_health(FAKE_REF, FAKE_KEY, timeout=2.0)
        assert "project_ref" in result
        assert "status" in result
        assert "db_size" in result
        assert "db_size_bytes" in result
        assert "active_connections" in result
        assert "max_connections" in result
        assert "top_bloated_tables" in result
        assert "issues" in result
        assert "checked_at" in result
        assert isinstance(result["issues"], list)

    @pytest.mark.asyncio
    async def test_unreachable_returns_error_status(self):
        result = await check_db_health(FAKE_REF, FAKE_KEY, timeout=2.0)
        assert result["status"] in ("error", "timeout")
        assert len(result["issues"]) > 0

    @pytest.mark.asyncio
    async def test_project_ref_preserved(self):
        result = await check_db_health(FAKE_REF, FAKE_KEY, timeout=2.0)
        assert result["project_ref"] == FAKE_REF


# ===================================================================
# 2. check_slow_queries 구조 테스트
# ===================================================================


class TestSlowQueriesResult:
    """check_slow_queries 반환 구조 검증"""

    @pytest.mark.asyncio
    async def test_result_structure(self):
        result = await check_slow_queries(FAKE_REF, FAKE_KEY, timeout=2.0)
        assert "project_ref" in result
        assert "slow_queries" in result
        assert "total_found" in result
        assert "issues" in result
        assert isinstance(result["slow_queries"], list)

    @pytest.mark.asyncio
    async def test_default_parameters(self):
        """기본 파라미터 확인"""
        result = await check_slow_queries(
            FAKE_REF, FAKE_KEY,
            min_mean_ms=500.0,
            min_calls=5,
            timeout=2.0,
        )
        assert "project_ref" in result


# ===================================================================
# 3. check_rls_policies 구조 테스트
# ===================================================================


class TestRlsCheckResult:
    """check_rls_policies 반환 구조 검증"""

    @pytest.mark.asyncio
    async def test_result_structure(self):
        result = await check_rls_policies(FAKE_REF, FAKE_KEY, timeout=2.0)
        assert "project_ref" in result
        assert "unprotected_tables" in result
        assert "total_tables" in result
        assert "issues" in result
        assert isinstance(result["unprotected_tables"], list)


# ===================================================================
# 4. check_db_lints 구조 테스트
# ===================================================================


class TestDbLintsResult:
    """check_db_lints 반환 구조 검증"""

    @pytest.mark.asyncio
    async def test_result_structure(self):
        result = await check_db_lints(FAKE_REF, FAKE_TOKEN, timeout=2.0)
        assert "project_ref" in result
        assert "lints" in result
        assert "total_lints" in result
        assert "error_count" in result
        assert "warning_count" in result
        assert "issues" in result

    @pytest.mark.asyncio
    async def test_auth_failure_recorded(self):
        """인증 실패 시 이슈에 기록"""
        result = await check_db_lints(FAKE_REF, FAKE_TOKEN, timeout=2.0)
        # 실제 API 호출 시 401 또는 connection error
        assert len(result["issues"]) > 0


# ===================================================================
# 5. run_db_audit 통합 구조 테스트
# ===================================================================


class TestDbAuditResult:
    """run_db_audit 반환 구조 검증"""

    @pytest.mark.asyncio
    async def test_result_structure(self):
        result = await run_db_audit(FAKE_REF, FAKE_KEY, timeout=2.0)
        assert "project_ref" in result
        assert "health" in result
        assert "slow_queries" in result
        assert "rls" in result
        assert "lints" in result
        assert "total_issues" in result
        assert "high_issues" in result
        assert "all_issues" in result
        assert "elapsed_ms" in result
        assert isinstance(result["all_issues"], list)

    @pytest.mark.asyncio
    async def test_without_access_token(self):
        """access_token 없으면 린트 스킵"""
        result = await run_db_audit(FAKE_REF, FAKE_KEY, access_token=None, timeout=2.0)
        lints = result["lints"]
        assert lints.get("lints", []) == []

    @pytest.mark.asyncio
    async def test_with_access_token(self):
        """access_token 있으면 린트 시도"""
        result = await run_db_audit(FAKE_REF, FAKE_KEY, access_token=FAKE_TOKEN, timeout=2.0)
        assert "lints" in result


# ===================================================================
# 6. DbAuditTool ToolExecutor
# ===================================================================


class TestDbAuditTool:
    """DbAuditTool ToolExecutor"""

    def test_definition(self):
        tool = DbAuditTool()
        defn = tool.get_definition()
        assert defn.name == "db_audit"
        assert "DB" in defn.description or "Supabase" in defn.description
        assert len(defn.parameters) == 3
        assert defn.parameters[0].name == "project_ref"

    @pytest.mark.asyncio
    async def test_empty_project_ref(self):
        tool = DbAuditTool()
        result = await tool.execute({"project_ref": ""})
        assert result.success is False
        assert "비어있습니다" in result.error

    @pytest.mark.asyncio
    async def test_no_service_role_key(self, monkeypatch):
        """service_role_key 없으면 에러"""
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        tool = DbAuditTool()
        result = await tool.execute({"project_ref": FAKE_REF})
        assert result.success is False
        assert "service_role_key" in result.error

    @pytest.mark.asyncio
    async def test_with_service_role_key(self, monkeypatch):
        """service_role_key 있으면 실행 (네트워크 실패해도 성공 반환)"""
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", FAKE_KEY)
        tool = DbAuditTool()
        result = await tool.execute({"project_ref": FAKE_REF})
        assert result.success is True
        assert result.output is not None

    def test_llm_format(self):
        tool = DbAuditTool()
        defn = tool.get_definition()
        llm_tool = defn.to_llm_tool()
        assert llm_tool["type"] == "function"
        assert llm_tool["function"]["name"] == "db_audit"


# ===================================================================
# 7. AlertType.DB_ISSUE
# ===================================================================


class TestDbAlertType:
    """DB 알림 타입"""

    def test_enum_exists(self):
        assert AlertType.DB_ISSUE == "db_issue"

    def test_emoji(self):
        assert AlertType.DB_ISSUE in ALERT_EMOJI
        assert ALERT_EMOJI[AlertType.DB_ISSUE] == "🗄️"

    def test_cooldown(self):
        assert AlertType.DB_ISSUE in DEFAULT_COOLDOWNS
        assert DEFAULT_COOLDOWNS[AlertType.DB_ISSUE] == 3600

    def test_alert_manager_has_method(self):
        from aria.alerts.alert_manager import AlertManager
        assert hasattr(AlertManager, "check_db_issue")


# ===================================================================
# 8. AlertManager.check_db_issue 로직
# ===================================================================


class TestCheckDbIssue:
    """AlertManager.check_db_issue 판정 로직"""

    @pytest.mark.asyncio
    async def test_no_alert_when_disabled(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(bot_token="", chat_id="", enabled=False)
        result = await mgr.check_db_issue(
            project_ref="test",
            total_issues=5,
            high_issues=3,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_alert_when_no_high(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(bot_token="test", chat_id="123", enabled=True)
        result = await mgr.check_db_issue(
            project_ref="test",
            total_issues=3,
            high_issues=0,
        )
        assert result is None


# ===================================================================
# 9. RPC Install SQL
# ===================================================================


class TestAriaRpcSql:
    """ARIA RPC 설치 SQL 검증"""

    def test_sql_exists(self):
        assert len(ARIA_RPC_INSTALL_SQL) > 100

    def test_contains_all_functions(self):
        assert "aria_db_size" in ARIA_RPC_INSTALL_SQL
        assert "aria_connection_stats" in ARIA_RPC_INSTALL_SQL
        assert "aria_slow_queries" in ARIA_RPC_INSTALL_SQL
        assert "aria_rls_check" in ARIA_RPC_INSTALL_SQL
        assert "aria_dead_tuples" in ARIA_RPC_INSTALL_SQL

    def test_security_definer(self):
        """모든 함수가 SECURITY DEFINER"""
        assert ARIA_RPC_INSTALL_SQL.count("SECURITY DEFINER") == 5

    def test_revoke_public(self):
        """PUBLIC 권한 제거"""
        assert ARIA_RPC_INSTALL_SQL.count("REVOKE ALL") == 5


# ===================================================================
# 10. app.py 등록 확인
# ===================================================================


class TestAppDbRegistration:
    """app.py에 DbAuditTool 등록 코드 존재 확인"""

    def test_db_tool_importable(self):
        from aria.tools.mcp.db_monitor_tools import DbAuditTool
        tool = DbAuditTool()
        assert tool.get_definition().name == "db_audit"

    def test_db_checks_importable(self):
        from aria.monitoring.db_checks import (
            check_db_health,
            check_slow_queries,
            check_rls_policies,
            check_db_lints,
            run_db_audit,
        )
        assert callable(check_db_health)
        assert callable(run_db_audit)
