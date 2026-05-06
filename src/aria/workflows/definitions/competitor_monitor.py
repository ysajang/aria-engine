"""#3 경쟁사 모니터링

cron 매일 또는 /marketing competitor 명령으로 트리거
경쟁사별 웹/뉴스 검색 → 변화 감지 → 리포트 + Notion 저장

LLM: 불필요 (검색 도구 + 규칙 기반 변화 감지 + 템플릿)
도구: ddg_news_search + naver_news_search + notion_create_page
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aria.workflows.types import (
    OnError,
    StepType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowFunc,
    WorkflowStep,
)

# 기본 경쟁사 목록 (initial_data로 오버라이드 가능)
DEFAULT_COMPETITORS = [
    {"name": "16personalities", "keywords": ["16personalities", "mbti test"]},
    {"name": "IDRlabs", "keywords": ["idrlabs", "personality test"]},
]


def build_competitor_monitor(
    tool_registry: Any = None,
    event_store: Any = None,
    **kwargs: Any,
) -> tuple[WorkflowDefinition, dict[str, WorkflowFunc]]:
    """경쟁사 모니터링 워크플로우 빌드"""

    async def search_competitors(ctx: WorkflowContext) -> list[dict[str, Any]]:
        """경쟁사별 뉴스/웹 검색"""
        competitors = ctx.get("competitors", DEFAULT_COMPETITORS)
        all_results: list[dict[str, Any]] = []

        for comp in competitors:
            name = comp.get("name", "")
            keywords = comp.get("keywords", [name])

            for kw in keywords[:2]:  # 키워드당 최대 2개
                # DuckDuckGo 뉴스 검색
                if tool_registry:
                    try:
                        result = await tool_registry.execute(
                            tool_name="ddg_news_search",
                            arguments={"query": kw, "max_results": 5},
                            context="competitor_monitor",
                        )
                        if result.success and result.output:
                            items = result.output if isinstance(result.output, list) else []
                            for item in items[:3]:
                                all_results.append({
                                    "competitor": name,
                                    "title": item.get("title", ""),
                                    "summary": item.get("body", item.get("snippet", ""))[:200],
                                    "source": "ddg_news",
                                    "url": item.get("url", item.get("href", "")),
                                    "detected_at": datetime.now(timezone.utc).isoformat(),
                                })
                    except Exception:
                        pass  # 도구 실패 시 무시하고 계속

                # 네이버 뉴스 검색
                if tool_registry:
                    try:
                        result = await tool_registry.execute(
                            tool_name="naver_news_search",
                            arguments={"query": kw, "display": 3},
                            context="competitor_monitor",
                        )
                        if result.success and result.output:
                            items = result.output.get("items", []) if isinstance(result.output, dict) else []
                            for item in items[:3]:
                                all_results.append({
                                    "competitor": name,
                                    "title": item.get("title", "").replace("<b>", "").replace("</b>", ""),
                                    "summary": item.get("description", "").replace("<b>", "").replace("</b>", "")[:200],
                                    "source": "naver_news",
                                    "url": item.get("link", ""),
                                    "detected_at": datetime.now(timezone.utc).isoformat(),
                                })
                    except Exception:
                        pass

        return all_results

    async def detect_changes(ctx: WorkflowContext) -> list[dict[str, Any]]:
        """이전 결과와 비교하여 새로운 항목만 추출

        간단한 중복 제거: URL 기반
        이전 결과는 EventStore에서 조회
        """
        search_results = ctx.get("search_results", [])
        if not search_results:
            return []

        # 이전 URL 목록 (최근 7일 이벤트에서)
        previous_urls: set[str] = set()
        if event_store:
            try:
                from aria.events.types import EventQuery
                from datetime import timedelta

                since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                events = await event_store.query(EventQuery(
                    source="aria",
                    event_type="competitor_change",
                    since=since,
                    limit=200,
                ))
                for ev in events:
                    url = ev.data.get("url", "")
                    if url:
                        previous_urls.add(url)
            except Exception:
                pass

        # 새로운 항목 필터링
        changes = []
        for item in search_results:
            url = item.get("url", "")
            if url and url not in previous_urls:
                changes.append(item)

        # 새 URL들을 이벤트로 저장
        if changes and event_store:
            try:
                from aria.events.types import EventInput

                for change in changes[:20]:
                    ev = EventInput(
                        event_type="competitor_change",
                        source="aria",
                        data={
                            "competitor": change.get("competitor", ""),
                            "url": change.get("url", ""),
                            "title": change.get("title", ""),
                        },
                    )
                    stored = ev.to_event()
                    await event_store.store(stored)
            except Exception:
                pass

        return changes

    definition = WorkflowDefinition(
        workflow_id="competitor-monitor",
        name="경쟁사 모니터링",
        description="경쟁사 뉴스/웹 변화 감지 → 리포트",
        category="marketing",
        scope="global",
        steps=[
            WorkflowStep(
                name="search",
                step_type=StepType.FUNCTION,
                config={"func_name": "competitor_search"},
                output_key="search_results",
                on_error=OnError.ABORT,
            ),
            WorkflowStep(
                name="detect-changes",
                step_type=StepType.FUNCTION,
                config={"func_name": "competitor_detect_changes"},
                output_key="changes",
            ),
            WorkflowStep(
                name="render-report",
                step_type=StepType.TEMPLATE,
                config={"template_name": "marketing/competitor_report.md.j2"},
                output_key="report",
            ),
        ],
    )

    functions = {
        "competitor_search": search_competitors,
        "competitor_detect_changes": detect_changes,
    }
    return definition, functions
