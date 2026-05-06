"""ARIA Engine - Workflow Types

워크플로우 스키마 정의 (Pydantic v2)
- StepType: 스텝 실행 유형 (tool_call/template/llm_call/function/condition)
- OnError: 에러 처리 정책 (abort/skip/retry)
- WorkflowStep: 워크플로우 단일 스텝
- WorkflowDefinition: 워크플로우 전체 정의
- WorkflowContext: 스텝 간 공유 데이터
- StepResult: 단일 스텝 실행 결과
- WorkflowResult: 워크플로우 전체 실행 결과
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

from pydantic import BaseModel, Field, field_validator


class StepType(str, Enum):
    """워크플로우 스텝 유형

    - TOOL_CALL: ToolRegistry 등록 도구 호출
    - TEMPLATE: Jinja2 템플릿 렌더링
    - LLM_CALL: LLM 호출 (Haiku — 최후 수단)
    - FUNCTION: 커스텀 async callable
    - CONDITION: 규칙 기반 분기 (context 데이터 조건 평가)
    """

    TOOL_CALL = "tool_call"
    TEMPLATE = "template"
    LLM_CALL = "llm_call"
    FUNCTION = "function"
    CONDITION = "condition"


class OnError(str, Enum):
    """스텝 실패 시 정책

    - ABORT: 워크플로우 즉시 중단
    - SKIP: 실패 무시하고 다음 스텝 진행
    - RETRY: 최대 2회 재시도 후 실패 시 ABORT
    """

    ABORT = "abort"
    SKIP = "skip"
    RETRY = "retry"


class WorkflowStatus(str, Enum):
    """워크플로우 실행 상태"""

    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"  # 일부 스텝 skip 후 완료


class WorkflowStep(BaseModel):
    """워크플로우 단일 스텝 정의

    step_type별 config 규격:
    - TOOL_CALL: {"tool_name": str, "args": dict}
    - TEMPLATE: {"template_name": str} 또는 {"template_string": str}
    - LLM_CALL: {"prompt": str, "system": str?, "model": str?}
    - FUNCTION: 별도 func 파라미터로 전달 (직렬화 불가)
    - CONDITION: {"expression": str}  # Python eval-safe 조건식

    config 내 값에 {context.key} 패턴 사용 시 런타임에 context.data[key]로 치환
    """

    name: str = Field(min_length=1, max_length=100, description="스텝 이름 (고유 식별)")
    step_type: StepType = Field(description="스텝 유형")
    config: dict[str, Any] = Field(default_factory=dict, description="스텝 타입별 설정")
    output_key: str = Field(
        default="",
        max_length=100,
        description="실행 결과를 저장할 context.data 키 (빈 문자열이면 저장 안 함)",
    )
    on_error: OnError = Field(default=OnError.ABORT, description="실패 시 정책")
    when: str = Field(
        default="",
        description="조건부 실행: Python bool 표현식 (빈 문자열이면 항상 실행)",
    )
    description: str = Field(default="", description="스텝 설명 (로깅/디버깅용)")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """스텝 이름: 영문/숫자/언더스코어/하이픈만"""
        import re

        v = v.strip()
        if not re.match(r"^[a-z0-9][a-z0-9_-]*$", v):
            raise ValueError(
                f"유효하지 않은 스텝 이름: '{v}'. "
                "영문 소문자/숫자/언더스코어/하이픈만 허용"
            )
        return v


class WorkflowDefinition(BaseModel):
    """워크플로우 전체 정의

    워크플로우는 steps 순서대로 실행되며
    condition 스텝으로 분기 제어 가능
    """

    workflow_id: str = Field(min_length=1, max_length=100, description="고유 워크플로우 ID")
    name: str = Field(min_length=1, max_length=200, description="워크플로우 이름")
    description: str = Field(default="", description="워크플로우 설명")
    steps: list[WorkflowStep] = Field(min_length=1, description="실행 스텝 목록 (순서대로)")
    scope: str = Field(default="global", description="메모리/이벤트 스코프")
    category: str = Field(
        default="general",
        description="카테고리: marketing / admin / general",
    )
    notify_on_complete: bool = Field(default=True, description="완료 시 텔레그램 알림")
    notify_on_failure: bool = Field(default=True, description="실패 시 텔레그램 알림")

    @field_validator("workflow_id")
    @classmethod
    def validate_workflow_id(cls, v: str) -> str:
        import re

        v = v.strip()
        if not re.match(r"^[a-z0-9][a-z0-9_-]*$", v):
            raise ValueError(
                f"유효하지 않은 workflow_id: '{v}'. "
                "영문 소문자/숫자/언더스코어/하이픈만 허용"
            )
        return v

    @field_validator("steps")
    @classmethod
    def validate_unique_step_names(cls, v: list[WorkflowStep]) -> list[WorkflowStep]:
        """스텝 이름 중복 방지"""
        names = [s.name for s in v]
        dupes = [n for n in names if names.count(n) > 1]
        if dupes:
            raise ValueError(f"스텝 이름 중복: {set(dupes)}")
        return v


class WorkflowContext:
    """워크플로우 실행 컨텍스트 (스텝 간 데이터 공유)

    data: 스텝 실행 결과 + 초기 데이터
    meta: 워크플로우 메타데이터 (읽기 전용)
    """

    def __init__(
        self,
        workflow_id: str,
        initial_data: dict[str, Any] | None = None,
    ) -> None:
        self.workflow_id = workflow_id
        self.data: dict[str, Any] = dict(initial_data or {})
        self.started_at = datetime.now(timezone.utc)
        self._step_results: list[StepResult] = []

    def set(self, key: str, value: Any) -> None:
        """컨텍스트에 데이터 저장"""
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """컨텍스트에서 데이터 조회"""
        return self.data.get(key, default)

    def add_step_result(self, result: StepResult) -> None:
        """스텝 결과 기록"""
        self._step_results.append(result)

    @property
    def step_results(self) -> list[StepResult]:
        return list(self._step_results)

    @property
    def elapsed_ms(self) -> float:
        """경과 시간 (밀리초)"""
        delta = datetime.now(timezone.utc) - self.started_at
        return delta.total_seconds() * 1000

    def resolve_value(self, value: Any) -> Any:
        """값 내 {context.key} 패턴을 context.data[key]로 치환

        예: "{keywords}" → context.data["keywords"]
            "블로그: {title}" → "블로그: " + str(context.data["title"])
        """
        if isinstance(value, str):
            return self._resolve_string(value)
        if isinstance(value, dict):
            return {k: self.resolve_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve_value(item) for item in value]
        return value

    def _resolve_string(self, s: str) -> Any:
        """문자열 내 {key} 패턴 치환

        단일 패턴 ("{key}")이면 원본 타입 유지
        혼합 패턴 ("prefix {key} suffix")이면 str 반환
        """
        import re

        # 단일 패턴: 정확히 {key}만 있는 경우 → 원본 타입 반환
        single_match = re.fullmatch(r"\{([a-zA-Z0-9_.]+)\}", s)
        if single_match:
            key = single_match.group(1)
            return self._get_nested(key)

        # 혼합 패턴: {key}가 문자열 일부인 경우 → str 치환
        def replacer(m: re.Match) -> str:
            key = m.group(1)
            val = self._get_nested(key)
            return str(val) if val is not None else ""

        return re.sub(r"\{([a-zA-Z0-9_.]+)\}", replacer, s)

    def _get_nested(self, key: str) -> Any:
        """점 표기법 키 조회: "a.b.c" → data["a"]["b"]["c"]"""
        parts = key.split(".")
        current: Any = self.data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
            if current is None:
                return None
        return current


class StepResult(BaseModel):
    """단일 스텝 실행 결과"""

    step_name: str
    step_type: StepType
    success: bool
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    skipped: bool = False
    retry_count: int = 0

    model_config = {"arbitrary_types_allowed": True}


class WorkflowResult(BaseModel):
    """워크플로우 전체 실행 결과"""

    workflow_id: str
    status: WorkflowStatus
    steps_completed: int
    steps_total: int
    steps_skipped: int = 0
    duration_ms: float
    outputs: dict[str, Any] = Field(default_factory=dict)
    step_results: list[StepResult] = Field(default_factory=list)
    error: str | None = None

    model_config = {"arbitrary_types_allowed": True}

    @property
    def success(self) -> bool:
        return self.status == WorkflowStatus.COMPLETED

    def to_telegram(self) -> str:
        """텔레그램 알림용 요약 텍스트"""
        emoji = {"completed": "✅", "failed": "❌", "partial": "⚠️"}.get(
            self.status.value, "❓"
        )
        lines = [
            f"{emoji} *워크플로우 완료*",
            f"ID: `{self.workflow_id}`",
            f"상태: {self.status.value}",
            f"스텝: {self.steps_completed}/{self.steps_total}",
            f"시간: {self.duration_ms:.0f}ms",
        ]
        if self.steps_skipped > 0:
            lines.append(f"건너뜀: {self.steps_skipped}")
        if self.error:
            lines.append(f"에러: {self.error[:200]}")

        # 실패한 스텝 상세
        failed = [r for r in self.step_results if not r.success and not r.skipped]
        for r in failed[:3]:
            lines.append(f"  ❌ `{r.step_name}`: {(r.error or '')[:100]}")

        return "\n".join(lines)


# Type alias for FUNCTION step
WorkflowFunc = Callable[
    ["WorkflowContext"],
    Coroutine[Any, Any, Any],
]
