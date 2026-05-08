"""#1 SEO 콘텐츠 파이프라인

TrendBot 데이터 → 키워드 선정(규칙) → 블로그 초안(Haiku) → Notion 저장

트리거: cron 주 3회 / /marketing seo-content 명령
LLM: Haiku (블로그 초안 생성 스텝만)
도구: ddg_web_search + naver_blog_search + notion_create_page

플로우:
1. FUNCTION: EventStore에서 TrendBot 트렌드 데이터 수집
2. FUNCTION: 규칙 기반 키워드 선정 (검색량 추정 + 경쟁도 + 제품 관련성)
3. LLM_CALL: Haiku로 블로그 초안 작성 (키워드 + 아웃라인 기반)
4. TEMPLATE: SEO 메타 포함 최종 마크다운 조합
5. TOOL_CALL: Notion에 드래프트 저장
"""

from __future__ import annotations

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

# 제품별 관련 키워드 시드 (규칙 기반 관련성 판단)
PRODUCT_KEYWORD_SEEDS: dict[str, list[str]] = {
    "testorum": [
        "personality test", "psychology test", "mbti", "enneagram",
        "심리테스트", "성격테스트", "성격유형", "자기이해",
    ],
    "mystel": [
        "tarot", "fortune telling", "horoscope", "zodiac",
        "타로", "운세", "별자리", "사주",
    ],
    "glowtype": [
        "attractiveness", "charm", "glow up", "beauty quiz",
        "매력", "매력도", "외모", "자기관리",
    ],
}

# 블로그 초안 시스템 프롬프트
SEO_BLOG_SYSTEM = """\
You are an expert SEO blog writer for consumer psychology and self-discovery products.
Write engaging, informative blog posts that naturally incorporate target keywords.

Rules:
- Write in the specified language (Korean or English)
- 800-1200 words for the main body
- Include the primary keyword in the first paragraph
- Use H2/H3 subheadings that include secondary keywords
- Write a compelling introduction that hooks the reader
- End with a soft CTA mentioning the relevant product
- Tone: friendly, authoritative, slightly playful
- Do NOT use clickbait or misleading claims
- Include 2-3 internal linking opportunities (marked as [LINK:topic])
- Output ONLY the blog body text (no frontmatter/meta)
"""


def build_seo_content_pipeline(
    tool_registry: Any = None,
    event_store: Any = None,
    **kwargs: Any,
) -> tuple[WorkflowDefinition, dict[str, WorkflowFunc]]:
    """SEO 콘텐츠 파이프라인 워크플로우 빌드"""

    async def fetch_trend_data(ctx: WorkflowContext) -> dict[str, Any]:
        """EventStore에서 TrendBot 트렌드 데이터 수집

        최근 7일 trendbot 이벤트에서 키워드/토픽 추출
        TrendBot 데이터 없으면 DuckDuckGo 뉴스로 대체
        """
        raw_keywords: list[str] = []
        trend_topics: list[dict[str, Any]] = []

        # 1) EventStore에서 TrendBot 데이터 조회
        if event_store:
            try:
                from aria.events.types import EventQuery

                since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                events = event_store.query(EventQuery(
                    source="trendbot",
                    since=since,
                    limit=50,
                ))
                for ev in events:
                    # TrendBot 이벤트 구조: data.keywords / data.topic / data.summary
                    kws = ev.data.get("keywords", [])
                    if isinstance(kws, list):
                        raw_keywords.extend(kws[:5])
                    topic = ev.data.get("topic", "")
                    if topic:
                        trend_topics.append({
                            "topic": topic,
                            "summary": ev.data.get("summary", "")[:200],
                            "source": "trendbot",
                        })
            except Exception:
                pass

        # 2) TrendBot 데이터 부족 시 DuckDuckGo 뉴스 보충
        if len(trend_topics) < 3 and tool_registry:
            target_product = ctx.get("target_product", "testorum")
            seeds = PRODUCT_KEYWORD_SEEDS.get(target_product, [])
            search_query = seeds[0] if seeds else "personality test trends"

            try:
                result = await tool_registry.execute(
                    tool_name="ddg_news_search",
                    arguments={"query": search_query, "max_results": 5},
                    context="seo_content_pipeline",
                )
                if result.success and isinstance(result.output, list):
                    for item in result.output[:5]:
                        title = item.get("title", "")
                        if title:
                            trend_topics.append({
                                "topic": title,
                                "summary": item.get("body", "")[:200],
                                "source": "ddg_news",
                            })
                            # 제목에서 키워드 추출 (공백 분리 → 2글자 이상)
                            words = [w.strip(".,!?()[]") for w in title.split()]
                            raw_keywords.extend(
                                w for w in words if len(w) >= 2
                            )
            except Exception:
                pass

        # 중복 제거
        seen: set[str] = set()
        unique_keywords: list[str] = []
        for kw in raw_keywords:
            kw_lower = kw.lower().strip()
            if kw_lower and kw_lower not in seen:
                seen.add(kw_lower)
                unique_keywords.append(kw_lower)

        return {
            "raw_keywords": unique_keywords[:20],
            "trend_topics": trend_topics[:10],
            "trend_count": len(trend_topics),
        }

    async def select_keywords(ctx: WorkflowContext) -> dict[str, Any]:
        """규칙 기반 키워드 선정

        점수 = 제품 관련성(0-3) + 트렌드 언급(0-2) + 길이 적정성(0-1)
        상위 5개 선정 → primary(1) + secondary(4)
        """
        raw_keywords = ctx.get("raw_keywords", [])
        trend_topics = ctx.get("trend_topics", [])
        target_product = ctx.get("target_product", "testorum")
        language = ctx.get("language", "en")

        # 제품 시드 키워드
        seeds = PRODUCT_KEYWORD_SEEDS.get(target_product, [])
        seed_set = {s.lower() for s in seeds}

        # 트렌드 토픽 텍스트 (관련성 판단용)
        trend_text = " ".join(
            t.get("topic", "") + " " + t.get("summary", "")
            for t in trend_topics
        ).lower()

        scored: list[tuple[str, float]] = []
        for kw in raw_keywords:
            score = 0.0

            # 제품 관련성: 시드에 포함되면 +3 / 시드 단어 일부 포함 +1
            if kw in seed_set:
                score += 3.0
            elif any(seed in kw or kw in seed for seed in seed_set):
                score += 1.0

            # 트렌드 언급 빈도
            mentions = trend_text.count(kw)
            score += min(mentions, 2)

            # 길이 적정성 (2-4 단어가 롱테일로 좋음)
            word_count = len(kw.split())
            if 2 <= word_count <= 4:
                score += 1.0
            elif word_count == 1 and len(kw) >= 4:
                score += 0.5

            scored.append((kw, score))

        # 점수 내림차순 정렬
        scored.sort(key=lambda x: x[1], reverse=True)
        top_keywords = [kw for kw, _ in scored[:5]]

        # 키워드 부족 시 시드에서 보충
        if len(top_keywords) < 3:
            for seed in seeds:
                if seed.lower() not in {k.lower() for k in top_keywords}:
                    top_keywords.append(seed.lower())
                    if len(top_keywords) >= 5:
                        break

        primary = top_keywords[0] if top_keywords else target_product
        secondary = top_keywords[1:5] if len(top_keywords) > 1 else []

        # 블로그 제목 후보 (규칙 기반)
        if language == "ko":
            title_candidates = [
                f"{primary} 완벽 가이드: 알아야 할 모든 것",
                f"왜 {primary}이(가) 중요한가?",
                f"{primary} 트렌드 분석 ({datetime.now().year})",
            ]
        else:
            title_candidates = [
                f"The Complete Guide to {primary.title()}",
                f"Why {primary.title()} Matters in {datetime.now().year}",
                f"{primary.title()}: Trends and Insights",
            ]

        # SEO 메타 디스크립션 (규칙 기반)
        keywords_str = ", ".join([primary] + secondary[:2])
        if language == "ko":
            seo_description = f"{primary}에 대해 알아보세요. {keywords_str} 관련 최신 정보와 인사이트를 제공합니다."
        else:
            seo_description = f"Learn about {primary}. Get the latest insights on {keywords_str} and more."

        return {
            "primary_keyword": primary,
            "secondary_keywords": secondary,
            "keywords": [primary] + secondary,
            "title": title_candidates[0],
            "seo_title": title_candidates[0][:60],
            "seo_description": seo_description[:155],
            "category": target_product,
        }

    definition = WorkflowDefinition(
        workflow_id="seo-content",
        name="SEO 콘텐츠 파이프라인",
        description="TrendBot → 키워드 선정 → 블로그 초안(Haiku) → Notion 저장",
        category="marketing",
        scope="global",
        steps=[
            # Step 1: 트렌드 데이터 수집
            WorkflowStep(
                name="fetch-trends",
                step_type=StepType.FUNCTION,
                config={"func_name": "seo_fetch_trends"},
                output_key="trend_data",
                on_error=OnError.ABORT,
                description="EventStore + DuckDuckGo에서 트렌드 데이터 수집",
            ),
            # Step 2: 키워드 선정 (규칙 기반)
            WorkflowStep(
                name="select-keywords",
                step_type=StepType.FUNCTION,
                config={"func_name": "seo_select_keywords"},
                output_key="keyword_data",
                on_error=OnError.ABORT,
                description="규칙 기반 키워드 선정 (관련성+트렌드+길이 점수)",
            ),
            # Step 3: 블로그 초안 생성 (Haiku LLM)
            WorkflowStep(
                name="generate-draft",
                step_type=StepType.LLM_CALL,
                config={
                    "system": SEO_BLOG_SYSTEM,
                    "prompt": (
                        "Write a blog post with the following specifications:\n"
                        "- Title: {title}\n"
                        "- Primary keyword: {primary_keyword}\n"
                        "- Secondary keywords: {secondary_keywords}\n"
                        "- Target product: {category}\n"
                        "- Language: {language}\n"
                        "- Trending context: {trend_topics}\n\n"
                        "Write the blog body only (800-1200 words)."
                    ),
                    "model": "cheap",
                    "max_tokens": 2048,
                    "temperature": 0.7,
                },
                output_key="draft_body",
                on_error=OnError.RETRY,
                description="Haiku로 SEO 블로그 초안 생성",
            ),
            # Step 4: SEO 메타 포함 최종 마크다운 조합
            WorkflowStep(
                name="format-post",
                step_type=StepType.TEMPLATE,
                config={"template_name": "marketing/seo_blog_draft.md.j2"},
                output_key="final_post",
                description="SEO 메타 + 본문 조합",
            ),
            # Step 5: Notion 저장 (조건부 — notion_parent_id 필요)
            WorkflowStep(
                name="save-to-notion",
                step_type=StepType.TOOL_CALL,
                config={
                    "tool_name": "notion_create_page",
                    "args": {
                        "parent_id": "{notion_parent_id}",
                        "title": "[SEO] {title}",
                        "content": "{final_post}",
                    },
                },
                output_key="notion_result",
                on_error=OnError.SKIP,
                when="notion_parent_id exists",
                description="Notion에 블로그 드래프트 저장",
            ),
        ],
    )

    functions = {
        "seo_fetch_trends": fetch_trend_data,
        "seo_select_keywords": select_keywords,
    }
    return definition, functions
