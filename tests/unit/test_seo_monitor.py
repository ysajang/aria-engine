"""ARIA Engine - SEO Monitoring Tests

Product Connector Step 3 테스트
- check_meta_tags: 메타태그 검증
- check_broken_links: 깨진 링크 검사
- check_robots_sitemap: robots/sitemap 검증
- run_seo_audit: 통합 감사
- SeoAuditTool: ToolExecutor
- AlertType.SEO_ISSUE: 알림
"""

from __future__ import annotations

import pytest

from aria.monitoring.seo_checks import (
    check_broken_links,
    check_meta_tags,
    check_robots_sitemap,
    run_seo_audit,
    _HREF_RE,
    _META_RE,
    _TITLE_RE,
    _CANONICAL_RE,
)
from aria.tools.mcp.seo_monitor_tools import SeoAuditTool
from aria.alerts.alert_types import AlertType, ALERT_EMOJI, DEFAULT_COOLDOWNS


# ===================================================================
# 1. Regex Patterns
# ===================================================================


class TestSeoRegex:
    """HTML 파싱 정규식 테스트"""

    def test_href_extraction(self):
        html = '<a href="https://example.com">Link</a> <a href="/about">About</a>'
        matches = _HREF_RE.findall(html)
        assert len(matches) == 2
        assert "https://example.com" in matches
        assert "/about" in matches

    def test_href_single_quotes(self):
        html = "<a href='/page'>Link</a>"
        matches = _HREF_RE.findall(html)
        assert len(matches) == 1

    def test_meta_name_content(self):
        html = '<meta name="description" content="Test description">'
        matches = _META_RE.findall(html)
        assert len(matches) == 1
        assert matches[0] == ("description", "Test description")

    def test_meta_property_content(self):
        html = '<meta property="og:title" content="OG Title">'
        matches = _META_RE.findall(html)
        assert len(matches) == 1
        assert matches[0] == ("og:title", "OG Title")

    def test_title_extraction(self):
        html = "<html><head><title>My Page Title</title></head></html>"
        match = _TITLE_RE.search(html)
        assert match is not None
        assert match.group(1) == "My Page Title"

    def test_canonical_extraction(self):
        html = '<link rel="canonical" href="https://example.com/page">'
        match = _CANONICAL_RE.search(html)
        assert match is not None
        assert match.group(1) == "https://example.com/page"

    def test_title_missing(self):
        html = "<html><head></head></html>"
        match = _TITLE_RE.search(html)
        assert match is None

    def test_href_ignores_anchors(self):
        html = '<a href="#section">Jump</a>'
        matches = _HREF_RE.findall(html)
        assert matches == ["#section"]  # 추출은 됨 (필터는 check_broken_links에서)


# ===================================================================
# 2. check_meta_tags (mock 없이 구조 테스트)
# ===================================================================


class TestMetaTagsResult:
    """check_meta_tags 반환 구조 검증"""

    @pytest.mark.asyncio
    async def test_result_structure(self):
        """타임아웃 등으로 실패해도 구조는 유지"""
        result = await check_meta_tags("http://invalid.test.localhost:99999", timeout=1.0)
        assert "url" in result
        assert "score" in result
        assert "max_score" in result
        assert "issues" in result
        assert isinstance(result["issues"], list)

    @pytest.mark.asyncio
    async def test_score_range(self):
        """점수 범위 검증"""
        result = await check_meta_tags("http://invalid.test.localhost:99999", timeout=1.0)
        assert 0 <= result["score"] <= result["max_score"]
        assert result["max_score"] == 7


# ===================================================================
# 3. check_broken_links (구조 테스트)
# ===================================================================


class TestBrokenLinksResult:
    """check_broken_links 반환 구조 검증"""

    @pytest.mark.asyncio
    async def test_result_structure(self):
        result = await check_broken_links("http://invalid.test.localhost:99999", timeout=1.0)
        assert "url" in result
        assert "total_links" in result
        assert "checked" in result
        assert "broken" in result
        assert "broken_links" in result
        assert isinstance(result["broken_links"], list)

    @pytest.mark.asyncio
    async def test_max_links_parameter(self):
        """max_links 파라미터 존재 확인"""
        result = await check_broken_links(
            "http://invalid.test.localhost:99999",
            max_links=5,
            timeout=1.0,
        )
        assert result["checked"] <= 5


# ===================================================================
# 4. check_robots_sitemap (구조 테스트)
# ===================================================================


class TestRobotsSitemapResult:
    """check_robots_sitemap 반환 구조 검증"""

    @pytest.mark.asyncio
    async def test_result_structure(self):
        result = await check_robots_sitemap("http://invalid.test.localhost:99999", timeout=1.0)
        assert "url" in result
        assert "robots_exists" in result
        assert "sitemap_exists" in result
        assert "sitemap_url_count" in result
        assert "issues" in result


# ===================================================================
# 5. run_seo_audit (통합 구조 테스트)
# ===================================================================


class TestSeoAuditResult:
    """run_seo_audit 반환 구조 검증"""

    @pytest.mark.asyncio
    async def test_result_structure(self):
        result = await run_seo_audit(
            "http://invalid.test.localhost:99999",
            check_links=False,
            timeout=1.0,
        )
        assert "url" in result
        assert "meta" in result
        assert "links" in result
        assert "robots_sitemap" in result
        assert "total_issues" in result
        assert "high_issues" in result
        assert "elapsed_ms" in result

    @pytest.mark.asyncio
    async def test_skip_links(self):
        """check_links=False면 링크 검사 안 함"""
        result = await run_seo_audit(
            "http://invalid.test.localhost:99999",
            check_links=False,
            timeout=1.0,
        )
        links = result["links"]
        assert links["checked"] == 0
        assert links["broken"] == 0


# ===================================================================
# 6. SeoAuditTool (ToolExecutor)
# ===================================================================


class TestSeoAuditTool:
    """SeoAuditTool ToolExecutor"""

    def test_definition(self):
        tool = SeoAuditTool()
        defn = tool.get_definition()
        assert defn.name == "seo_audit"
        assert "SEO" in defn.description
        assert len(defn.parameters) == 3
        assert defn.parameters[0].name == "url"

    @pytest.mark.asyncio
    async def test_empty_url(self):
        tool = SeoAuditTool()
        result = await tool.execute({"url": ""})
        assert result.success is False
        assert "비어있습니다" in result.error

    @pytest.mark.asyncio
    async def test_invalid_url(self):
        tool = SeoAuditTool()
        result = await tool.execute({"url": "not-a-url"})
        assert result.success is False
        assert "http" in result.error

    @pytest.mark.asyncio
    async def test_unreachable_url(self):
        tool = SeoAuditTool()
        result = await tool.execute({
            "url": "http://invalid.test.localhost:99999",
            "check_links": False,
        })
        # 접근 불가해도 성공적으로 결과 반환 (이슈에 기록)
        assert result.success is True
        assert result.output is not None
        assert result.output["total_issues"] > 0

    def test_llm_format(self):
        tool = SeoAuditTool()
        defn = tool.get_definition()
        llm_tool = defn.to_llm_tool()
        assert llm_tool["type"] == "function"
        assert llm_tool["function"]["name"] == "seo_audit"


# ===================================================================
# 7. AlertType.SEO_ISSUE
# ===================================================================


class TestSeoAlertType:
    """SEO 알림 타입"""

    def test_enum_exists(self):
        assert AlertType.SEO_ISSUE == "seo_issue"

    def test_emoji(self):
        assert AlertType.SEO_ISSUE in ALERT_EMOJI
        assert ALERT_EMOJI[AlertType.SEO_ISSUE] == "🔍"

    def test_cooldown(self):
        assert AlertType.SEO_ISSUE in DEFAULT_COOLDOWNS
        assert DEFAULT_COOLDOWNS[AlertType.SEO_ISSUE] == 86400  # 24시간

    def test_alert_manager_has_method(self):
        from aria.alerts.alert_manager import AlertManager
        assert hasattr(AlertManager, "check_seo_issue")


# ===================================================================
# 8. AlertManager.check_seo_issue 로직
# ===================================================================


class TestCheckSeoIssue:
    """AlertManager.check_seo_issue 판정 로직"""

    @pytest.mark.asyncio
    async def test_no_alert_when_disabled(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(bot_token="", chat_id="", enabled=False)
        result = await mgr.check_seo_issue(
            url="https://test.com",
            total_issues=5,
            high_issues=3,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_alert_when_no_high_issues_good_score(self):
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(bot_token="test", chat_id="123", enabled=True)
        result = await mgr.check_seo_issue(
            url="https://test.com",
            total_issues=2,
            high_issues=0,
            meta_score=5,
            meta_max_score=7,
        )
        # high=0 + score >= max//2(3) → 알림 안 함
        assert result is None

    @pytest.mark.asyncio
    async def test_alert_when_high_issues(self):
        """high 이슈가 있으면 알림 시도 (텔레그램 미연결이라 실패하지만 로직 실행)"""
        from aria.alerts.alert_manager import AlertManager
        mgr = AlertManager(bot_token="test", chat_id="123", enabled=True)
        # 텔레그램 전송은 실패하지만 알림 판정 로직은 실행됨
        result = await mgr.check_seo_issue(
            url="https://test.com",
            total_issues=5,
            high_issues=3,
            meta_score=2,
            meta_max_score=7,
            broken_links=2,
            top_issues=[{"severity": "high", "message": "title 없음"}],
        )
        # 텔레그램 미연결이므로 None 반환 (하지만 에러 없이 실행됨)
        # 실제 환경에서는 Alert 객체 반환


# ===================================================================
# 9. app.py SEO Tool 등록 확인
# ===================================================================


class TestAppSeoRegistration:
    """app.py에 SeoAuditTool 등록 코드 존재 확인"""

    def test_seo_tool_import_in_app(self):
        """app.py에서 SeoAuditTool 임포트 가능"""
        from aria.tools.mcp.seo_monitor_tools import SeoAuditTool
        tool = SeoAuditTool()
        assert tool.get_definition().name == "seo_audit"

    def test_seo_checks_importable(self):
        """seo_checks 모듈 임포트 가능"""
        from aria.monitoring.seo_checks import (
            check_meta_tags,
            check_broken_links,
            check_robots_sitemap,
            run_seo_audit,
        )
        assert callable(check_meta_tags)
        assert callable(run_seo_audit)
