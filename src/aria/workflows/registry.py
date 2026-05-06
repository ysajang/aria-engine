"""ARIA Engine - Workflow Registry

워크플로우 등록/조회/트리거 관리
- 워크플로우 정의 등록 (register)
- 카테고리별 조회 (list_by_category)
- ID 기반 실행 (execute)
- 텔레그램 명령 라우팅 (/marketing, /admin)

모든 워크플로우 정의는 여기서 등록
실행은 WorkflowRunner에 위임
"""

from __future__ import annotations

from typing import Any

import structlog

from aria.workflows.runner import WorkflowRunner
from aria.workflows.types import WorkflowDefinition, WorkflowResult

logger = structlog.get_logger()


class WorkflowRegistry:
    """워크플로우 중앙 레지스트리

    Args:
        runner: WorkflowRunner 인스턴스
    """

    def __init__(self, runner: WorkflowRunner) -> None:
        self._runner = runner
        self._workflows: dict[str, WorkflowDefinition] = {}

    def register(self, definition: WorkflowDefinition) -> None:
        """워크플로우 등록

        Raises:
            ValueError: 이미 등록된 workflow_id
        """
        if definition.workflow_id in self._workflows:
            raise ValueError(
                f"이미 등록된 워크플로우: '{definition.workflow_id}'"
            )
        self._workflows[definition.workflow_id] = definition
        logger.info(
            "workflow_registered",
            workflow_id=definition.workflow_id,
            category=definition.category,
            steps=len(definition.steps),
        )

    def unregister(self, workflow_id: str) -> bool:
        """워크플로우 등록 해제"""
        if workflow_id in self._workflows:
            del self._workflows[workflow_id]
            logger.info("workflow_unregistered", workflow_id=workflow_id)
            return True
        return False

    def get(self, workflow_id: str) -> WorkflowDefinition | None:
        """워크플로우 정의 조회"""
        return self._workflows.get(workflow_id)

    def list_all(self) -> list[WorkflowDefinition]:
        """전체 워크플로우 목록"""
        return list(self._workflows.values())

    def list_by_category(self, category: str) -> list[WorkflowDefinition]:
        """카테고리별 워크플로우 목록"""
        return [w for w in self._workflows.values() if w.category == category]

    def list_ids(self) -> list[str]:
        """등록된 워크플로우 ID 목록"""
        return list(self._workflows.keys())

    async def execute(
        self,
        workflow_id: str,
        initial_data: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        """워크플로우 실행

        Args:
            workflow_id: 실행할 워크플로우 ID
            initial_data: 초기 컨텍스트 데이터

        Returns:
            WorkflowResult

        Raises:
            ValueError: 등록되지 않은 workflow_id
        """
        definition = self._workflows.get(workflow_id)
        if not definition:
            raise ValueError(
                f"등록되지 않은 워크플로우: '{workflow_id}'. "
                f"사용 가능: {list(self._workflows.keys())}"
            )

        return await self._runner.run(definition, initial_data)

    def to_help_text(self, category: str | None = None) -> str:
        """텔레그램 도움말 텍스트 생성

        Args:
            category: 필터할 카테고리 (None이면 전체)

        Returns:
            Markdown 텍스트
        """
        workflows = (
            self.list_by_category(category) if category else self.list_all()
        )

        if not workflows:
            return "등록된 워크플로우가 없습니다."

        lines = []
        current_cat = ""
        for w in sorted(workflows, key=lambda x: (x.category, x.workflow_id)):
            if w.category != current_cat:
                current_cat = w.category
                emoji = {"marketing": "📢", "admin": "📋"}.get(current_cat, "🔧")
                lines.append(f"\n{emoji} *{current_cat.upper()}*")

            desc = w.description[:80] if w.description else w.name
            lines.append(f"  `{w.workflow_id}` — {desc}")

        return "\n".join(lines)

    @property
    def count(self) -> int:
        """등록된 워크플로우 수"""
        return len(self._workflows)
