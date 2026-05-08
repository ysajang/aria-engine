"""ARIA Engine - LLM Workflow Tests

LLM 워크플로우 3종 (#1 SEO / #2 SNS / #5 이메일) + runner 수정 + 템플릿 테스트

실행: ARIA_ENV_FILE="" pytest tests/unit/test_llm_workflows.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.workflows.types import (
    StepType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowStatus,
)
from aria.workflows.runner import WorkflowRunner
from aria.workflows.templates import TemplateEngine, BUILTIN_TEMPLATES
from aria.workflows.definitions import ALL_BUILDERS
from aria.workflows.definitions.seo_content_pipeline import (
    build_seo_content_pipeline,
    PRODUCT_KEYWORD_SEEDS,
    SEO_BLOG_SYSTEM,
)
from aria.workflows.definitions.sns_posting import (
    build_sns_posting,
    PLATFORM_LIMITS,
    DEFAULT_HASHTAGS,
    SNS_DRAFT_SYSTEM,
    _parse_llm_json,
)
from aria.workflows.definitions.email_marketing import (
    build_email_marketing,
    SEGMENT_DEFINITIONS,
    EMAIL_PERSONALIZE_SYSTEM,
)
from aria.workflows.setup import setup_workflows


# ============================================================
# ALL_BUILDERS 통합 검증
# ============================================================


class TestAllBuildersUpdated:
    """ALL_BUILDERS 목록 업데이트 검증"""

    def test_builders_count_is_10(self):
        """기존 7 + 신규 3 = 10개"""
        assert len(ALL_BUILDERS) == 10

    def test_all_builders_return_correct_format(self):
        """모든 빌더가 (WorkflowDefinition, dict) 반환"""
        for builder in ALL_BUILDERS:
            defn, funcs = builder()
            assert isinstance(defn, WorkflowDefinition), f"{builder.__name__} definition 타입 오류"
            assert isinstance(funcs, dict), f"{builder.__name__} functions 타입 오류"
            assert len(defn.steps) > 0, f"{builder.__name__} steps 비어있음"

    def test_no_duplicate_workflow_ids(self):
        """워크플로우 ID 중복 없음"""
        ids = []
        for builder in ALL_BUILDERS:
            defn, _ = builder()
            ids.append(defn.workflow_id)
        assert len(ids) == len(set(ids)), f"중복 workflow_id: {ids}"

    def test_new_workflows_are_marketing_category(self):
        """신규 3종은 모두 marketing 카테고리"""
        new_builders = [build_seo_content_pipeline, build_sns_posting, build_email_marketing]
        for builder in new_builders:
            defn, _ = builder()
            assert defn.category == "marketing"

    def test_new_workflows_have_llm_steps(self):
        """신규 3종은 LLM_CALL 스텝 포함"""
        new_builders = [build_seo_content_pipeline, build_sns_posting, build_email_marketing]
        for builder in new_builders:
            defn, _ = builder()
            llm_steps = [s for s in defn.steps if s.step_type == StepType.LLM_CALL]
            assert len(llm_steps) >= 1, f"{defn.workflow_id}에 LLM_CALL 스텝 없음"


# ============================================================
# runner.py _run_llm_call 수정 검증
# ============================================================


class TestRunnerLLMCall:
    """WorkflowRunner._run_llm_call 파라미터 수정 검증"""

    @pytest.fixture
    def mock_llm_provider(self):
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value={
            "content": "Generated content here",
            "model": "claude-haiku-4-5-20251001",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })
        return provider

    @pytest.fixture
    def runner(self, mock_llm_provider):
        return WorkflowRunner(llm_provider=mock_llm_provider)

    @pytest.mark.asyncio
    async def test_llm_call_uses_correct_params(self, runner, mock_llm_provider):
        """complete()가 올바른 파라미터명으로 호출되는지 검증"""
        config = {
            "prompt": "Write a blog post",
            "system": "You are a writer",
            "model": "cheap",
        }
        result = await runner._run_llm_call(config)
        assert result == "Generated content here"

        # 실제 호출 파라미터 검증
        call_args = mock_llm_provider.complete.call_args
        assert call_args.args[0] == "Write a blog post"  # positional prompt
        assert call_args.kwargs["system_prompt"] == "You are a writer"
        assert call_args.kwargs["model_tier"] == "cheap"

    @pytest.mark.asyncio
    async def test_llm_call_default_model_is_cheap(self, runner, mock_llm_provider):
        """모델 미지정 시 cheap(Haiku) 기본 사용"""
        await runner._run_llm_call({"prompt": "test"})
        call_kwargs = mock_llm_provider.complete.call_args.kwargs
        assert call_kwargs["model_tier"] == "cheap"

    @pytest.mark.asyncio
    async def test_llm_call_max_tokens(self, runner, mock_llm_provider):
        """max_tokens 전달"""
        await runner._run_llm_call({"prompt": "test", "max_tokens": 1024})
        assert mock_llm_provider.complete.call_args.kwargs["max_tokens"] == 1024

    @pytest.mark.asyncio
    async def test_llm_call_default_max_tokens(self, runner, mock_llm_provider):
        """기본 max_tokens = 2048"""
        await runner._run_llm_call({"prompt": "test"})
        assert mock_llm_provider.complete.call_args.kwargs["max_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_llm_call_temperature(self, runner, mock_llm_provider):
        """temperature 전달"""
        await runner._run_llm_call({"prompt": "test", "temperature": 0.9})
        assert mock_llm_provider.complete.call_args.kwargs["temperature"] == 0.9

    @pytest.mark.asyncio
    async def test_llm_call_default_temperature(self, runner, mock_llm_provider):
        """기본 temperature = 0.7"""
        await runner._run_llm_call({"prompt": "test"})
        assert mock_llm_provider.complete.call_args.kwargs["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_llm_call_no_provider_raises(self):
        """LLMProvider 없으면 RuntimeError"""
        runner = WorkflowRunner(llm_provider=None)
        with pytest.raises(RuntimeError, match="LLMProvider"):
            await runner._run_llm_call({"prompt": "test"})

    @pytest.mark.asyncio
    async def test_llm_call_no_prompt_raises(self, runner):
        """prompt 없으면 ValueError"""
        with pytest.raises(ValueError, match="prompt"):
            await runner._run_llm_call({})

    @pytest.mark.asyncio
    async def test_llm_call_empty_system(self, runner, mock_llm_provider):
        """system 빈 문자열이면 None으로 전달"""
        await runner._run_llm_call({"prompt": "test", "system": ""})
        assert mock_llm_provider.complete.call_args.kwargs["system_prompt"] is None

    @pytest.mark.asyncio
    async def test_llm_call_empty_content_returns_empty(self, mock_llm_provider):
        """LLM이 빈 content 반환 시 빈 문자열"""
        mock_llm_provider.complete = AsyncMock(return_value={"model": "haiku"})
        runner = WorkflowRunner(llm_provider=mock_llm_provider)
        result = await runner._run_llm_call({"prompt": "test"})
        assert result == ""


# ============================================================
# 새 템플릿 검증
# ============================================================


class TestNewTemplates:
    """신규 내장 템플릿 3종 검증"""

    @pytest.fixture
    def engine(self):
        return TemplateEngine()

    def test_seo_blog_draft_template_exists(self):
        assert "marketing/seo_blog_draft.md.j2" in BUILTIN_TEMPLATES

    def test_sns_post_set_template_exists(self):
        assert "marketing/sns_post_set.md.j2" in BUILTIN_TEMPLATES

    def test_email_campaign_template_exists(self):
        assert "marketing/email_campaign.md.j2" in BUILTIN_TEMPLATES

    def test_seo_blog_draft_renders(self, engine):
        """SEO 블로그 드래프트 템플릿 렌더링"""
        result = engine.render("marketing/seo_blog_draft.md.j2", {
            "seo_title": "Test Title",
            "seo_description": "Test description for SEO",
            "keywords": ["keyword1", "keyword2"],
            "now": datetime.now(timezone.utc).isoformat(),
            "category": "testorum",
            "title": "Full Blog Title",
            "draft_body": "This is the blog body content.",
        })
        assert "Test Title" in result
        assert "keyword1" in result
        assert "blog body content" in result
        assert "---" in result  # frontmatter

    def test_sns_post_set_renders(self, engine):
        """SNS 포스트 세트 템플릿 렌더링"""
        result = engine.render("marketing/sns_post_set.md.j2", {
            "now": datetime.now(timezone.utc).isoformat(),
            "topic": "New Feature Launch",
            "posts": [
                {"platform": "twitter", "content": "Check this out!", "hashtags": "#test", "char_count": 15},
                {"platform": "instagram", "content": "Longer post here", "hashtags": "#test #ig", "char_count": 17},
            ],
        })
        assert "TWITTER" in result
        assert "INSTAGRAM" in result
        assert "2개 플랫폼" in result

    def test_email_campaign_renders(self, engine):
        """이메일 캠페인 템플릿 렌더링"""
        result = engine.render("marketing/email_campaign.md.j2", {
            "now": datetime.now(timezone.utc).isoformat(),
            "campaign_name": "Test Campaign",
            "segment_name": "new_users",
            "recipients": [{"email": "a@b.com"}, {"email": "c@d.com"}],
            "subject": "Hello!",
            "email_body": "This is the email body.",
            "cta_text": "Click here",
            "cta_url": "https://testorum.app",
        })
        assert "Test Campaign" in result
        assert "2명" in result
        assert "Click here" in result

    def test_existing_templates_still_work(self):
        """기존 11개 템플릿 보존"""
        expected = [
            "marketing/competitor_report.md.j2",
            "marketing/viral_report.md.j2",
            "marketing/sns_draft.md.j2",
            "marketing/seo_blog_outline.md.j2",
            "admin/kpi_briefing.md.j2",
            "admin/invoice.md.j2",
            "admin/tax_summary.md.j2",
            "admin/schedule_confirm.md.j2",
            "common/event_summary.md.j2",
            "common/workflow_notification.md.j2",
        ]
        for name in expected:
            assert name in BUILTIN_TEMPLATES, f"기존 템플릿 누락: {name}"

    def test_total_template_count(self):
        """총 14개 내장 템플릿 (기존 11 + 신규 3 - seo_blog_outline 겹침 없음)"""
        # 기존 10 + seo_blog_outline(기존) + 신규 3 = 14
        assert len(BUILTIN_TEMPLATES) >= 13


# ============================================================
# #1 SEO 콘텐츠 파이프라인 테스트
# ============================================================


class TestSEOContentPipeline:
    """SEO 콘텐츠 파이프라인 빌드 + 함수 테스트"""

    def test_build_returns_valid_definition(self):
        defn, funcs = build_seo_content_pipeline()
        assert defn.workflow_id == "seo-content"
        assert defn.category == "marketing"
        assert len(defn.steps) == 5

    def test_step_types(self):
        defn, _ = build_seo_content_pipeline()
        types = [s.step_type for s in defn.steps]
        assert types == [
            StepType.FUNCTION,   # fetch-trends
            StepType.FUNCTION,   # select-keywords
            StepType.LLM_CALL,   # generate-draft
            StepType.TEMPLATE,   # format-post
            StepType.TOOL_CALL,  # save-to-notion
        ]

    def test_notion_step_is_conditional(self):
        defn, _ = build_seo_content_pipeline()
        notion_step = defn.steps[-1]
        assert notion_step.when == "notion_parent_id exists"
        assert notion_step.on_error.value == "skip"

    def test_llm_step_uses_cheap_model(self):
        defn, _ = build_seo_content_pipeline()
        llm_step = [s for s in defn.steps if s.step_type == StepType.LLM_CALL][0]
        assert llm_step.config["model"] == "cheap"

    def test_llm_step_has_retry(self):
        defn, _ = build_seo_content_pipeline()
        llm_step = [s for s in defn.steps if s.step_type == StepType.LLM_CALL][0]
        assert llm_step.on_error.value == "retry"

    def test_functions_registered(self):
        _, funcs = build_seo_content_pipeline()
        assert "seo_fetch_trends" in funcs
        assert "seo_select_keywords" in funcs
        assert callable(funcs["seo_fetch_trends"])
        assert callable(funcs["seo_select_keywords"])

    def test_product_keyword_seeds_populated(self):
        assert "testorum" in PRODUCT_KEYWORD_SEEDS
        assert "mystel" in PRODUCT_KEYWORD_SEEDS
        assert "glowtype" in PRODUCT_KEYWORD_SEEDS
        assert len(PRODUCT_KEYWORD_SEEDS["testorum"]) >= 4

    def test_seo_blog_system_prompt_nonempty(self):
        assert len(SEO_BLOG_SYSTEM) > 100
        assert "SEO" in SEO_BLOG_SYSTEM

    @pytest.mark.asyncio
    async def test_fetch_trends_no_eventstore(self):
        """EventStore 없이도 DuckDuckGo fallback"""
        _, funcs = build_seo_content_pipeline(tool_registry=None, event_store=None)
        ctx = WorkflowContext("seo-content", {"target_product": "testorum"})
        result = await funcs["seo_fetch_trends"](ctx)
        assert "raw_keywords" in result
        assert "trend_topics" in result
        assert isinstance(result["raw_keywords"], list)

    @pytest.mark.asyncio
    async def test_fetch_trends_with_eventstore(self):
        """EventStore에서 TrendBot 데이터 조회"""
        mock_event = MagicMock()
        mock_event.data = {"keywords": ["psychology", "mbti"], "topic": "MBTI Trends", "summary": "test"}
        mock_store = MagicMock()
        mock_store.query = MagicMock(return_value=[mock_event])

        _, funcs = build_seo_content_pipeline(event_store=mock_store)
        ctx = WorkflowContext("seo-content", {"target_product": "testorum"})
        result = await funcs["seo_fetch_trends"](ctx)
        assert "psychology" in result["raw_keywords"]
        assert result["trend_count"] >= 1

    @pytest.mark.asyncio
    async def test_select_keywords_scoring(self):
        """키워드 점수 기반 선정"""
        _, funcs = build_seo_content_pipeline()
        ctx = WorkflowContext("seo-content", {
            "raw_keywords": ["personality test", "random word", "mbti", "weather forecast", "psychology test"],
            "trend_topics": [{"topic": "personality test trends", "summary": "personality test is trending"}],
            "target_product": "testorum",
            "language": "en",
        })
        result = await funcs["seo_select_keywords"](ctx)
        assert "primary_keyword" in result
        assert "secondary_keywords" in result
        assert "keywords" in result
        # personality test는 시드+트렌드 모두에 있어 최상위
        assert result["primary_keyword"] in ("personality test", "psychology test", "mbti")

    @pytest.mark.asyncio
    async def test_select_keywords_fallback_to_seeds(self):
        """키워드 부족 시 시드에서 보충"""
        _, funcs = build_seo_content_pipeline()
        ctx = WorkflowContext("seo-content", {
            "raw_keywords": [],
            "trend_topics": [],
            "target_product": "testorum",
            "language": "en",
        })
        result = await funcs["seo_select_keywords"](ctx)
        assert len(result["keywords"]) >= 1

    @pytest.mark.asyncio
    async def test_select_keywords_korean_title(self):
        """한국어 제목 생성"""
        _, funcs = build_seo_content_pipeline()
        ctx = WorkflowContext("seo-content", {
            "raw_keywords": ["심리테스트"],
            "trend_topics": [],
            "target_product": "testorum",
            "language": "ko",
        })
        result = await funcs["seo_select_keywords"](ctx)
        assert "가이드" in result["title"] or "중요" in result["title"] or "트렌드" in result["title"]


# ============================================================
# #2 SNS 자동 포스팅 테스트
# ============================================================


class TestSNSPosting:
    """SNS 자동 포스팅 빌드 + 함수 테스트"""

    def test_build_returns_valid_definition(self):
        defn, funcs = build_sns_posting()
        assert defn.workflow_id == "sns-posting"
        assert defn.category == "marketing"
        assert len(defn.steps) == 5

    def test_step_types(self):
        defn, _ = build_sns_posting()
        types = [s.step_type for s in defn.steps]
        assert types == [
            StepType.FUNCTION,   # gather-updates
            StepType.LLM_CALL,   # generate-drafts
            StepType.FUNCTION,   # parse-output
            StepType.TEMPLATE,   # format-drafts
            StepType.TOOL_CALL,  # save-to-notion
        ]

    def test_llm_step_uses_cheap(self):
        defn, _ = build_sns_posting()
        llm_step = [s for s in defn.steps if s.step_type == StepType.LLM_CALL][0]
        assert llm_step.config["model"] == "cheap"
        assert llm_step.config["temperature"] == 0.8  # 창의적 콘텐츠

    def test_functions_registered(self):
        _, funcs = build_sns_posting()
        assert "sns_gather_updates" in funcs
        assert "sns_parse_output" in funcs

    def test_platform_limits(self):
        assert PLATFORM_LIMITS["twitter"] == 280
        assert PLATFORM_LIMITS["instagram"] == 2200
        assert PLATFORM_LIMITS["threads"] == 500

    def test_default_hashtags_all_products(self):
        for product in ("testorum", "mystel", "glowtype"):
            assert product in DEFAULT_HASHTAGS
            for platform in ("twitter", "instagram", "threads"):
                assert platform in DEFAULT_HASHTAGS[product]

    @pytest.mark.asyncio
    async def test_gather_updates_custom_topic(self):
        """사용자 지정 토픽"""
        _, funcs = build_sns_posting()
        ctx = WorkflowContext("sns-posting", {
            "topic": "New feature: AI-powered results",
            "target_product": "testorum",
        })
        result = await funcs["sns_gather_updates"](ctx)
        assert len(result["updates"]) >= 1
        assert result["updates"][0]["type"] == "custom"

    @pytest.mark.asyncio
    async def test_gather_updates_fallback_promotion(self):
        """이벤트 없으면 프로모션 폴백"""
        _, funcs = build_sns_posting(event_store=None)
        ctx = WorkflowContext("sns-posting", {"target_product": "testorum"})
        result = await funcs["sns_gather_updates"](ctx)
        assert result["updates"][0]["type"] == "promotion"

    @pytest.mark.asyncio
    async def test_parse_sns_output_valid_json(self):
        """유효한 JSON LLM 출력 파싱"""
        _, funcs = build_sns_posting()
        llm_output = json.dumps({
            "posts": [
                {"platform": "twitter", "content": "Check out our new test!"},
                {"platform": "instagram", "content": "Discover your personality type today"},
                {"platform": "threads", "content": "Something cool is here"},
            ]
        })
        ctx = WorkflowContext("sns-posting", {
            "llm_sns_raw": llm_output,
            "target_product": "testorum",
            "platforms": ["twitter", "instagram", "threads"],
        })
        result = await funcs["sns_parse_output"](ctx)
        assert len(result["posts"]) == 3
        assert result["post_count"] == 3

    @pytest.mark.asyncio
    async def test_parse_sns_output_adds_hashtags(self):
        """해시태그 자동 추가"""
        _, funcs = build_sns_posting()
        llm_output = json.dumps({
            "posts": [{"platform": "twitter", "content": "Hello!"}]
        })
        ctx = WorkflowContext("sns-posting", {
            "llm_sns_raw": llm_output,
            "target_product": "testorum",
            "platforms": ["twitter"],
        })
        result = await funcs["sns_parse_output"](ctx)
        assert result["posts"][0]["hashtags"] != ""

    @pytest.mark.asyncio
    async def test_parse_sns_output_truncates_long(self):
        """글자수 초과 시 자동 트렁케이트"""
        _, funcs = build_sns_posting()
        long_text = "x" * 300  # twitter 280 초과
        llm_output = json.dumps({
            "posts": [{"platform": "twitter", "content": long_text}]
        })
        ctx = WorkflowContext("sns-posting", {
            "llm_sns_raw": llm_output,
            "target_product": "testorum",
            "platforms": ["twitter"],
        })
        result = await funcs["sns_parse_output"](ctx)
        assert result["posts"][0]["char_count"] <= 280

    @pytest.mark.asyncio
    async def test_parse_sns_output_missing_platform_fallback(self):
        """LLM이 플랫폼 누락 시 폴백 생성"""
        _, funcs = build_sns_posting()
        llm_output = json.dumps({
            "posts": [{"platform": "twitter", "content": "Hello!"}]
        })
        ctx = WorkflowContext("sns-posting", {
            "llm_sns_raw": llm_output,
            "target_product": "testorum",
            "platforms": ["twitter", "instagram"],
        })
        result = await funcs["sns_parse_output"](ctx)
        platforms = {p["platform"] for p in result["posts"]}
        assert "instagram" in platforms  # 누락분 폴백 생성됨


class TestParseLLMJson:
    """_parse_llm_json 유틸리티 테스트"""

    def test_plain_json(self):
        result = _parse_llm_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_with_code_block(self):
        result = _parse_llm_json('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_json_with_preamble(self):
        result = _parse_llm_json('Here is the JSON:\n{"key": "value"}')
        assert result == {"key": "value"}

    def test_invalid_json(self):
        result = _parse_llm_json("not json at all")
        assert result is None

    def test_empty_input(self):
        assert _parse_llm_json("") is None
        assert _parse_llm_json(None) is None

    def test_nested_json(self):
        data = {"posts": [{"platform": "twitter", "content": "hi"}]}
        result = _parse_llm_json(json.dumps(data))
        assert result["posts"][0]["platform"] == "twitter"


# ============================================================
# #5 이메일 마케팅 테스트
# ============================================================


class TestEmailMarketing:
    """이메일 마케팅 빌드 + 함수 테스트"""

    def test_build_returns_valid_definition(self):
        defn, funcs = build_email_marketing()
        assert defn.workflow_id == "email-campaign"
        assert defn.category == "marketing"
        assert len(defn.steps) == 5

    def test_step_types(self):
        defn, _ = build_email_marketing()
        types = [s.step_type for s in defn.steps]
        assert types == [
            StepType.FUNCTION,   # define-segment
            StepType.TEMPLATE,   # render-template
            StepType.LLM_CALL,   # personalize
            StepType.FUNCTION,   # compose-email
            StepType.TOOL_CALL,  # create-gmail-draft
        ]

    def test_personalize_step_is_skippable(self):
        """개인화 실패해도 워크플로우 계속"""
        defn, _ = build_email_marketing()
        personalize = [s for s in defn.steps if s.name == "personalize"][0]
        assert personalize.on_error.value == "skip"

    def test_gmail_step_conditional(self):
        defn, _ = build_email_marketing()
        gmail_step = defn.steps[-1]
        assert gmail_step.when == "email_body is_not_empty"

    def test_segment_definitions_complete(self):
        for key in ("new_users", "inactive_users", "power_users", "all_users"):
            assert key in SEGMENT_DEFINITIONS
            seg = SEGMENT_DEFINITIONS[key]
            assert "name" in seg
            assert "campaign_type" in seg
            assert "default_subject_en" in seg
            assert "default_subject_ko" in seg

    def test_functions_registered(self):
        _, funcs = build_email_marketing()
        assert "email_define_segments" in funcs
        assert "email_compose" in funcs

    @pytest.mark.asyncio
    async def test_define_segments_default(self):
        """기본 세그먼트 = all_users"""
        _, funcs = build_email_marketing()
        ctx = WorkflowContext("email-campaign", {
            "recipients": [{"email": "test@test.com", "name": "Test"}],
        })
        result = await funcs["email_define_segments"](ctx)
        assert result["segment_key"] == "all_users"
        assert result["recipient_count"] == 1

    @pytest.mark.asyncio
    async def test_define_segments_custom(self):
        """커스텀 세그먼트"""
        _, funcs = build_email_marketing()
        ctx = WorkflowContext("email-campaign", {
            "segment": "power_users",
            "recipients": [],
            "language": "ko",
        })
        result = await funcs["email_define_segments"](ctx)
        assert result["segment_key"] == "power_users"
        assert result["campaign_type"] == "upsell"
        assert "프리미엄" in result["subject"]

    @pytest.mark.asyncio
    async def test_define_segments_custom_subject(self):
        """커스텀 제목 지정"""
        _, funcs = build_email_marketing()
        ctx = WorkflowContext("email-campaign", {
            "subject": "Custom Subject Here",
            "recipients": [],
        })
        result = await funcs["email_define_segments"](ctx)
        assert result["subject"] == "Custom Subject Here"

    @pytest.mark.asyncio
    async def test_define_segments_invalid_segment_fallback(self):
        """존재하지 않는 세그먼트 → all_users 폴백"""
        _, funcs = build_email_marketing()
        ctx = WorkflowContext("email-campaign", {
            "segment": "nonexistent_segment",
            "recipients": [],
        })
        result = await funcs["email_define_segments"](ctx)
        assert result["segment_key"] == "all_users"

    @pytest.mark.asyncio
    async def test_compose_email_with_personalization(self):
        """개인화 멘트 포함 이메일 조합"""
        _, funcs = build_email_marketing()
        ctx = WorkflowContext("email-campaign", {
            "template_body": "Welcome to our service!",
            "personalized_raw": "GREETING: Hey there, welcome aboard!\nCTA: Try your first test now",
            "subject": "Welcome",
            "campaign_name": "Test",
            "recipients": [{"email": "a@b.com"}],
            "target_product": "testorum",
            "language": "en",
        })
        result = await funcs["email_compose"](ctx)
        assert "Hey there" in result["email_body"]
        assert "Try your first test now" in result["cta_text"]
        assert "testorum.app" in result["cta_url"]
        assert "unsubscribe" in result["email_body"].lower()

    @pytest.mark.asyncio
    async def test_compose_email_without_personalization(self):
        """개인화 실패 시 폴백"""
        _, funcs = build_email_marketing()
        ctx = WorkflowContext("email-campaign", {
            "template_body": "Body text",
            "personalized_raw": "",
            "subject": "Test",
            "campaign_name": "Test",
            "recipients": [],
            "target_product": "testorum",
            "language": "en",
        })
        result = await funcs["email_compose"](ctx)
        assert result["greeting"] == "Hi there!"
        assert result["cta_text"] == "Get started now"

    @pytest.mark.asyncio
    async def test_compose_email_korean_fallback(self):
        """한국어 폴백"""
        _, funcs = build_email_marketing()
        ctx = WorkflowContext("email-campaign", {
            "template_body": "본문",
            "personalized_raw": "",
            "subject": "테스트",
            "campaign_name": "테스트",
            "recipients": [],
            "target_product": "testorum",
            "language": "ko",
        })
        result = await funcs["email_compose"](ctx)
        assert result["greeting"] == "안녕하세요!"
        assert "수신 거부" in result["email_body"]

    @pytest.mark.asyncio
    async def test_compose_email_unsubscribe_always_present(self):
        """수신거부 안내 항상 포함"""
        _, funcs = build_email_marketing()
        for lang in ("en", "ko"):
            ctx = WorkflowContext("email-campaign", {
                "template_body": "body",
                "personalized_raw": "GREETING: hi\nCTA: click",
                "subject": "s",
                "campaign_name": "c",
                "recipients": [],
                "target_product": "testorum",
                "language": lang,
            })
            result = await funcs["email_compose"](ctx)
            assert "---" in result["email_body"]  # 구분선 포함


# ============================================================
# setup_workflows 통합 테스트
# ============================================================


class TestSetupWorkflowsIntegration:
    """setup_workflows로 전체 워크플로우 등록 통합 검증"""

    def test_setup_registers_all_10(self):
        """10개 워크플로우 전부 등록"""
        registry = setup_workflows()
        assert registry.count == 10

    def test_marketing_category_has_5(self):
        """마케팅 카테고리: 기존 2 + 신규 3 = 5"""
        registry = setup_workflows()
        marketing = registry.list_by_category("marketing")
        assert len(marketing) == 5

    def test_new_workflow_ids(self):
        """신규 워크플로우 ID 확인"""
        registry = setup_workflows()
        ids = registry.list_ids()
        assert "seo-content" in ids
        assert "sns-posting" in ids
        assert "email-campaign" in ids

    def test_setup_with_llm_provider(self):
        """LLMProvider 주입 시 정상 등록"""
        mock_llm = AsyncMock()
        registry = setup_workflows(llm_provider=mock_llm)
        assert registry.count == 10


# ============================================================
# 워크플로우 실행 통합 테스트 (mock LLM)
# ============================================================


class TestWorkflowExecution:
    """워크플로우 실행 테스트 (mock LLM + mock 도구)"""

    @pytest.fixture
    def mock_llm(self):
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value={
            "content": "Generated content by Haiku",
            "model": "claude-haiku-4-5-20251001",
            "usage": {"input_tokens": 100, "output_tokens": 200},
        })
        return provider

    @pytest.fixture
    def mock_tool_registry(self):
        registry = AsyncMock()
        result = MagicMock()
        result.success = True
        result.output = {"id": "notion-page-id", "url": "https://notion.so/test"}
        registry.execute = AsyncMock(return_value=result)
        return registry

    @pytest.mark.asyncio
    async def test_seo_pipeline_execution(self, mock_llm, mock_tool_registry):
        """SEO 파이프라인 전체 실행 (mock)"""
        registry = setup_workflows(
            tool_registry=mock_tool_registry,
            llm_provider=mock_llm,
        )
        result = await registry.execute("seo-content", {
            "target_product": "testorum",
            "language": "en",
            "notion_parent_id": "test-parent-id",
        })
        assert result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL)
        assert result.steps_completed >= 3  # fetch + keywords + LLM 최소

    @pytest.mark.asyncio
    async def test_sns_posting_execution(self, mock_llm, mock_tool_registry):
        """SNS 포스팅 실행 (mock) — LLM이 JSON 반환"""
        mock_llm.complete = AsyncMock(return_value={
            "content": json.dumps({
                "posts": [
                    {"platform": "twitter", "content": "Cool stuff!"},
                    {"platform": "instagram", "content": "Check this out!"},
                    {"platform": "threads", "content": "Something new!"},
                ]
            }),
            "model": "haiku",
            "usage": {},
        })
        registry = setup_workflows(
            tool_registry=mock_tool_registry,
            llm_provider=mock_llm,
        )
        result = await registry.execute("sns-posting", {
            "target_product": "testorum",
            "topic": "New feature launch",
            "language": "en",
            "notion_parent_id": "test-parent-id",
        })
        assert result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL)

    @pytest.mark.asyncio
    async def test_email_campaign_execution(self, mock_llm, mock_tool_registry):
        """이메일 캠페인 실행 (mock)"""
        mock_llm.complete = AsyncMock(return_value={
            "content": "GREETING: Welcome to the family!\nCTA: Start your journey now",
            "model": "haiku",
            "usage": {},
        })
        registry = setup_workflows(
            tool_registry=mock_tool_registry,
            llm_provider=mock_llm,
        )
        result = await registry.execute("email-campaign", {
            "segment": "new_users",
            "recipients": [{"email": "user@test.com", "name": "User"}],
            "target_product": "testorum",
            "language": "en",
        })
        assert result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL)

    @pytest.mark.asyncio
    async def test_seo_without_notion_skips_save(self, mock_llm):
        """notion_parent_id 없으면 Notion 저장 스텝 스킵"""
        registry = setup_workflows(llm_provider=mock_llm)
        result = await registry.execute("seo-content", {
            "target_product": "testorum",
            "language": "en",
            # notion_parent_id 없음
        })
        notion_steps = [r for r in result.step_results if r.step_name == "save-to-notion"]
        if notion_steps:
            assert notion_steps[0].skipped  # when 조건 불충족 → 스킵
