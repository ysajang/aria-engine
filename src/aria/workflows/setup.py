"""ARIA Engine - Workflow Setup

워크플로우 시스템 초기화 + 모든 워크플로우 등록
app.py lifespan에서 호출

사용법:
    registry = setup_workflows(
        tool_registry=tool_registry,
        template_engine=template_engine,
        event_store=event_store,
        aria_client=aria_client,
        notifier_func=send_telegram,
    )
"""

from __future__ import annotations

from typing import Any

import structlog

from aria.workflows.runner import WorkflowRunner
from aria.workflows.registry import WorkflowRegistry
from aria.workflows.templates import TemplateEngine
from aria.workflows.definitions import ALL_BUILDERS

logger = structlog.get_logger()


def setup_workflows(
    tool_registry: Any = None,
    event_store: Any = None,
    aria_client: Any = None,
    llm_provider: Any = None,
    notifier_func: Any = None,
    templates_dir: str = "./templates",
) -> WorkflowRegistry:
    """워크플로우 시스템 초기화

    1. TemplateEngine 생성
    2. WorkflowRunner 생성
    3. 모든 워크플로우 정의 빌드 + 등록

    Args:
        tool_registry: ToolRegistry (도구 호출용)
        event_store: EventStore (이벤트 조회/저장)
        aria_client: ARIAClient (비용 등 API 조회)
        llm_provider: LLMProvider (LLM 호출 — 최후 수단)
        notifier_func: 텔레그램 알림 함수 (async (str) -> None)
        templates_dir: 파일 기반 템플릿 디렉토리

    Returns:
        WorkflowRegistry (등록 완료)
    """
    # 1. TemplateEngine
    template_engine = TemplateEngine(templates_dir)

    # 2. WorkflowRunner
    runner = WorkflowRunner(
        tool_registry=tool_registry,
        template_engine=template_engine,
        llm_provider=llm_provider,
        event_store=event_store,
        notifier_func=notifier_func,
    )

    # 3. WorkflowRegistry
    registry = WorkflowRegistry(runner)

    # 4. 모든 워크플로우 빌드 + 등록
    services = {
        "tool_registry": tool_registry,
        "event_store": event_store,
        "aria_client": aria_client,
        "llm_provider": llm_provider,
    }

    registered = 0
    for builder in ALL_BUILDERS:
        try:
            definition, functions = builder(**services)

            # 함수 등록
            for func_name, func in functions.items():
                runner.register_function(func_name, func)

            # 워크플로우 등록
            registry.register(definition)
            registered += 1
        except Exception as e:
            logger.error(
                "workflow_setup_failed",
                builder=builder.__name__,
                error=str(e)[:200],
            )

    logger.info(
        "workflows_setup_complete",
        registered=registered,
        total=len(ALL_BUILDERS),
        marketing=len(registry.list_by_category("marketing")),
        admin=len(registry.list_by_category("admin")),
    )

    return registry
