"""ARIA Engine - Workflow Automation

마케팅/행정 자동화 워크플로우 시스템
- 기존 74종 도구 조합으로 워크플로우 구성
- LLM 비종속: 규칙/템플릿 우선 + LLM은 Fallback(Haiku)
- 트리거: 텔레그램 명령 또는 cron
"""

from aria.workflows.types import (
    OnError,
    StepResult,
    StepType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowResult,
    WorkflowStatus,
    WorkflowStep,
)
from aria.workflows.templates import TemplateEngine
from aria.workflows.runner import WorkflowRunner
from aria.workflows.registry import WorkflowRegistry

__all__ = [
    "OnError",
    "StepResult",
    "StepType",
    "TemplateEngine",
    "WorkflowContext",
    "WorkflowDefinition",
    "WorkflowRegistry",
    "WorkflowResult",
    "WorkflowRunner",
    "WorkflowStatus",
    "WorkflowStep",
]
