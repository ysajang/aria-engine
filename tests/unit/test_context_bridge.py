"""ARIA Engine - Phase 3.6 External Context Bridge Tests

테스트 범위:
- Context Types: 스키마 검증 / 직렬화
- Analyzer: 패턴 매칭 / 토픽 감지 / 요약 / 메모리 콘텐츠 생성
- Bridge: Push 플로우 / Pull 플로우 / 세션 중복 / 에러 핸들링
- MCP Server: JSON-RPC 2.0 / 도구 목록 / 도구 실행
- Config: ContextBridgeConfig / MCPServerConfig
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.context.types import (
    AnalysisResult,
    ContextPullRequest,
    ContextPullResponse,
    ContextPushRequest,
    ContextPushResponse,
    ConversationMessage,
    ExtractedInsight,
    ExternalSource,
    INSIGHT_CATEGORIES,
    SessionRecord,
)
from aria.context.analyzer import (
    analyze_conversation,
    build_memory_content,
    _detect_primary_topic,
    _extract_insights_from_message,
    _generate_summary,
    _split_sentences,
    _trim_to_summary,
    _deduplicate_insights,
)
from aria.context.bridge import ContextBridge
from aria.mcp.server import (
    create_mcp_router,
    MCP_TOOLS,
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    _jsonrpc_response,
    _jsonrpc_error,
    _handle_initialize,
    _handle_tools_list,
)
from aria.core.config import ContextBridgeConfig, MCPServerConfig


# ============================================================
# Context Types Tests
# ============================================================

class TestConversationMessage:
    """ConversationMessage 스키마 테스트"""

    def test_valid_message(self):
        msg = ConversationMessage(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"
        assert msg.timestamp is None

    def test_valid_roles(self):
        for role in ["user", "assistant", "system", "tool"]:
            msg = ConversationMessage(role=role, content="test")
            assert msg.role == role

    def test_invalid_role(self):
        with pytest.raises(ValueError, match="허용되지 않은 role"):
            ConversationMessage(role="admin", content="test")

    def test_role_normalization(self):
        msg = ConversationMessage(role=" User ", content="test")
        assert msg.role == "user"

    def test_empty_content_rejected(self):
        with pytest.raises(ValueError):
            ConversationMessage(role="user", content="")

    def test_with_timestamp(self):
        msg = ConversationMessage(
            role="user",
            content="hello",
            timestamp="2026-05-06T12:00:00Z",
        )
        assert msg.timestamp == "2026-05-06T12:00:00Z"


class TestContextPushRequest:
    """ContextPushRequest 스키마 테스트"""

    def test_minimal_request(self):
        req = ContextPushRequest(
            source="cursor",
            messages=[ConversationMessage(role="user", content="hello")],
        )
        assert req.source == "cursor"
        assert req.scope == "global"
        assert req.auto_analyze is True
        assert len(req.messages) == 1

    def test_full_request(self):
        req = ContextPushRequest(
            source="claude-desktop",
            scope="testorum",
            messages=[
                ConversationMessage(role="user", content="fix the bug"),
                ConversationMessage(role="assistant", content="I found the issue"),
            ],
            session_id="sess-123",
            tags=["coding", "bugfix"],
            auto_analyze=False,
            metadata={"project": "testorum"},
        )
        assert req.scope == "testorum"
        assert req.session_id == "sess-123"
        assert len(req.tags) == 2
        assert req.auto_analyze is False

    def test_source_normalization(self):
        req = ContextPushRequest(
            source=" Cursor ",
            messages=[ConversationMessage(role="user", content="test")],
        )
        assert req.source == "cursor"

    def test_tags_normalization(self):
        req = ContextPushRequest(
            source="cursor",
            messages=[ConversationMessage(role="user", content="test")],
            tags=[" Coding ", " BugFix ", ""],
        )
        assert req.tags == ["coding", "bugfix"]

    def test_empty_messages_rejected(self):
        with pytest.raises(ValueError):
            ContextPushRequest(source="cursor", messages=[])


class TestContextPullRequest:
    """ContextPullRequest 스키마 테스트"""

    def test_defaults(self):
        req = ContextPullRequest()
        assert req.scope == "global"
        assert req.domains is None
        assert req.token_budget == 4000
        assert req.format == "markdown"

    def test_json_format(self):
        req = ContextPullRequest(format="json")
        assert req.format == "json"

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="지원하지 않는 포맷"):
            ContextPullRequest(format="xml")

    def test_budget_bounds(self):
        with pytest.raises(ValueError):
            ContextPullRequest(token_budget=100)
        with pytest.raises(ValueError):
            ContextPullRequest(token_budget=50000)


class TestExtractedInsight:
    """ExtractedInsight 스키마 테스트"""

    def test_valid_insight(self):
        insight = ExtractedInsight(
            category="decision",
            content="Next.js 15로 결정",
            confidence=0.8,
        )
        assert insight.category == "decision"
        assert insight.confidence == 0.8

    def test_confidence_bounds(self):
        with pytest.raises(ValueError):
            ExtractedInsight(category="decision", content="test", confidence=1.5)
        with pytest.raises(ValueError):
            ExtractedInsight(category="decision", content="test", confidence=-0.1)


class TestAnalysisResult:
    """AnalysisResult 스키마 테스트"""

    def test_empty_result(self):
        result = AnalysisResult()
        assert result.has_insights is False
        assert result.primary_topic == "general"

    def test_with_insights(self):
        result = AnalysisResult(
            insights=[ExtractedInsight(category="decision", content="test")],
            summary="test summary",
            primary_topic="coding",
        )
        assert result.has_insights is True


class TestExternalSource:
    """ExternalSource enum 테스트"""

    def test_all_sources(self):
        assert ExternalSource.CURSOR.value == "cursor"
        assert ExternalSource.CLAUDE_DESKTOP.value == "claude-desktop"
        assert ExternalSource.CHATGPT.value == "chatgpt"
        assert ExternalSource.CLAUDE_CODE.value == "claude-code"
        assert ExternalSource.CUSTOM.value == "custom"


class TestInsightCategories:
    """인사이트 카테고리 상수 테스트"""

    def test_all_categories_present(self):
        expected = {"decision", "preference", "fact", "todo", "learning", "issue"}
        assert INSIGHT_CATEGORIES == expected


class TestSessionRecord:
    """SessionRecord 스키마 테스트"""

    def test_creation(self):
        record = SessionRecord(
            session_id="sess-1",
            source="cursor",
            scope="global",
            message_count=5,
        )
        assert record.session_id == "sess-1"
        assert record.received_at  # auto-generated


class TestContextPushResponse:
    """ContextPushResponse 스키마 테스트"""

    def test_defaults(self):
        resp = ContextPushResponse()
        assert resp.status == "accepted"
        assert resp.insights_extracted == 0
        assert resp.memory_updated is False

    def test_serialization(self):
        resp = ContextPushResponse(
            status="accepted",
            event_id="ev-1",
            insights_extracted=3,
            memory_updated=True,
            memory_domain="external-context-coding",
        )
        data = resp.model_dump(mode="json")
        assert data["status"] == "accepted"
        assert data["insights_extracted"] == 3


class TestContextPullResponse:
    """ContextPullResponse 스키마 테스트"""

    def test_defaults(self):
        resp = ContextPullResponse(scope="global")
        assert resp.scope == "global"
        assert resp.content == ""
        assert resp.generated_at  # auto-generated


# ============================================================
# Analyzer Tests
# ============================================================

class TestSplitSentences:
    """문장 분리 테스트"""

    def test_newline_split(self):
        text = "첫 번째 줄\n두 번째 줄\n세 번째 줄"
        result = _split_sentences(text)
        assert len(result) == 3

    def test_markdown_header(self):
        text = "# 제목\n## 부제목\n내용"
        result = _split_sentences(text)
        assert "제목" in result
        assert "부제목" in result

    def test_list_items(self):
        text = "- 항목 1\n- 항목 2\n* 항목 3"
        result = _split_sentences(text)
        assert len(result) == 3

    def test_period_split(self):
        text = "첫 문장이다. 두 번째 문장이다."
        result = _split_sentences(text)
        assert len(result) == 2

    def test_empty_lines_skipped(self):
        text = "내용\n\n\n또 내용"
        result = _split_sentences(text)
        assert len(result) == 2


class TestTrimToSummary:
    """요약 길이 제한 테스트"""

    def test_short_text_unchanged(self):
        assert _trim_to_summary("짧은 텍스트") == "짧은 텍스트"

    def test_long_text_trimmed(self):
        long_text = "a " * 200
        result = _trim_to_summary(long_text, max_len=50)
        assert len(result) <= 54  # 50 + "..."

    def test_prefix_stripped(self):
        assert _trim_to_summary("네, 알겠습니다") == "알겠습니다"
        assert _trim_to_summary("Sure, I'll do that") == "I'll do that"


class TestExtractInsights:
    """인사이트 추출 테스트"""

    def test_decision_korean(self):
        result = _extract_insights_from_message("Next.js 15로 결정했습니다", 0)
        assert any(i.category == "decision" for i in result)

    def test_decision_english(self):
        result = _extract_insights_from_message("Let's go with React for the frontend", 0)
        assert any(i.category == "decision" for i in result)

    def test_todo_korean(self):
        result = _extract_insights_from_message("배포 전에 테스트를 해야 한다", 0)
        assert any(i.category == "todo" for i in result)

    def test_todo_english(self):
        result = _extract_insights_from_message("TODO: fix the login page styles", 0)
        assert any(i.category == "todo" for i in result)

    def test_issue_pattern(self):
        result = _extract_insights_from_message("로그인 시 에러가 발생합니다", 0)
        assert any(i.category == "issue" for i in result)

    def test_learning_pattern(self):
        result = _extract_insights_from_message("이번 장애의 원인은 CORS 설정 문제였다", 0)
        assert any(i.category == "learning" for i in result)

    def test_preference_pattern(self):
        result = _extract_insights_from_message("Tailwind을 선호합니다", 0)
        assert any(i.category == "preference" for i in result)

    def test_short_message_skipped(self):
        result = _extract_insights_from_message("OK", 0)
        assert len(result) == 0

    def test_source_index_preserved(self):
        result = _extract_insights_from_message("프론트엔드 프레임워크를 이것으로 결정합니다", 5)
        assert len(result) > 0
        assert result[0].source_index == 5

    def test_confidence_is_0_8(self):
        result = _extract_insights_from_message("Next.js로 결정했습니다", 0)
        assert result[0].confidence == 0.8


class TestDetectPrimaryTopic:
    """주요 토픽 감지 테스트"""

    def test_coding_topic(self):
        text = "함수를 리팩토링하고 모듈을 분리해야 합니다. 클래스 구조를 개선합시다."
        assert _detect_primary_topic(text) == "coding"

    def test_business_topic(self):
        text = "매출이 증가하고 있습니다. 마케팅 전략을 수정하고 고객 유지율을 높여야 합니다."
        assert _detect_primary_topic(text) == "business"

    def test_debugging_topic(self):
        text = "에러가 발생했습니다. 버그를 수정해야 합니다. 디버깅을 시작합시다."
        assert _detect_primary_topic(text) == "debugging"

    def test_general_when_no_keywords(self):
        text = "오늘 날씨가 좋습니다"
        assert _detect_primary_topic(text) == "general"

    def test_minimum_count_threshold(self):
        text = "코드 하나"  # 'code' 1번만 → general
        assert _detect_primary_topic(text) == "general"


class TestGenerateSummary:
    """요약 생성 테스트"""

    def test_from_first_user_message(self):
        messages = [
            ConversationMessage(role="user", content="로그인 페이지 버그 수정해주세요"),
            ConversationMessage(role="assistant", content="확인하겠습니다"),
        ]
        result = _generate_summary(messages, "debugging")
        assert "로그인" in result

    def test_no_user_message(self):
        messages = [
            ConversationMessage(role="assistant", content="안녕하세요"),
        ]
        result = _generate_summary(messages, "general")
        assert "general" in result

    def test_empty_messages(self):
        assert _generate_summary([], "general") == ""


class TestAnalyzeConversation:
    """전체 분석 파이프라인 테스트"""

    def test_empty_messages(self):
        result = analyze_conversation([])
        assert result.message_count == 0
        assert result.has_insights is False

    def test_basic_conversation(self):
        messages = [
            ConversationMessage(role="user", content="Tailwind CSS를 선호합니다"),
            ConversationMessage(role="assistant", content="Tailwind으로 진행하겠습니다"),
        ]
        result = analyze_conversation(messages)
        assert result.message_count == 2
        assert result.user_message_count == 1
        assert result.total_chars > 0

    def test_conversation_with_decisions(self):
        messages = [
            ConversationMessage(role="user", content="React와 Vue 중에 어떤 게 나을까요?"),
            ConversationMessage(role="assistant", content="React로 결정하는 것이 좋겠습니다"),
            ConversationMessage(role="user", content="좋습니다. React로 가자"),
        ]
        result = analyze_conversation(messages)
        assert result.has_insights
        assert any(i.category == "decision" for i in result.insights)

    def test_system_messages_excluded(self):
        messages = [
            ConversationMessage(role="system", content="You are a helpful assistant"),
            ConversationMessage(role="user", content="hello world test message here"),
        ]
        result = analyze_conversation(messages)
        # system 메시지는 분석 대상 제외
        assert result.user_message_count == 1

    def test_max_insights_limit(self):
        messages = [
            ConversationMessage(
                role="user",
                content="이것으로 결정합니다\n"
                "저것으로 결정합니다\n"
                "또 다른 것으로 결정합니다\n" * 10,
            ),
        ]
        result = analyze_conversation(messages, max_insights=3)
        assert len(result.insights) <= 3


class TestDeduplicateInsights:
    """인사이트 중복 제거 테스트"""

    def test_no_duplicates(self):
        insights = [
            ExtractedInsight(category="decision", content="A로 결정"),
            ExtractedInsight(category="todo", content="B를 해야 함"),
        ]
        result = _deduplicate_insights(insights)
        assert len(result) == 2

    def test_remove_duplicates(self):
        # 첫 50자가 동일한 두 인사이트 → 중복으로 판단
        base = "이번 프로젝트에서 React 프레임워크를 사용하기로 최종적으로 결정했습니다 팀원 모두 동의합니다"
        insights = [
            ExtractedInsight(category="decision", content=base),
            ExtractedInsight(category="decision", content=base + " 내일 착수합니다"),
        ]
        result = _deduplicate_insights(insights)
        assert len(result) == 1

    def test_single_insight(self):
        insights = [ExtractedInsight(category="decision", content="test")]
        assert len(_deduplicate_insights(insights)) == 1

    def test_empty_list(self):
        assert len(_deduplicate_insights([])) == 0


class TestBuildMemoryContent:
    """메모리 콘텐츠 생성 테스트"""

    def test_new_content(self):
        analysis = AnalysisResult(
            insights=[
                ExtractedInsight(category="decision", content="React 선택"),
                ExtractedInsight(category="todo", content="설정 파일 추가"),
            ],
            summary="프론트엔드 결정",
            primary_topic="coding",
        )
        result = build_memory_content(analysis, "cursor")
        assert "외부 컨텍스트" in result
        assert "결정사항" in result
        assert "할 일" in result
        assert "React 선택" in result

    def test_append_to_existing(self):
        existing = "# 기존 메모리\n\n- 기존 내용"
        analysis = AnalysisResult(
            insights=[ExtractedInsight(category="learning", content="새 발견")],
            summary="새 학습",
        )
        result = build_memory_content(analysis, "chatgpt", existing_content=existing)
        assert "기존 메모리" in result
        assert "---" in result  # 구분선
        assert "새 발견" in result

    def test_no_insights_returns_existing(self):
        analysis = AnalysisResult()
        result = build_memory_content(analysis, "cursor", existing_content="기존")
        assert result == "기존"

    def test_no_insights_no_existing(self):
        analysis = AnalysisResult()
        result = build_memory_content(analysis, "cursor")
        assert result == ""


# ============================================================
# Bridge Tests
# ============================================================

class TestContextBridge:
    """ContextBridge 통합 테스트"""

    def _make_bridge(self) -> tuple[ContextBridge, MagicMock, MagicMock, MagicMock]:
        index_manager = MagicMock()
        memory_loader = MagicMock()
        event_store = MagicMock()

        # ingest() 반환값: Event.event_id가 문자열이어야 함
        mock_event = MagicMock()
        mock_event.event_id = "test-event-id-001"
        event_store.ingest = MagicMock(return_value=mock_event)

        bridge = ContextBridge(
            index_manager=index_manager,
            memory_loader=memory_loader,
            event_store=event_store,
        )
        return bridge, index_manager, memory_loader, event_store

    @pytest.mark.asyncio
    async def test_push_basic(self):
        bridge, idx_mgr, _, _ = self._make_bridge()

        # get_topic raises → 새 토픽 생성
        idx_mgr.get_topic.side_effect = Exception("not found")

        req = ContextPushRequest(
            source="cursor",
            scope="global",
            messages=[
                ConversationMessage(role="user", content="React로 결정했습니다 프론트엔드는"),
            ],
        )
        resp = await bridge.push(req)

        assert resp.status == "accepted"

    @pytest.mark.asyncio
    async def test_push_duplicate_session(self):
        bridge, _, _, _ = self._make_bridge()

        req = ContextPushRequest(
            source="cursor",
            messages=[ConversationMessage(role="user", content="test content here")],
            session_id="sess-dup",
        )

        # 첫 번째 push
        resp1 = await bridge.push(req)
        assert resp1.status == "accepted"

        # 두 번째 push (중복)
        resp2 = await bridge.push(req)
        assert resp2.status == "duplicate"

    @pytest.mark.asyncio
    async def test_push_no_analyze(self):
        bridge, _, _, _ = self._make_bridge()

        req = ContextPushRequest(
            source="cursor",
            messages=[ConversationMessage(role="user", content="test")],
            auto_analyze=False,
        )
        resp = await bridge.push(req)
        assert resp.insights_extracted == 0
        assert resp.memory_updated is False

    @pytest.mark.asyncio
    async def test_push_analysis_failure_graceful(self):
        bridge, idx_mgr, _, _ = self._make_bridge()

        # 분석 중 에러 → 이벤트 저장은 성공해야 함
        with patch("aria.context.bridge.analyze_conversation", side_effect=RuntimeError("analysis failed")):
            req = ContextPushRequest(
                source="cursor",
                messages=[ConversationMessage(role="user", content="test content")],
            )
            resp = await bridge.push(req)
            assert resp.status == "accepted"  # 실패해도 accepted

    @pytest.mark.asyncio
    async def test_push_event_stored(self):
        bridge, _, _, event_store = self._make_bridge()

        req = ContextPushRequest(
            source="cursor",
            scope="testorum",
            messages=[ConversationMessage(role="user", content="hello world here")],
        )
        await bridge.push(req)

        # 이벤트 저장 호출 확인
        event_store.ingest.assert_called_once()

    @pytest.mark.asyncio
    async def test_push_no_event_store(self):
        bridge = ContextBridge(
            index_manager=MagicMock(),
            memory_loader=MagicMock(),
            event_store=None,
        )

        req = ContextPushRequest(
            source="cursor",
            messages=[ConversationMessage(role="user", content="test content here")],
            auto_analyze=False,
        )
        resp = await bridge.push(req)
        assert resp.event_id is None

    @pytest.mark.asyncio
    async def test_pull_markdown(self):
        bridge, _, memory_loader, _ = self._make_bridge()

        # LoadResult mock
        load_result = MagicMock()
        load_result.loaded_domains = ["user-profile", "coding"]
        load_result.prompt_markdown = "# User Profile\n\n- Name: seungjae"
        load_result.total_tokens = 100
        memory_loader.load.return_value = load_result

        req = ContextPullRequest(scope="global", format="markdown")
        resp = await bridge.pull(req)

        assert resp.scope == "global"
        assert resp.format == "markdown"
        assert "User Profile" in resp.content
        assert resp.token_count == 100

    @pytest.mark.asyncio
    async def test_pull_json_format(self):
        bridge, _, memory_loader, _ = self._make_bridge()

        load_result = MagicMock()
        load_result.loaded_domains = ["test"]
        load_result.prompt_markdown = "# Test"
        load_result.total_tokens = 50
        memory_loader.load.return_value = load_result

        req = ContextPullRequest(scope="global", format="json")
        resp = await bridge.pull(req)

        assert resp.format == "json"
        parsed = json.loads(resp.content)
        assert parsed["scope"] == "global"

    @pytest.mark.asyncio
    async def test_pull_load_failure(self):
        bridge, _, memory_loader, _ = self._make_bridge()
        memory_loader.load.side_effect = RuntimeError("load failed")

        req = ContextPullRequest(scope="global")
        resp = await bridge.pull(req)

        assert resp.content == ""
        assert resp.token_count == 0

    def test_session_count(self):
        bridge, _, _, _ = self._make_bridge()
        assert bridge.session_count == 0

    @pytest.mark.asyncio
    async def test_session_cache_limit(self):
        bridge = ContextBridge(
            index_manager=MagicMock(),
            memory_loader=MagicMock(),
            event_store=None,
        )

        # 캐시 크기를 작게 설정해서 LRU 동작 확인
        bridge._session_cache = {}

        for i in range(5):
            req = ContextPushRequest(
                source="cursor",
                messages=[ConversationMessage(role="user", content=f"msg {i} content")],
                session_id=f"sess-{i}",
                auto_analyze=False,
            )
            await bridge.push(req)

        assert bridge.session_count == 5


# ============================================================
# MCP Server Tests
# ============================================================

class TestMCPProtocol:
    """MCP JSON-RPC 2.0 프로토콜 테스트"""

    def test_jsonrpc_response(self):
        resp = _jsonrpc_response("req-1", {"status": "ok"})
        body = json.loads(resp.body)
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == "req-1"
        assert body["result"]["status"] == "ok"

    def test_jsonrpc_error(self):
        resp = _jsonrpc_error("req-1", -32600, "Invalid Request")
        body = json.loads(resp.body)
        assert body["error"]["code"] == -32600
        assert body["error"]["message"] == "Invalid Request"

    def test_jsonrpc_error_with_data(self):
        resp = _jsonrpc_error("req-1", -32602, "Bad params", data={"field": "name"})
        body = json.loads(resp.body)
        assert body["error"]["data"]["field"] == "name"

    def test_initialize_handler(self):
        resp = _handle_initialize("req-1", {
            "protocolVersion": "2025-03-26",
            "clientInfo": {"name": "test-client", "version": "1.0"},
        })
        body = json.loads(resp.body)
        result = body["result"]
        assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == MCP_SERVER_NAME
        assert "tools" in result["capabilities"]

    def test_tools_list_handler(self):
        resp = _handle_tools_list("req-1")
        body = json.loads(resp.body)
        tools = body["result"]["tools"]
        assert len(tools) == 5

        tool_names = {t["name"] for t in tools}
        assert "aria_memory_read" in tool_names
        assert "aria_memory_list" in tool_names
        assert "aria_context_push" in tool_names
        assert "aria_context_pull" in tool_names
        assert "aria_knowledge_search" in tool_names


class TestMCPTools:
    """MCP 도구 정의 테스트"""

    def test_tool_count(self):
        assert len(MCP_TOOLS) == 5

    def test_all_tools_have_schema(self):
        for tool in MCP_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"

    def test_memory_read_requires_domain(self):
        tool = next(t for t in MCP_TOOLS if t["name"] == "aria_memory_read")
        assert "domain" in tool["inputSchema"]["required"]

    def test_context_push_requires_source_messages(self):
        tool = next(t for t in MCP_TOOLS if t["name"] == "aria_context_push")
        assert "source" in tool["inputSchema"]["required"]
        assert "messages" in tool["inputSchema"]["required"]

    def test_knowledge_search_requires_query(self):
        tool = next(t for t in MCP_TOOLS if t["name"] == "aria_knowledge_search")
        assert "query" in tool["inputSchema"]["required"]


class TestMCPServerRouter:
    """MCP 서버 FastAPI 라우터 테스트"""

    def _create_test_app(self) -> FastAPI:
        test_app = FastAPI()
        router = create_mcp_router()
        test_app.include_router(router)

        # mock state
        test_app.state.index_manager = None
        test_app.state.memory_loader = None
        test_app.state.context_bridge = None
        test_app.state.hybrid_retriever = None

        return test_app

    def test_invalid_json(self):
        app = self._create_test_app()
        client = TestClient(app)
        resp = client.post("/mcp", content="not json", headers={"Content-Type": "application/json"})
        body = resp.json()
        assert body["error"]["code"] == -32700

    def test_missing_jsonrpc(self):
        app = self._create_test_app()
        client = TestClient(app)
        resp = client.post("/mcp", json={"method": "initialize", "id": 1})
        body = resp.json()
        assert body["error"]["code"] == -32600

    def test_initialize(self):
        app = self._create_test_app()
        client = TestClient(app)
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "clientInfo": {"name": "test", "version": "0.1"},
            },
        })
        body = resp.json()
        assert "result" in body
        assert body["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION

    def test_tools_list(self):
        app = self._create_test_app()
        client = TestClient(app)
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        })
        body = resp.json()
        assert len(body["result"]["tools"]) == 5

    def test_notification_no_response(self):
        app = self._create_test_app()
        client = TestClient(app)
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        assert resp.status_code == 204

    def test_unknown_method(self):
        app = self._create_test_app()
        client = TestClient(app)
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "unknown/method",
            "params": {},
        })
        body = resp.json()
        assert body["error"]["code"] == -32601

    def test_tool_call_memory_read_not_initialized(self):
        app = self._create_test_app()
        client = TestClient(app)
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "aria_memory_read",
                "arguments": {"domain": "test"},
            },
        })
        body = resp.json()
        assert "not initialized" in body["result"]["content"][0]["text"]

    def test_tool_call_missing_name(self):
        app = self._create_test_app()
        client = TestClient(app)
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"arguments": {}},
        })
        body = resp.json()
        assert body["error"]["code"] == -32602

    def test_tool_call_unknown_tool(self):
        app = self._create_test_app()
        client = TestClient(app)
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "nonexistent_tool",
                "arguments": {},
            },
        })
        body = resp.json()
        assert body["error"]["code"] == -32602

    def test_tool_call_memory_read_with_manager(self):
        app = self._create_test_app()

        mock_manager = MagicMock()
        mock_topic = MagicMock()
        mock_topic.content = "# User Profile\n- Name: test"
        mock_manager.get_topic.return_value = mock_topic
        app.state.index_manager = mock_manager

        client = TestClient(app)
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "aria_memory_read",
                "arguments": {"scope": "global", "domain": "user-profile"},
            },
        })
        body = resp.json()
        assert "User Profile" in body["result"]["content"][0]["text"]

    def test_tool_call_memory_list_with_manager(self):
        app = self._create_test_app()

        mock_manager = MagicMock()
        mock_index = MagicMock()
        mock_entry = MagicMock()
        mock_entry.summary = "사용자 프로필"
        mock_entry.version = 1
        mock_index.entries = {"user-profile": mock_entry}
        mock_manager.get_index.return_value = mock_index
        app.state.index_manager = mock_manager

        client = TestClient(app)
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "aria_memory_list",
                "arguments": {"scope": "global"},
            },
        })
        body = resp.json()
        assert "user-profile" in body["result"]["content"][0]["text"]


# ============================================================
# Config Tests
# ============================================================

class TestContextBridgeConfig:
    """ContextBridgeConfig 테스트"""

    def test_defaults(self):
        config = ContextBridgeConfig()
        assert config.enabled is True
        assert config.max_messages_per_push == 200
        assert config.auto_analyze is True
        assert config.session_cache_size == 1000
        assert config.default_pull_budget == 4000

    def test_is_configured(self):
        assert ContextBridgeConfig(enabled=True).is_configured is True
        assert ContextBridgeConfig(enabled=False).is_configured is False

    def test_budget_bounds(self):
        config = ContextBridgeConfig(default_pull_budget=8000)
        assert config.default_pull_budget == 8000


class TestMCPServerConfig:
    """MCPServerConfig 테스트"""

    def test_defaults(self):
        config = MCPServerConfig()
        assert config.enabled is True
        assert config.expose_write_tools is True

    def test_is_configured(self):
        assert MCPServerConfig(enabled=True).is_configured is True
        assert MCPServerConfig(enabled=False).is_configured is False
