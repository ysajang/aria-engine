"""ARIA Engine - Workflow Runner

워크플로우 오케스트레이터: 정의된 스텝을 순서대로 실행
- 스텝 간 context.data 공유
- {key} 패턴으로 이전 스텝 결과 참조
- 에러 처리 정책 (abort/skip/retry)
- EventStore 실행 기록
- 텔레그램 완료/실패 알림

LLM 비종속: TOOL_CALL / TEMPLATE / FUNCTION / CONDITION은 LLM 0
           LLM_CALL만 Haiku 사용 (최후 수단)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import structlog

from aria.workflows.types import (
    OnError,
    StepResult,
    StepType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowFunc,
    WorkflowResult,
    WorkflowStatus,
    WorkflowStep,
)

logger = structlog.get_logger()

MAX_RETRY = 2


class WorkflowRunner:
    """워크플로우 실행 엔진

    Args:
        tool_registry: ToolRegistry (TOOL_CALL 스텝용)
        template_engine: TemplateEngine (TEMPLATE 스텝용)
        llm_provider: LLMProvider (LLM_CALL 스텝용 — Haiku)
        event_store: EventStore (실행 기록 저장)
        notifier_func: 텔레그램 알림 함수 (async (str) -> None)
        functions: 이름→함수 매핑 (FUNCTION 스텝용)
    """

    def __init__(
        self,
        tool_registry: Any = None,
        template_engine: Any = None,
        llm_provider: Any = None,
        event_store: Any = None,
        notifier_func: Any = None,
        functions: dict[str, WorkflowFunc] | None = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._template_engine = template_engine
        self._llm_provider = llm_provider
        self._event_store = event_store
        self._notifier = notifier_func
        self._functions: dict[str, WorkflowFunc] = dict(functions or {})

    def register_function(self, name: str, func: WorkflowFunc) -> None:
        """FUNCTION 스텝에서 호출할 함수 등록"""
        self._functions[name] = func

    async def run(
        self,
        definition: WorkflowDefinition,
        initial_data: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        """워크플로우 실행

        Args:
            definition: 워크플로우 정의
            initial_data: 초기 컨텍스트 데이터

        Returns:
            WorkflowResult
        """
        ctx = WorkflowContext(
            workflow_id=definition.workflow_id,
            initial_data=initial_data,
        )

        logger.info(
            "workflow_started",
            workflow_id=definition.workflow_id,
            steps=len(definition.steps),
        )

        steps_completed = 0
        steps_skipped = 0
        final_error: str | None = None

        for step in definition.steps:
            # 조건부 실행 평가
            if step.when and not self._evaluate_condition(step.when, ctx):
                skip_result = StepResult(
                    step_name=step.name,
                    step_type=step.step_type,
                    success=True,
                    skipped=True,
                )
                ctx.add_step_result(skip_result)
                steps_skipped += 1
                logger.debug("workflow_step_skipped", step=step.name, condition=step.when)
                continue

            # 스텝 실행 (재시도 포함)
            result = await self._execute_with_retry(step, ctx)
            ctx.add_step_result(result)

            if result.success:
                steps_completed += 1
                # output_key가 있으면 context에 저장
                if step.output_key and result.output is not None:
                    ctx.set(step.output_key, result.output)
            elif result.skipped:
                steps_skipped += 1
            else:
                # 실패 처리
                if step.on_error == OnError.ABORT:
                    final_error = f"Step '{step.name}' failed: {result.error}"
                    logger.error(
                        "workflow_aborted",
                        workflow_id=definition.workflow_id,
                        step=step.name,
                        error=result.error,
                    )
                    break
                elif step.on_error == OnError.SKIP:
                    steps_skipped += 1
                    logger.warning(
                        "workflow_step_skipped_on_error",
                        step=step.name,
                        error=result.error,
                    )
                # RETRY는 _execute_with_retry에서 처리 후 여기서는 abort

        # 상태 결정
        total = len(definition.steps)
        if final_error:
            status = WorkflowStatus.FAILED
        elif steps_skipped > 0:
            status = WorkflowStatus.PARTIAL
        else:
            status = WorkflowStatus.COMPLETED

        workflow_result = WorkflowResult(
            workflow_id=definition.workflow_id,
            status=status,
            steps_completed=steps_completed,
            steps_total=total,
            steps_skipped=steps_skipped,
            duration_ms=ctx.elapsed_ms,
            outputs=dict(ctx.data),
            step_results=ctx.step_results,
            error=final_error,
        )

        logger.info(
            "workflow_finished",
            workflow_id=definition.workflow_id,
            status=status.value,
            steps=f"{steps_completed}/{total}",
            duration_ms=f"{ctx.elapsed_ms:.0f}",
        )

        # 이벤트 저장 (비차단)
        await self._record_event(definition, workflow_result)

        # 텔레그램 알림 (비차단)
        await self._notify(definition, workflow_result)

        return workflow_result

    # === 스텝 실행 ===

    async def _execute_with_retry(
        self, step: WorkflowStep, ctx: WorkflowContext,
    ) -> StepResult:
        """스텝 실행 + 재시도 정책"""
        max_attempts = MAX_RETRY + 1 if step.on_error == OnError.RETRY else 1

        last_result: StepResult | None = None
        for attempt in range(max_attempts):
            result = await self._execute_step(step, ctx)
            result.retry_count = attempt

            if result.success:
                return result

            last_result = result
            if attempt < max_attempts - 1:
                logger.warning(
                    "workflow_step_retry",
                    step=step.name,
                    attempt=attempt + 1,
                    error=result.error,
                )

        # 모든 재시도 실패
        return last_result or StepResult(
            step_name=step.name,
            step_type=step.step_type,
            success=False,
            error="Unknown error",
        )

    async def _execute_step(
        self, step: WorkflowStep, ctx: WorkflowContext,
    ) -> StepResult:
        """단일 스텝 실행 (타입별 분기)"""
        start = time.monotonic()

        try:
            # config 내 {key} 패턴 치환
            resolved_config = ctx.resolve_value(step.config)

            if step.step_type == StepType.TOOL_CALL:
                output = await self._run_tool_call(resolved_config)
            elif step.step_type == StepType.TEMPLATE:
                output = self._run_template(resolved_config, ctx)
            elif step.step_type == StepType.LLM_CALL:
                output = await self._run_llm_call(resolved_config)
            elif step.step_type == StepType.FUNCTION:
                output = await self._run_function(resolved_config, ctx)
            elif step.step_type == StepType.CONDITION:
                output = self._run_condition(resolved_config, ctx)
            else:
                raise ValueError(f"알 수 없는 스텝 타입: {step.step_type}")

            duration_ms = (time.monotonic() - start) * 1000
            logger.debug(
                "workflow_step_completed",
                step=step.name,
                type=step.step_type.value,
                duration_ms=f"{duration_ms:.0f}",
            )

            return StepResult(
                step_name=step.name,
                step_type=step.step_type,
                success=True,
                output=output,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            logger.error(
                "workflow_step_failed",
                step=step.name,
                type=step.step_type.value,
                error=str(e)[:300],
            )
            return StepResult(
                step_name=step.name,
                step_type=step.step_type,
                success=False,
                error=str(e)[:500],
                duration_ms=duration_ms,
            )

    # === 스텝 타입별 실행기 ===

    async def _run_tool_call(self, config: dict[str, Any]) -> Any:
        """TOOL_CALL: ToolRegistry.execute() 호출

        config: {"tool_name": str, "args": dict}
        """
        if not self._tool_registry:
            raise RuntimeError("ToolRegistry가 설정되지 않았습니다")

        tool_name = config.get("tool_name")
        if not tool_name:
            raise ValueError("tool_name이 필요합니다")

        args = config.get("args", {})

        result = await self._tool_registry.execute(
            tool_name=tool_name,
            arguments=args,
            context="workflow_automation",
        )

        if result.success:
            return result.output
        raise RuntimeError(f"도구 실행 실패: {result.error}")

    def _run_template(self, config: dict[str, Any], ctx: WorkflowContext) -> str:
        """TEMPLATE: TemplateEngine 렌더링

        config: {"template_name": str} 또는 {"template_string": str}
        추가 변수: {"context_keys": ["key1", "key2"]} — ctx.data에서 추출
        """
        if not self._template_engine:
            raise RuntimeError("TemplateEngine이 설정되지 않았습니다")

        # 템플릿 변수 구성: config에서 직접 전달 + context.data에서 추출
        template_vars: dict[str, Any] = {}

        # context.data 전체를 기본으로 사용
        template_vars.update(ctx.data)

        # config의 vars 덮어쓰기
        extra_vars = config.get("vars", {})
        template_vars.update(extra_vars)

        # 현재 시각 자동 주입
        template_vars["now"] = datetime.now(timezone.utc).isoformat()

        if "template_name" in config:
            return self._template_engine.render(config["template_name"], template_vars)
        elif "template_string" in config:
            return self._template_engine.render_string(config["template_string"], template_vars)
        else:
            raise ValueError("template_name 또는 template_string이 필요합니다")

    async def _run_llm_call(self, config: dict[str, Any]) -> str:
        """LLM_CALL: LLMProvider.complete() — Haiku 우선

        config: {"prompt": str, "system": str?, "model": str?}

        주의: LLM은 최후 수단. 규칙/템플릿으로 안 되는 경우만 사용.
        """
        if not self._llm_provider:
            raise RuntimeError("LLMProvider가 설정되지 않았습니다")

        prompt = config.get("prompt")
        if not prompt:
            raise ValueError("prompt가 필요합니다")

        system = config.get("system", "")
        # 기본 cheap 모델 (Haiku) 사용 — 워크플로우에서 Sonnet/Opus 금지
        model = config.get("model") or "cheap"

        result = await self._llm_provider.complete(
            user_prompt=prompt,
            system_prompt=system or None,
            use_model=model,
        )

        return result.get("content", "")

    async def _run_function(self, config: dict[str, Any], ctx: WorkflowContext) -> Any:
        """FUNCTION: 등록된 커스텀 async callable 호출

        config: {"func_name": str}
        """
        func_name = config.get("func_name")
        if not func_name:
            raise ValueError("func_name이 필요합니다")

        func = self._functions.get(func_name)
        if not func:
            raise ValueError(
                f"등록되지 않은 함수: '{func_name}'. "
                f"사용 가능: {list(self._functions.keys())}"
            )

        return await func(ctx)

    def _run_condition(self, config: dict[str, Any], ctx: WorkflowContext) -> bool:
        """CONDITION: 규칙 기반 조건 평가 → context에 결과 저장

        config: {"expression": str}
        expression 내에서 사용 가능한 변수: ctx.data의 모든 키

        보안: eval 대신 안전한 비교 연산만 허용
        """
        expression = config.get("expression", "")
        if not expression:
            raise ValueError("expression이 필요합니다")

        return self._safe_eval(expression, ctx.data)

    # === 조건 평가 (안전한 규칙 기반) ===

    @staticmethod
    def _evaluate_condition(expression: str, ctx: WorkflowContext) -> bool:
        """when 필드 조건 평가 (안전한 규칙 기반)"""
        return WorkflowRunner._safe_eval(expression, ctx.data)

    @staticmethod
    def _safe_eval(expression: str, data: dict[str, Any]) -> bool:
        """안전한 조건 평가

        허용 패턴:
        - "key exists" → key in data
        - "key == value" / "key != value"
        - "key > value" / "key >= value" / "key < value" / "key <= value"
        - "key contains value" → value in data[key]
        - "key is_empty" / "key is_not_empty"
        - "not expr" → 부정
        - "true" / "false" → 상수

        eval() 절대 사용 안 함 (보안)
        """
        expr = expression.strip()

        if not expr:
            return True

        # 상수
        if expr.lower() == "true":
            return True
        if expr.lower() == "false":
            return False

        # not 접두사
        if expr.lower().startswith("not "):
            inner = expr[4:].strip()
            return not WorkflowRunner._safe_eval(inner, data)

        # "key exists"
        if expr.endswith(" exists"):
            key = expr[:-7].strip()
            return key in data and data[key] is not None

        # "key is_empty"
        if expr.endswith(" is_empty"):
            key = expr[:-9].strip()
            val = data.get(key)
            if val is None:
                return True
            if isinstance(val, (str, list, dict)):
                return len(val) == 0
            return False

        # "key is_not_empty"
        if expr.endswith(" is_not_empty"):
            key = expr[:-13].strip()
            val = data.get(key)
            if val is None:
                return False
            if isinstance(val, (str, list, dict)):
                return len(val) > 0
            return True

        # 비교 연산자
        for op in ["==", "!=", ">=", "<=", ">", "<", " contains "]:
            if op in expr:
                parts = expr.split(op, 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    raw_value = parts[1].strip()

                    left = data.get(key)
                    right = _parse_literal(raw_value)

                    if left is None:
                        return False

                    if op == "==":
                        return left == right
                    elif op == "!=":
                        return left != right
                    elif op == ">":
                        return float(left) > float(right)
                    elif op == ">=":
                        return float(left) >= float(right)
                    elif op == "<":
                        return float(left) < float(right)
                    elif op == "<=":
                        return float(left) <= float(right)
                    elif op.strip() == "contains":
                        return str(right) in str(left)

        logger.warning("workflow_condition_unparseable", expression=expression)
        return False

    # === 이벤트 기록 + 알림 ===

    async def _record_event(
        self, definition: WorkflowDefinition, result: WorkflowResult,
    ) -> None:
        """워크플로우 실행 결과를 EventStore에 기록"""
        if not self._event_store:
            return

        try:
            from aria.events.types import EventInput, EventSeverity

            severity = EventSeverity.INFO if result.success else EventSeverity.ERROR
            event = EventInput(
                event_type="workflow_execution",
                source="aria",
                severity=severity,
                data={
                    "workflow_id": definition.workflow_id,
                    "status": result.status.value,
                    "steps_completed": result.steps_completed,
                    "steps_total": result.steps_total,
                    "duration_ms": result.duration_ms,
                    "error": result.error,
                },
            )
            stored = event.to_event()
            await self._event_store.store(stored)
        except Exception as e:
            logger.error("workflow_event_record_failed", error=str(e)[:200])

    async def _notify(
        self, definition: WorkflowDefinition, result: WorkflowResult,
    ) -> None:
        """텔레그램 알림 전송"""
        if not self._notifier:
            return

        should_notify = (
            (result.success and definition.notify_on_complete)
            or (not result.success and definition.notify_on_failure)
        )

        if not should_notify:
            return

        try:
            text = result.to_telegram()
            await self._notifier(text)
        except Exception as e:
            logger.error("workflow_notify_failed", error=str(e)[:200])


def _parse_literal(raw: str) -> Any:
    """문자열 리터럴 → Python 값 변환 (안전)"""
    raw = raw.strip()

    # 따옴표 제거
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        return raw[1:-1]

    # boolean
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False

    # null
    if raw.lower() in ("none", "null"):
        return None

    # 숫자
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        pass

    # 그 외 문자열 그대로
    return raw
