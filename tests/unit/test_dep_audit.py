"""ARIA Engine - Dependency Audit Tests

Product Connector Step 5 테스트
- _parse_repo_url: GitHub URL 파싱
- check_dependabot_alerts: 구조 검증
- run_dependency_audit: 통합 구조
- DependencyAuditTool: ToolExecutor
- AlertType.DEPENDENCY_VULN: 알림
"""

from __future__ import annotations

import pytest

from aria.monitoring.dep_checks import (
    _parse_repo_url,
    check_dependabot_alerts,
    run_dependency_audit,
)
from aria.tools.mcp.dep_audit_tools import DependencyAuditTool
from aria.alerts.alert_types import AlertType, ALERT_EMOJI, DEFAULT_COOLDOWNS


FAKE_TOKEN = "ghp_fake1234567890abcdef"


# ===================================================================
# 1. _parse_repo_url
# ===================================================================


class TestParseRepoUrl:
    """GitHub URL 파싱"""

    def test_https_url(self):
        result = _parse_repo_url("https://github.com/Testorum/testorum")
        assert result == ("Testorum", "testorum")

    def test_https_url_with_git(self):
        result = _parse_repo_url("https://github.com/Testorum/testorum.git")
        assert result == ("Testorum", "testorum")

    def test_trailing_slash(self):
        result = _parse_repo_url("https://github.com/owner/repo/")
        assert result == ("owner", "repo")

    def test_short_form(self):
        result = _parse_repo_url("Testorum/testorum")
        assert result == ("Testorum", "testorum")

    def test_no_protocol(self):
        result = _parse_repo_url("github.com/owner/repo")
        assert result == ("owner", "repo")

    def test_invalid_url(self):
        result = _parse_repo_url("not-a-url")
        assert result is None

    def test_empty(self):
        result = _parse_repo_url("")
        assert result is None

    def test_single_word(self):
        result = _parse_repo_url("justarepo")
        assert result is None


# ===================================================================
# 2. check_dependabot_alerts 구조
# ===================================================================


class TestDependabotAlertsResult:
    """check_dependabot_alerts 반환 구조"""

    @pytest.mark.asyncio
    async def test_result_structure(self):
        result = await check_dependabot_alerts(
            "https://github.com/Testorum/testorum.git",
            FAKE_TOKEN,
            timeout=3.0,
        )
        assert "repo_url" in result
        assert "owner" in result
        assert "repo" in result
        assert "total_alerts" in result
        assert "critical" in result
        assert "high" in result
        assert "medium" in result
        assert "low" in result
        assert "alerts" in result
        assert "issues" in result
        assert isinstance(result["alerts"], list)

    @pytest.mark.asyncio
    async def test_invalid_url(self):
        result = await check_dependabot_alerts("not-a-url", FAKE_TOKEN, timeout=3.0)
        assert len(result["issues"]) > 0
        assert result["issues"][0]["type"] == "invalid_repo_url"

    @pytest.mark.asyncio
    async def test_fake_token_auth_failure(self):
        """가짜 토큰 → 인증 실패 기록"""
        result = await check_dependabot_alerts(
            "https://github.com/Testorum/testorum.git",
            FAKE_TOKEN,
            timeout=5.0,
        )
        # 401 또는 기타 에러 → issues에 기록
        assert len(result["issues"]) > 0

    @pytest.mark.asyncio
    async def test_parsed_owner_repo(self):
        result = await check_dependabot_alerts(
            "https://github.com/MyOrg/my-repo.git",
            FAKE_TOKEN,
            timeout=3.0,
        )
        assert result["owner"] == "MyOrg"
        assert result["repo"] == "my-repo"


# ===================================================================
# 3. run_dependency_audit 통합
# ===================================================================


class TestDependencyAuditResult:
    """run_dependency_audit 반환 구조"""

    @pytest.mark.asyncio
    async def test_result_structure(self):
        result = await run_dependency_audit(
            ["https://github.com/Testorum/testorum.git"],
            FAKE_TOKEN,
            timeout=3.0,
        )
        assert "repos" in result
        assert "repo_count" in result
        assert "total_alerts" in result
        assert "critical" in result
        assert "high" in result
        assert "total_issues" in result
        assert "elapsed_ms" in result
        assert result["repo_count"] == 1

    @pytest.mark.asyncio
    async def test_multiple_repos(self):
        result = await run_dependency_audit(
            [
                "https://github.com/Testorum/testorum.git",
                "https://github.com/LordOfWins/aria-engine.git",
            ],
            FAKE_TOKEN,
            timeout=3.0,
        )
        assert result["repo_count"] == 2
        assert len(result["repos"]) == 2


# ===================================================================
# 4. DependencyAuditTool ToolExecutor
# ===================================================================


class TestDependencyAuditTool:
    """DependencyAuditTool ToolExecutor"""

    def test_definition(self):
        tool = DependencyAuditTool()
        defn = tool.get_definition()
        assert defn.name == "dependency_audit"
        assert "취약점" in defn.description or "Dependabot" in defn.description
        assert len(defn.parameters) == 2

    @pytest.mark.asyncio
    async def test_empty_urls(self):
        tool = DependencyAuditTool()
        result = await tool.execute({"repo_urls": ""})
        assert result.success is False
        assert "비어있습니다" in result.error

    @pytest.mark.asyncio
    async def test_no_github_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        tool = DependencyAuditTool()
        result = await tool.execute({"repo_urls": "Testorum/testorum"})
        assert result.success is False
        assert "GITHUB_TOKEN" in result.error

    @pytest.mark.asyncio
    async def test_with_token(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)
        tool = DependencyAuditTool()
        result = await tool.execute({"repo_urls": "Testorum/testorum"})
        assert result.success is True
        assert result.output is not None

    @pytest.mark.asyncio
    async def test_comma_separated(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)
        tool = DependencyAuditTool()
        result = await tool.execute({
            "repo_urls": "Testorum/testorum, LordOfWins/aria-engine",
        })
        assert result.success is True
        assert result.output["repo_count"] == 2

    def test_llm_format(self):
        tool = DependencyAuditTool()
        llm_tool = tool.get_definition().to_llm_tool()
        assert llm_tool["function"]["name"] == "dependency_audit"


# ===================================================================
# 5. AlertType.DEPENDENCY_VULN
# ===================================================================


class TestDepAlertType:
    """의존성 알림 타입"""

    def test_enum_exists(self):
        assert AlertType.DEPENDENCY_VULN == "dependency_vuln"

    def test_emoji(self):
        assert ALERT_EMOJI[AlertType.DEPENDENCY_VULN] == "📦"

    def test_cooldown(self):
        assert DEFAULT_COOLDOWNS[AlertType.DEPENDENCY_VULN] == 86400

    def test_alert_manager_has_method(self):
        from aria.alerts.alert_manager import AlertManager
        assert hasattr(AlertManager, "check_dependency_vuln")


# ===================================================================
# 6. AlertManager.check_dependency_vuln 로직
# ===================================================================


class TestCheckDependencyVuln:

    @pytest.mark.asyncio
    async def test_no_alert_when_disabled(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(bot_token="", chat_id="", enabled=False)
        result = await mgr.check_dependency_vuln(
            repo_label="test/repo",
            total_alerts=10,
            critical=5,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_alert_when_no_critical_high(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(bot_token="test", chat_id="123", enabled=True)
        result = await mgr.check_dependency_vuln(
            repo_label="test/repo",
            total_alerts=5,
            critical=0,
            high=0,
            medium=5,
        )
        assert result is None


# ===================================================================
# 7. app.py 등록 확인
# ===================================================================


class TestAppDepRegistration:

    def test_dep_tool_importable(self):
        from aria.tools.mcp.dep_audit_tools import DependencyAuditTool
        tool = DependencyAuditTool()
        assert tool.get_definition().name == "dependency_audit"

    def test_dep_checks_importable(self):
        from aria.monitoring.dep_checks import (
            check_dependabot_alerts,
            run_dependency_audit,
        )
        assert callable(check_dependabot_alerts)
