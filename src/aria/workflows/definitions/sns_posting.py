"""#2 SNS 자동 포스팅 준비

제품 업데이트/트렌드 → 플랫폼별 초안(Haiku+템플릿) → Notion 드래프트

트리거: cron 주 2회 / /marketing sns-posting 명령
LLM: Haiku (플랫폼별 초안 생성 스텝만)
도구: notion_create_page

플로우:
1. FUNCTION: 소스 데이터 수집 (EventStore 제품 이벤트 + 트렌드)
2. LLM_CALL: Haiku로 플랫폼별 SNS 초안 생성 (Twitter/Instagram/etc)
3. FUNCTION: LLM 출력 파싱 → 플랫폼별 분리 + 글자수 검증
4. TEMPLATE: 최종 SNS 드래프트 세트 포맷팅
5. TOOL_CALL: Notion에 드래프트 저장
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from typing import Any

from aria.workflows.types import (
    OnError,
    StepType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowFunc,
    WorkflowStep,
)

# 플랫폼별 글자수 제한
PLATFORM_LIMITS: dict[str, int] = {
    "twitter": 280,
    "instagram": 2200,
    "threads": 500,
}

# 플랫폼별 해시태그 기본값
DEFAULT_HASHTAGS: dict[str, dict[str, str]] = {
    "testorum": {
        "twitter": "#PersonalityTest #SelfDiscovery #Psychology",
        "instagram": "#personalitytest #selfdiscovery #psychology #mbti #testorum #knowyourself",
        "threads": "#PersonalityTest #SelfDiscovery #Testorum",
    },
    "mystel": {
        "twitter": "#Tarot #Fortune #DailyHoroscope",
        "instagram": "#tarot #fortune #horoscope #mystel #dailytarot #zodiac",
        "threads": "#Tarot #Fortune #Mystel",
    },
    "glowtype": {
        "twitter": "#GlowUp #CharmQuiz #SelfCare",
        "instagram": "#glowup #charmquiz #selfcare #beauty #glowtype #attractiveness",
        "threads": "#GlowUp #CharmQuiz #GlowType",
    },
}

# SNS 초안 시스템 프롬프트
SNS_DRAFT_SYSTEM = """\
You are a social media content specialist for consumer tech products.
Create engaging SNS posts for multiple platforms from the given content.

Output format — respond ONLY with valid JSON (no markdown fences):
{
  "posts": [
    {
      "platform": "twitter",
      "content": "post text here (max 250 chars to leave room for hashtags)"
    },
    {
      "platform": "instagram",
      "content": "post text here (can be longer, include line breaks)"
    },
    {
      "platform": "threads",
      "content": "post text here (max 470 chars)"
    }
  ]
}

Rules:
- Each platform gets a unique version (not copy-paste)
- Twitter: punchy, conversational, 1-2 sentences max
- Instagram: storytelling tone, can use emoji, 3-5 sentences
- Threads: casual/conversational, 2-3 sentences
- Include a soft CTA where natural (try it / check it out / link in bio)
- Do NOT include hashtags in content (added separately)
- Write in the specified language
- NEVER use misleading or clickbait language
"""


def build_sns_posting(
    tool_registry: Any = None,
    event_store: Any = None,
    **kwargs: Any,
) -> tuple[WorkflowDefinition, dict[str, WorkflowFunc]]:
    """SNS 자동 포스팅 준비 워크플로우 빌드"""

    async def gather_updates(ctx: WorkflowContext) -> dict[str, Any]:
        """소스 데이터 수집: 제품 이벤트 + 트렌드 토픽

        EventStore에서 최근 7일 제품 업데이트/마일스톤 이벤트 조회
        사용자가 topic을 직접 지정할 수도 있음
        """
        target_product = ctx.get("target_product", "testorum")
        custom_topic = ctx.get("topic", "")
        language = ctx.get("language", "en")

        updates: list[dict[str, Any]] = []

        # 사용자 직접 지정 토픽
        if custom_topic:
            updates.append({
                "type": "custom",
                "title": custom_topic,
                "summary": custom_topic,
            })

        # EventStore에서 제품 이벤트
        if event_store and not custom_topic:
            try:
                from aria.events.types import EventQuery

                since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                events = event_store.query(EventQuery(
                    source=target_product,
                    since=since,
                    limit=20,
                ))
                for ev in events:
                    ev_type = ev.event_type
                    # 포스팅 가치 있는 이벤트 필터링
                    if ev_type in (
                        "feature_launch", "milestone", "update",
                        "user_milestone", "content_published",
                    ):
                        updates.append({
                            "type": ev_type,
                            "title": ev.data.get("title", ev_type),
                            "summary": ev.data.get("summary", "")[:300],
                        })
            except Exception:
                pass

        # 업데이트 없으면 일반 프로모션 토픽 폴백
        if not updates:
            fallback_topics = {
                "testorum": "Discover your personality type with fun, science-backed tests",
                "mystel": "Get your daily tarot reading and fortune insights",
                "glowtype": "Find out your charm type with our quick quiz",
            }
            updates.append({
                "type": "promotion",
                "title": f"{target_product} promotion",
                "summary": fallback_topics.get(target_product, f"Check out {target_product}"),
            })

        return {
            "updates": updates[:5],
            "target_product": target_product,
            "language": language,
            "platforms": ctx.get("platforms", list(PLATFORM_LIMITS.keys())),
        }

    async def parse_sns_output(ctx: WorkflowContext) -> dict[str, Any]:
        """LLM 출력 파싱 → 플랫폼별 분리 + 글자수 검증 + 해시태그 추가"""
        llm_output = ctx.get("llm_sns_raw", "")
        target_product = ctx.get("target_product", "testorum")
        platforms = ctx.get("platforms", list(PLATFORM_LIMITS.keys()))

        posts: list[dict[str, Any]] = []

        # JSON 파싱 시도
        parsed = _parse_llm_json(llm_output)
        raw_posts = parsed.get("posts", []) if parsed else []

        # 해시태그 맵
        hashtags_map = DEFAULT_HASHTAGS.get(target_product, {})

        for raw in raw_posts:
            platform = raw.get("platform", "").lower().strip()
            content = raw.get("content", "").strip()

            if not platform or not content or platform not in PLATFORM_LIMITS:
                continue

            # 글자수 제한 적용
            limit = PLATFORM_LIMITS[platform]
            if len(content) > limit:
                content = content[: limit - 3].rstrip() + "..."

            hashtags = hashtags_map.get(platform, "")

            posts.append({
                "platform": platform,
                "content": content,
                "hashtags": hashtags,
                "char_count": len(content),
            })

        # 누락 플랫폼 체크 (LLM이 일부 빠뜨릴 수 있음)
        existing_platforms = {p["platform"] for p in posts}
        for platform in platforms:
            if platform not in existing_platforms:
                posts.append({
                    "platform": platform,
                    "content": f"[초안 생성 실패 — 수동 작성 필요]",
                    "hashtags": hashtags_map.get(platform, ""),
                    "char_count": 0,
                })

        # 토픽 요약 (템플릿용)
        updates = ctx.get("updates", [])
        topic = updates[0].get("title", "update") if updates else "update"

        return {
            "posts": posts,
            "topic": topic,
            "post_count": len([p for p in posts if p["char_count"] > 0]),
        }

    definition = WorkflowDefinition(
        workflow_id="sns-posting",
        name="SNS 자동 포스팅 준비",
        description="제품 업데이트/트렌드 → 플랫폼별 SNS 초안 → Notion 드래프트",
        category="marketing",
        scope="global",
        steps=[
            # Step 1: 소스 데이터 수집
            WorkflowStep(
                name="gather-updates",
                step_type=StepType.FUNCTION,
                config={"func_name": "sns_gather_updates"},
                output_key="source_data",
                on_error=OnError.ABORT,
                description="EventStore에서 제품 업데이트 + 트렌드 수집",
            ),
            # Step 2: Haiku로 플랫폼별 SNS 초안 생성
            WorkflowStep(
                name="generate-drafts",
                step_type=StepType.LLM_CALL,
                config={
                    "system": SNS_DRAFT_SYSTEM,
                    "prompt": (
                        "Create SNS posts for the following:\n"
                        "- Product: {target_product}\n"
                        "- Platforms: {platforms}\n"
                        "- Language: {language}\n"
                        "- Content to promote:\n{updates}\n\n"
                        "Respond with JSON only."
                    ),
                    "model": "cheap",
                    "max_tokens": 1024,
                    "temperature": 0.8,
                },
                output_key="llm_sns_raw",
                on_error=OnError.RETRY,
                description="Haiku로 Twitter/Instagram/Threads 초안 생성",
            ),
            # Step 3: LLM 출력 파싱 + 검증
            WorkflowStep(
                name="parse-output",
                step_type=StepType.FUNCTION,
                config={"func_name": "sns_parse_output"},
                output_key="parsed_posts",
                on_error=OnError.ABORT,
                description="JSON 파싱 + 글자수 검증 + 해시태그 추가",
            ),
            # Step 4: 최종 포맷팅
            WorkflowStep(
                name="format-drafts",
                step_type=StepType.TEMPLATE,
                config={"template_name": "marketing/sns_post_set.md.j2"},
                output_key="formatted_drafts",
                description="SNS 드래프트 세트 마크다운 포맷팅",
            ),
            # Step 5: Notion 저장 (조건부)
            WorkflowStep(
                name="save-to-notion",
                step_type=StepType.TOOL_CALL,
                config={
                    "tool_name": "notion_create_page",
                    "args": {
                        "parent_id": "{notion_parent_id}",
                        "title": "[SNS] {topic} ({now})",
                        "content": "{formatted_drafts}",
                    },
                },
                output_key="notion_result",
                on_error=OnError.SKIP,
                when="notion_parent_id exists",
                description="Notion에 SNS 드래프트 세트 저장",
            ),
        ],
    )

    functions = {
        "sns_gather_updates": gather_updates,
        "sns_parse_output": parse_sns_output,
    }
    return definition, functions


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    """LLM 출력에서 JSON 추출 (마크다운 코드블록 대응)

    ```json ... ``` 또는 순수 JSON 모두 처리
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # 마크다운 코드블록 제거
    code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if code_block:
        text = code_block.group(1).strip()

    # JSON 객체 추출 (첫 번째 { ... } 매칭)
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        text = brace_match.group(0)

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
