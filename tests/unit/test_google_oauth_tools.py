"""ARIA Engine - Google OAuth2 + Gmail + Calendar Tests

- GoogleOAuthConfig: 환경변수 + is_configured
- GoogleTokenManager: 토큰 갱신 + 캐시 + 무효화
- GmailClient: 검색 + 읽기 + 발송 + 초안
- GCalClient: 조회 + 생성 + 수정
- Tool 정의 + 실행 + 에러 처리
- 유틸리티 함수
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# GoogleOAuthConfig
# ============================================================


class TestGoogleOAuthConfig:

    def test_default_not_configured(self):
        from aria.core.config import GoogleOAuthConfig
        config = GoogleOAuthConfig(client_id="", client_secret="", refresh_token="")
        assert config.is_configured is False

    def test_configured(self):
        from aria.core.config import GoogleOAuthConfig
        config = GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok")
        assert config.is_configured is True

    def test_partial_not_configured(self):
        from aria.core.config import GoogleOAuthConfig
        config = GoogleOAuthConfig(client_id="id", client_secret="", refresh_token="tok")
        assert config.is_configured is False

    def test_in_aria_config(self):
        from aria.core.config import AriaConfig, GoogleOAuthConfig
        config = AriaConfig()
        assert hasattr(config, "google_oauth")
        assert isinstance(config.google_oauth, GoogleOAuthConfig)


# ============================================================
# GoogleTokenManager
# ============================================================


class TestGoogleTokenManager:

    def test_init_no_token(self):
        from aria.core.config import GoogleOAuthConfig
        from aria.auth.google_oauth import GoogleTokenManager
        config = GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok")
        mgr = GoogleTokenManager(config)
        assert mgr.has_valid_token is False

    @pytest.mark.asyncio
    async def test_refresh_success(self):
        from aria.core.config import GoogleOAuthConfig
        from aria.auth.google_oauth import GoogleTokenManager

        config = GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok")
        mgr = GoogleTokenManager(config)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "new-token", "expires_in": 3600}

        with patch("aria.auth.google_oauth.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_resp)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            token = await mgr.get_access_token()

        assert token == "new-token"
        assert mgr.has_valid_token is True

    @pytest.mark.asyncio
    async def test_cached_token_reuse(self):
        from aria.core.config import GoogleOAuthConfig
        from aria.auth.google_oauth import GoogleTokenManager
        import time

        config = GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok")
        mgr = GoogleTokenManager(config)
        mgr._access_token = "cached"
        mgr._expires_at = time.time() + 3600  # 1시간 뒤 만료

        token = await mgr.get_access_token()
        assert token == "cached"

    @pytest.mark.asyncio
    async def test_no_refresh_token_error(self):
        from aria.core.config import GoogleOAuthConfig
        from aria.auth.google_oauth import GoogleTokenManager, GoogleAuthError

        config = GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="")
        mgr = GoogleTokenManager(config)

        with pytest.raises(GoogleAuthError, match="refresh_token"):
            await mgr.get_access_token()

    @pytest.mark.asyncio
    async def test_invalid_grant_error(self):
        from aria.core.config import GoogleOAuthConfig
        from aria.auth.google_oauth import GoogleTokenManager, GoogleAuthError

        config = GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="expired")
        mgr = GoogleTokenManager(config)

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"error": "invalid_grant", "error_description": "Token expired"}

        with patch("aria.auth.google_oauth.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_resp)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            with pytest.raises(GoogleAuthError, match="만료"):
                await mgr.get_access_token()

    def test_invalidate(self):
        from aria.core.config import GoogleOAuthConfig
        from aria.auth.google_oauth import GoogleTokenManager
        import time

        config = GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok")
        mgr = GoogleTokenManager(config)
        mgr._access_token = "valid"
        mgr._expires_at = time.time() + 3600

        mgr.invalidate()
        assert mgr.has_valid_token is False
        assert mgr._access_token == ""


# ============================================================
# Gmail Utility Tests
# ============================================================


class TestGmailUtils:

    def test_extract_headers(self):
        from aria.tools.mcp.gmail_tools import _extract_headers
        headers = [
            {"name": "From", "value": "test@example.com"},
            {"name": "Subject", "value": "Hello"},
        ]
        result = _extract_headers(headers)
        assert result["From"] == "test@example.com"
        assert result["Subject"] == "Hello"

    def test_extract_body_plain(self):
        from aria.tools.mcp.gmail_tools import _extract_body
        data = base64.urlsafe_b64encode(b"Hello World").decode()
        payload = {"mimeType": "text/plain", "body": {"data": data}}
        assert _extract_body(payload) == "Hello World"

    def test_extract_body_multipart_plain(self):
        from aria.tools.mcp.gmail_tools import _extract_body
        data = base64.urlsafe_b64encode(b"Body text").decode()
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": data}},
                {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>HTML</p>").decode()}},
            ],
        }
        assert _extract_body(payload) == "Body text"

    def test_extract_body_empty(self):
        from aria.tools.mcp.gmail_tools import _extract_body
        assert "(본문 없음)" in _extract_body({})

    def test_create_raw_message(self):
        from aria.tools.mcp.gmail_tools import _create_raw_message
        raw = _create_raw_message(to="a@b.com", subject="Test", body="Hello")
        # base64url 디코딩 가능한지 확인
        decoded = base64.urlsafe_b64decode(raw)
        assert b"a@b.com" in decoded
        assert b"Test" in decoded


# ============================================================
# Gmail Tool Tests
# ============================================================


class TestGmailSearchTool:

    def test_definition(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gmail_tools import GmailClient, GmailSearchTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GmailSearchTool(GmailClient(mgr))
        defn = tool.get_definition()
        assert defn.name == "gmail_search"
        assert defn.safety_hint.value == "read_only"

    @pytest.mark.asyncio
    async def test_empty_query(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gmail_tools import GmailClient, GmailSearchTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GmailSearchTool(GmailClient(mgr))
        result = await tool.execute({"query": ""})
        assert result.success is False


class TestGmailReadTool:

    def test_definition(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gmail_tools import GmailClient, GmailReadTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GmailReadTool(GmailClient(mgr))
        assert tool.get_definition().name == "gmail_read"

    @pytest.mark.asyncio
    async def test_empty_id(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gmail_tools import GmailClient, GmailReadTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GmailReadTool(GmailClient(mgr))
        result = await tool.execute({"message_id": ""})
        assert result.success is False


class TestGmailSendTool:

    def test_definition_external(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gmail_tools import GmailClient, GmailSendTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GmailSendTool(GmailClient(mgr))
        defn = tool.get_definition()
        assert defn.name == "gmail_send"
        assert defn.safety_hint.value == "external"

    @pytest.mark.asyncio
    async def test_missing_to(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gmail_tools import GmailClient, GmailSendTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GmailSendTool(GmailClient(mgr))
        result = await tool.execute({"to": "", "subject": "x", "body": "y"})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_missing_subject(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gmail_tools import GmailClient, GmailSendTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GmailSendTool(GmailClient(mgr))
        result = await tool.execute({"to": "a@b.com", "subject": "", "body": "y"})
        assert result.success is False


class TestGmailDraftTool:

    def test_definition(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gmail_tools import GmailClient, GmailDraftTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GmailDraftTool(GmailClient(mgr))
        assert tool.get_definition().name == "gmail_draft"
        assert tool.get_definition().safety_hint.value == "write"


# ============================================================
# Calendar Utility Tests
# ============================================================


class TestCalendarUtils:

    def test_simplify_event(self):
        from aria.tools.mcp.gcal_tools import _simplify_event
        raw = {
            "id": "evt123",
            "summary": "회의",
            "description": "팀 미팅",
            "location": "서울",
            "start": {"dateTime": "2026-05-04T10:00:00+09:00"},
            "end": {"dateTime": "2026-05-04T11:00:00+09:00"},
            "status": "confirmed",
            "htmlLink": "https://calendar.google.com/event?eid=123",
            "creator": {"email": "user@example.com"},
        }
        result = _simplify_event(raw)
        assert result["id"] == "evt123"
        assert result["summary"] == "회의"
        assert result["all_day"] is False

    def test_simplify_all_day_event(self):
        from aria.tools.mcp.gcal_tools import _simplify_event
        raw = {
            "id": "evt456",
            "summary": "휴가",
            "start": {"date": "2026-05-04"},
            "end": {"date": "2026-05-05"},
        }
        result = _simplify_event(raw)
        assert result["all_day"] is True
        assert result["start"] == "2026-05-04"

    def test_simplify_event_minimal(self):
        from aria.tools.mcp.gcal_tools import _simplify_event
        result = _simplify_event({})
        assert result["summary"] == "(제목 없음)"


# ============================================================
# Calendar Tool Tests
# ============================================================


class TestGCalListEventsTool:

    def test_definition(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gcal_tools import GCalClient, GCalListEventsTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GCalListEventsTool(GCalClient(mgr))
        assert tool.get_definition().name == "gcal_list_events"
        assert tool.get_definition().safety_hint.value == "read_only"


class TestGCalCreateEventTool:

    def test_definition(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gcal_tools import GCalClient, GCalCreateEventTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GCalCreateEventTool(GCalClient(mgr))
        defn = tool.get_definition()
        assert defn.name == "gcal_create_event"
        assert defn.safety_hint.value == "write"

    @pytest.mark.asyncio
    async def test_missing_summary(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gcal_tools import GCalClient, GCalCreateEventTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GCalCreateEventTool(GCalClient(mgr))
        result = await tool.execute({"summary": "", "start": "x", "end": "y"})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_missing_times(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gcal_tools import GCalClient, GCalCreateEventTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GCalCreateEventTool(GCalClient(mgr))
        result = await tool.execute({"summary": "test", "start": "", "end": ""})
        assert result.success is False


class TestGCalUpdateEventTool:

    def test_definition(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gcal_tools import GCalClient, GCalUpdateEventTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GCalUpdateEventTool(GCalClient(mgr))
        assert tool.get_definition().name == "gcal_update_event"

    @pytest.mark.asyncio
    async def test_empty_event_id(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        from aria.tools.mcp.gcal_tools import GCalClient, GCalUpdateEventTool
        mgr = GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))
        tool = GCalUpdateEventTool(GCalClient(mgr))
        result = await tool.execute({"event_id": ""})
        assert result.success is False


# ============================================================
# LLM Format Tests
# ============================================================


class TestGoogleServiceLLMFormat:

    def _make_mgr(self):
        from aria.auth.google_oauth import GoogleTokenManager
        from aria.core.config import GoogleOAuthConfig
        return GoogleTokenManager(GoogleOAuthConfig(client_id="id", client_secret="sec", refresh_token="tok"))

    def test_gmail_search(self):
        from aria.tools.mcp.gmail_tools import GmailClient, GmailSearchTool
        llm = GmailSearchTool(GmailClient(self._make_mgr())).get_definition().to_llm_tool()
        assert llm["function"]["name"] == "gmail_search"

    def test_gmail_read(self):
        from aria.tools.mcp.gmail_tools import GmailClient, GmailReadTool
        llm = GmailReadTool(GmailClient(self._make_mgr())).get_definition().to_llm_tool()
        assert llm["function"]["name"] == "gmail_read"

    def test_gmail_send(self):
        from aria.tools.mcp.gmail_tools import GmailClient, GmailSendTool
        llm = GmailSendTool(GmailClient(self._make_mgr())).get_definition().to_llm_tool()
        assert llm["function"]["name"] == "gmail_send"

    def test_gmail_draft(self):
        from aria.tools.mcp.gmail_tools import GmailClient, GmailDraftTool
        llm = GmailDraftTool(GmailClient(self._make_mgr())).get_definition().to_llm_tool()
        assert llm["function"]["name"] == "gmail_draft"

    def test_gcal_list(self):
        from aria.tools.mcp.gcal_tools import GCalClient, GCalListEventsTool
        llm = GCalListEventsTool(GCalClient(self._make_mgr())).get_definition().to_llm_tool()
        assert llm["function"]["name"] == "gcal_list_events"

    def test_gcal_create(self):
        from aria.tools.mcp.gcal_tools import GCalClient, GCalCreateEventTool
        llm = GCalCreateEventTool(GCalClient(self._make_mgr())).get_definition().to_llm_tool()
        assert llm["function"]["name"] == "gcal_create_event"

    def test_gcal_update(self):
        from aria.tools.mcp.gcal_tools import GCalClient, GCalUpdateEventTool
        llm = GCalUpdateEventTool(GCalClient(self._make_mgr())).get_definition().to_llm_tool()
        assert llm["function"]["name"] == "gcal_update_event"
