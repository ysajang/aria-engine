"""ARIA Engine - Workflow System Tests

워크플로우 인프라 테스트:
- types.py: 스키마 검증 + WorkflowContext
- templates.py: TemplateEngine 렌더링 + 필터
- runner.py: WorkflowRunner 실행 + 에러 처리
- registry.py: WorkflowRegistry 등록/조회/실행

실행: ARIA_ENV_FILE="" pytest tests/unit/test_workflows.py -v
"""

from __future__ import annotations

import pytest

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
from aria.workflows.templates import (
    TemplateEngine,
    _datetime_kst,
    _number_format,
    _severity_emoji,
    _truncate_smart,
    _usd,
)
from aria.workflows.runner import WorkflowRunner, _parse_literal
from aria.workflows.registry import WorkflowRegistry


# ============================================================
# Types — 스키마 검증
# ============================================================


class TestStepType:
    def test_all_types(self):
        assert StepType.TOOL_CALL.value == "tool_call"
        assert StepType.TEMPLATE.value == "template"
        assert StepType.LLM_CALL.value == "llm_call"
        assert StepType.FUNCTION.value == "function"
        assert StepType.CONDITION.value == "condition"


class TestOnError:
    def test_all_policies(self):
        assert OnError.ABORT.value == "abort"
        assert OnError.SKIP.value == "skip"
        assert OnError.RETRY.value == "retry"


class TestWorkflowStep:
    def test_valid_step(self):
        step = WorkflowStep(
            name="fetch-data",
            step_type=StepType.TOOL_CALL,
            config={"tool_name": "ddg_web_search", "args": {"query": "test"}},
            output_key="search_results",
        )
        assert step.name == "fetch-data"
        assert step.on_error == OnError.ABORT  # 기본값

    def test_invalid_name_uppercase(self):
        with pytest.raises(ValueError, match="유효하지 않은"):
            WorkflowStep(name="FetchData", step_type=StepType.TOOL_CALL)

    def test_invalid_name_spaces(self):
        with pytest.raises(ValueError, match="유효하지 않은"):
            WorkflowStep(name="fetch data", step_type=StepType.TOOL_CALL)

    def test_empty_name(self):
        with pytest.raises(ValueError):
            WorkflowStep(name="", step_type=StepType.TOOL_CALL)

    def test_with_when_condition(self):
        step = WorkflowStep(
            name="conditional-step",
            step_type=StepType.TEMPLATE,
            when="results is_not_empty",
        )
        assert step.when == "results is_not_empty"

    def test_default_output_key_empty(self):
        step = WorkflowStep(name="test-step", step_type=StepType.FUNCTION)
        assert step.output_key == ""


class TestWorkflowDefinition:
    def test_valid_definition(self):
        defn = WorkflowDefinition(
            workflow_id="competitor-monitor",
            name="경쟁사 모니터링",
            steps=[
                WorkflowStep(name="search", step_type=StepType.TOOL_CALL),
                WorkflowStep(name="report", step_type=StepType.TEMPLATE),
            ],
            category="marketing",
        )
        assert defn.workflow_id == "competitor-monitor"
        assert len(defn.steps) == 2
        assert defn.scope == "global"

    def test_invalid_workflow_id(self):
        with pytest.raises(ValueError, match="유효하지 않은"):
            WorkflowDefinition(
                workflow_id="My Workflow",
                name="test",
                steps=[WorkflowStep(name="s1", step_type=StepType.FUNCTION)],
            )

    def test_duplicate_step_names(self):
        with pytest.raises(ValueError, match="스텝 이름 중복"):
            WorkflowDefinition(
                workflow_id="test",
                name="test",
                steps=[
                    WorkflowStep(name="same-name", step_type=StepType.FUNCTION),
                    WorkflowStep(name="same-name", step_type=StepType.TEMPLATE),
                ],
            )

    def test_empty_steps(self):
        with pytest.raises(ValueError):
            WorkflowDefinition(
                workflow_id="test",
                name="test",
                steps=[],
            )


# ============================================================
# WorkflowContext — 데이터 공유 + 치환
# ============================================================


class TestWorkflowContext:
    def test_basic_set_get(self):
        ctx = WorkflowContext("test-wf")
        ctx.set("key", "value")
        assert ctx.get("key") == "value"

    def test_get_default(self):
        ctx = WorkflowContext("test-wf")
        assert ctx.get("missing", "default") == "default"

    def test_initial_data(self):
        ctx = WorkflowContext("test-wf", initial_data={"a": 1, "b": "hello"})
        assert ctx.get("a") == 1
        assert ctx.get("b") == "hello"

    def test_resolve_string_single(self):
        ctx = WorkflowContext("test-wf", initial_data={"name": "테스트"})
        assert ctx.resolve_value("{name}") == "테스트"

    def test_resolve_string_mixed(self):
        ctx = WorkflowContext("test-wf", initial_data={"count": 5})
        assert ctx.resolve_value("결과: {count}건") == "결과: 5건"

    def test_resolve_dict(self):
        ctx = WorkflowContext("test-wf", initial_data={"q": "AI 트렌드"})
        result = ctx.resolve_value({"tool_name": "ddg_web_search", "args": {"query": "{q}"}})
        assert result["args"]["query"] == "AI 트렌드"

    def test_resolve_list(self):
        ctx = WorkflowContext("test-wf", initial_data={"a": "x", "b": "y"})
        result = ctx.resolve_value(["{a}", "{b}"])
        assert result == ["x", "y"]

    def test_resolve_nested_key(self):
        ctx = WorkflowContext("test-wf", initial_data={"result": {"data": {"value": 42}}})
        assert ctx.resolve_value("{result.data.value}") == 42

    def test_resolve_missing_key_returns_empty(self):
        ctx = WorkflowContext("test-wf")
        assert ctx.resolve_value("prefix {missing} suffix") == "prefix  suffix"

    def test_resolve_preserves_type_single(self):
        """단일 {key} 패턴은 원본 타입 유지"""
        ctx = WorkflowContext("test-wf", initial_data={"items": [1, 2, 3]})
        result = ctx.resolve_value("{items}")
        assert isinstance(result, list)
        assert result == [1, 2, 3]

    def test_elapsed_ms(self):
        ctx = WorkflowContext("test-wf")
        assert ctx.elapsed_ms >= 0

    def test_step_results(self):
        ctx = WorkflowContext("test-wf")
        r = StepResult(step_name="s1", step_type=StepType.FUNCTION, success=True)
        ctx.add_step_result(r)
        assert len(ctx.step_results) == 1
        assert ctx.step_results[0].step_name == "s1"


# ============================================================
# TemplateEngine — 렌더링 + 필터
# ============================================================


class TestTemplateFilters:
    def test_datetime_kst_string(self):
        result = _datetime_kst("2026-05-06T00:00:00+00:00")
        assert "2026-05-06 09:00" == result  # UTC+9

    def test_datetime_kst_invalid(self):
        assert _datetime_kst("invalid") == "invalid"

    def test_number_format_int(self):
        assert _number_format(1234567) == "1,234,567"

    def test_number_format_float(self):
        assert _number_format(1234.5, 2) == "1,234.50"

    def test_usd(self):
        assert _usd(12.3456) == "$12.3456"
        assert _usd(0, 2) == "$0.00"

    def test_truncate_smart_short(self):
        assert _truncate_smart("짧은 텍스트", 100) == "짧은 텍스트"

    def test_truncate_smart_long(self):
        result = _truncate_smart("a" * 200, 100)
        assert len(result) == 100
        assert result.endswith("...")

    def test_severity_emoji(self):
        assert _severity_emoji("info") == "ℹ️"
        assert _severity_emoji("error") == "🔴"
        assert _severity_emoji("unknown") == "❓"


class TestTemplateEngine:
    def test_render_builtin_template(self):
        engine = TemplateEngine("/tmp/nonexistent")
        result = engine.render("admin/schedule_confirm.md.j2", {
            "title": "미팅",
            "start": "2026-05-06T09:00:00+00:00",
        })
        assert "미팅" in result
        assert "일정 등록 완료" in result

    def test_render_string(self):
        engine = TemplateEngine("/tmp/nonexistent")
        result = engine.render_string(
            "안녕 {{ name }}! {{ count }}건 처리됨",
            {"name": "승재", "count": 5},
        )
        assert result == "안녕 승재! 5건 처리됨"

    def test_render_with_filters(self):
        engine = TemplateEngine("/tmp/nonexistent")
        result = engine.render_string(
            "금액: {{ amount | number_format }}원",
            {"amount": 50000},
        )
        assert result == "금액: 50,000원"

    def test_has_builtin_template(self):
        engine = TemplateEngine("/tmp/nonexistent")
        assert engine.has_template("marketing/competitor_report.md.j2")
        assert engine.has_template("admin/invoice.md.j2")
        assert not engine.has_template("nonexistent.md.j2")

    def test_list_templates(self):
        engine = TemplateEngine("/tmp/nonexistent")
        templates = engine.list_templates()
        assert len(templates) > 0
        assert "admin/kpi_briefing.md.j2" in templates

    def test_render_competitor_report(self):
        engine = TemplateEngine("/tmp/nonexistent")
        result = engine.render("marketing/competitor_report.md.j2", {
            "now": "2026-05-06T00:00:00+00:00",
            "changes": [
                {
                    "competitor": "CompanyA",
                    "summary": "새로운 기능 출시",
                    "source": "naver_news",
                    "detected_at": "2026-05-06T00:00:00+00:00",
                },
            ],
        })
        assert "CompanyA" in result
        assert "새로운 기능 출시" in result

    def test_render_competitor_report_empty(self):
        engine = TemplateEngine("/tmp/nonexistent")
        result = engine.render("marketing/competitor_report.md.j2", {
            "now": "2026-05-06T00:00:00+00:00",
            "changes": [],
        })
        assert "변화 감지 없음" in result

    def test_render_invoice(self):
        engine = TemplateEngine("/tmp/nonexistent")
        result = engine.render("admin/invoice.md.j2", {
            "issue_date": "2026-05-06T00:00:00+00:00",
            "invoice_number": "INV-2026-001",
            "issuer": {
                "name": "YSajang",
                "business_number": "123-45-67890",
                "address": "경기도 남양주시",
            },
            "recipient": {"name": "고객사", "email": "client@example.com"},
            "items": [
                {"description": "웹 개발", "quantity": 1, "unit_price": 500000, "total": 500000},
            ],
            "total": 500000,
            "tax": 50000,
            "grand_total": 550000,
            "payment_info": "카카오뱅크 3333-00-1234567",
        })
        assert "INV-2026-001" in result
        assert "500,000" in result

    def test_render_undefined_variable_raises(self):
        engine = TemplateEngine("/tmp/nonexistent")
        with pytest.raises(Exception):  # jinja2.UndefinedError
            engine.render_string("{{ undefined_var }}", {})


# ============================================================
# WorkflowRunner — 실행 엔진
# ============================================================


class TestParseLiteral:
    def test_string_quoted(self):
        assert _parse_literal('"hello"') == "hello"
        assert _parse_literal("'world'") == "world"

    def test_int(self):
        assert _parse_literal("42") == 42

    def test_float(self):
        assert _parse_literal("3.14") == 3.14

    def test_bool(self):
        assert _parse_literal("true") is True
        assert _parse_literal("false") is False

    def test_none(self):
        assert _parse_literal("none") is None
        assert _parse_literal("null") is None

    def test_raw_string(self):
        assert _parse_literal("hello") == "hello"


class TestSafeEval:
    def test_true_false_constants(self):
        assert WorkflowRunner._safe_eval("true", {}) is True
        assert WorkflowRunner._safe_eval("false", {}) is False

    def test_empty_expression(self):
        assert WorkflowRunner._safe_eval("", {}) is True

    def test_exists(self):
        assert WorkflowRunner._safe_eval("key exists", {"key": "val"}) is True
        assert WorkflowRunner._safe_eval("key exists", {}) is False
        assert WorkflowRunner._safe_eval("key exists", {"key": None}) is False

    def test_is_empty(self):
        assert WorkflowRunner._safe_eval("items is_empty", {"items": []}) is True
        assert WorkflowRunner._safe_eval("items is_empty", {"items": [1]}) is False
        assert WorkflowRunner._safe_eval("text is_empty", {"text": ""}) is True
        assert WorkflowRunner._safe_eval("missing is_empty", {}) is True

    def test_is_not_empty(self):
        assert WorkflowRunner._safe_eval("items is_not_empty", {"items": [1]}) is True
        assert WorkflowRunner._safe_eval("items is_not_empty", {"items": []}) is False

    def test_equals(self):
        assert WorkflowRunner._safe_eval("status == completed", {"status": "completed"}) is True
        assert WorkflowRunner._safe_eval("count == 5", {"count": 5}) is True
        assert WorkflowRunner._safe_eval("count == 3", {"count": 5}) is False

    def test_not_equals(self):
        assert WorkflowRunner._safe_eval("status != failed", {"status": "ok"}) is True

    def test_greater_than(self):
        assert WorkflowRunner._safe_eval("count > 3", {"count": 5}) is True
        assert WorkflowRunner._safe_eval("count > 10", {"count": 5}) is False

    def test_less_than(self):
        assert WorkflowRunner._safe_eval("count < 10", {"count": 5}) is True

    def test_contains(self):
        assert WorkflowRunner._safe_eval("text contains hello", {"text": "hello world"}) is True
        assert WorkflowRunner._safe_eval("text contains bye", {"text": "hello"}) is False

    def test_not_prefix(self):
        assert WorkflowRunner._safe_eval("not key exists", {}) is True
        assert WorkflowRunner._safe_eval("not key exists", {"key": "val"}) is False

    def test_missing_key_returns_false(self):
        assert WorkflowRunner._safe_eval("missing == 5", {}) is False


class TestWorkflowRunnerTemplateStep:
    @pytest.fixture
    def runner(self):
        engine = TemplateEngine("/tmp/nonexistent")
        return WorkflowRunner(template_engine=engine)

    @pytest.mark.asyncio
    async def test_template_step(self, runner):
        defn = WorkflowDefinition(
            workflow_id="test-template",
            name="템플릿 테스트",
            steps=[
                WorkflowStep(
                    name="render",
                    step_type=StepType.TEMPLATE,
                    config={
                        "template_string": "안녕 {{ name }}!",
                        "vars": {"name": "승재"},
                    },
                    output_key="greeting",
                ),
            ],
            notify_on_complete=False,
        )
        result = await runner.run(defn)
        assert result.success
        assert result.outputs["greeting"] == "안녕 승재!"

    @pytest.mark.asyncio
    async def test_template_step_with_context_data(self, runner):
        defn = WorkflowDefinition(
            workflow_id="test-ctx",
            name="컨텍스트 테스트",
            steps=[
                WorkflowStep(
                    name="render",
                    step_type=StepType.TEMPLATE,
                    config={"template_string": "{{ item_count }}건 처리"},
                    output_key="msg",
                ),
            ],
            notify_on_complete=False,
        )
        result = await runner.run(defn, initial_data={"item_count": 10})
        assert result.outputs["msg"] == "10건 처리"


class TestWorkflowRunnerFunctionStep:
    @pytest.mark.asyncio
    async def test_function_step(self):
        async def my_func(ctx: WorkflowContext) -> dict:
            return {"computed": ctx.get("input_val", 0) * 2}

        runner = WorkflowRunner(functions={"double": my_func})

        defn = WorkflowDefinition(
            workflow_id="test-func",
            name="함수 테스트",
            steps=[
                WorkflowStep(
                    name="compute",
                    step_type=StepType.FUNCTION,
                    config={"func_name": "double"},
                    output_key="result",
                ),
            ],
            notify_on_complete=False,
        )
        result = await runner.run(defn, initial_data={"input_val": 21})
        assert result.success
        assert result.outputs["result"] == {"computed": 42}

    @pytest.mark.asyncio
    async def test_function_not_found(self):
        runner = WorkflowRunner()
        defn = WorkflowDefinition(
            workflow_id="test-missing",
            name="없는 함수",
            steps=[
                WorkflowStep(
                    name="bad",
                    step_type=StepType.FUNCTION,
                    config={"func_name": "nonexistent"},
                ),
            ],
            notify_on_complete=False,
            notify_on_failure=False,
        )
        result = await runner.run(defn)
        assert not result.success
        assert result.status == WorkflowStatus.FAILED


class TestWorkflowRunnerConditionStep:
    @pytest.mark.asyncio
    async def test_condition_step(self):
        runner = WorkflowRunner(template_engine=TemplateEngine("/tmp/nonexistent"))

        defn = WorkflowDefinition(
            workflow_id="test-cond",
            name="조건 테스트",
            steps=[
                WorkflowStep(
                    name="check",
                    step_type=StepType.CONDITION,
                    config={"expression": "count > 5"},
                    output_key="is_large",
                ),
                WorkflowStep(
                    name="render-if-large",
                    step_type=StepType.TEMPLATE,
                    config={"template_string": "큰 숫자: {{ count }}"},
                    when="is_large == True",
                    output_key="msg",
                ),
            ],
            notify_on_complete=False,
        )
        result = await runner.run(defn, initial_data={"count": 10})
        assert result.success
        assert result.outputs.get("msg") == "큰 숫자: 10"

    @pytest.mark.asyncio
    async def test_condition_skip(self):
        runner = WorkflowRunner(template_engine=TemplateEngine("/tmp/nonexistent"))

        defn = WorkflowDefinition(
            workflow_id="test-cond-skip",
            name="조건 스킵 테스트",
            steps=[
                WorkflowStep(
                    name="conditional",
                    step_type=StepType.TEMPLATE,
                    config={"template_string": "실행됨"},
                    when="flag exists",
                    output_key="msg",
                ),
            ],
            notify_on_complete=False,
        )
        # flag 없으므로 skip
        result = await runner.run(defn)
        assert result.status == WorkflowStatus.PARTIAL
        assert result.steps_skipped == 1
        assert "msg" not in result.outputs


class TestWorkflowRunnerErrorHandling:
    @pytest.mark.asyncio
    async def test_abort_on_error(self):
        async def failing_func(ctx):
            raise RuntimeError("의도적 실패")

        runner = WorkflowRunner(
            template_engine=TemplateEngine("/tmp/nonexistent"),
            functions={"fail": failing_func},
        )

        defn = WorkflowDefinition(
            workflow_id="test-abort",
            name="중단 테스트",
            steps=[
                WorkflowStep(
                    name="fail-step",
                    step_type=StepType.FUNCTION,
                    config={"func_name": "fail"},
                    on_error=OnError.ABORT,
                ),
                WorkflowStep(
                    name="never-reached",
                    step_type=StepType.TEMPLATE,
                    config={"template_string": "도달 불가"},
                ),
            ],
            notify_on_complete=False,
            notify_on_failure=False,
        )
        result = await runner.run(defn)
        assert result.status == WorkflowStatus.FAILED
        assert result.steps_completed == 0
        assert "의도적 실패" in result.error

    @pytest.mark.asyncio
    async def test_skip_on_error(self):
        async def failing_func(ctx):
            raise RuntimeError("실패")

        runner = WorkflowRunner(
            template_engine=TemplateEngine("/tmp/nonexistent"),
            functions={"fail": failing_func},
        )

        defn = WorkflowDefinition(
            workflow_id="test-skip",
            name="스킵 테스트",
            steps=[
                WorkflowStep(
                    name="fail-step",
                    step_type=StepType.FUNCTION,
                    config={"func_name": "fail"},
                    on_error=OnError.SKIP,
                ),
                WorkflowStep(
                    name="should-run",
                    step_type=StepType.TEMPLATE,
                    config={"template_string": "실행됨"},
                    output_key="msg",
                ),
            ],
            notify_on_complete=False,
        )
        result = await runner.run(defn)
        assert result.status == WorkflowStatus.PARTIAL
        assert result.steps_skipped == 1
        assert result.outputs.get("msg") == "실행됨"

    @pytest.mark.asyncio
    async def test_retry_on_error(self):
        call_count = 0

        async def sometimes_fail(ctx):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError(f"실패 {call_count}")
            return "성공"

        runner = WorkflowRunner(functions={"flaky": sometimes_fail})

        defn = WorkflowDefinition(
            workflow_id="test-retry",
            name="재시도 테스트",
            steps=[
                WorkflowStep(
                    name="flaky-step",
                    step_type=StepType.FUNCTION,
                    config={"func_name": "flaky"},
                    on_error=OnError.RETRY,
                    output_key="result",
                ),
            ],
            notify_on_complete=False,
        )
        result = await runner.run(defn)
        assert result.success
        assert call_count == 3


class TestWorkflowRunnerMultiStep:
    @pytest.mark.asyncio
    async def test_multi_step_pipeline(self):
        """다중 스텝: function → context 저장 → template 렌더링"""
        async def fetch_data(ctx):
            return {"items": ["A", "B", "C"], "count": 3}

        engine = TemplateEngine("/tmp/nonexistent")
        runner = WorkflowRunner(
            template_engine=engine,
            functions={"fetch": fetch_data},
        )

        defn = WorkflowDefinition(
            workflow_id="test-pipeline",
            name="파이프라인 테스트",
            steps=[
                WorkflowStep(
                    name="fetch",
                    step_type=StepType.FUNCTION,
                    config={"func_name": "fetch"},
                    output_key="data",
                ),
                WorkflowStep(
                    name="render",
                    step_type=StepType.TEMPLATE,
                    config={"template_string": "{{ data.count }}건 발견"},
                    output_key="report",
                ),
            ],
            notify_on_complete=False,
        )
        result = await runner.run(defn)
        assert result.success
        assert result.outputs["report"] == "3건 발견"
        assert result.steps_completed == 2

    @pytest.mark.asyncio
    async def test_context_value_resolution_in_tool_config(self):
        """이전 스텝 결과가 다음 스텝 config에 {key}로 치환되는지"""
        async def set_query(ctx):
            return "AI 트렌드 2026"

        runner = WorkflowRunner(
            template_engine=TemplateEngine("/tmp/nonexistent"),
            functions={"set_query": set_query},
        )

        defn = WorkflowDefinition(
            workflow_id="test-resolve",
            name="치환 테스트",
            steps=[
                WorkflowStep(
                    name="set-query",
                    step_type=StepType.FUNCTION,
                    config={"func_name": "set_query"},
                    output_key="query",
                ),
                WorkflowStep(
                    name="render",
                    step_type=StepType.TEMPLATE,
                    config={"template_string": "검색어: {{ query }}"},
                    output_key="msg",
                ),
            ],
            notify_on_complete=False,
        )
        result = await runner.run(defn)
        assert result.success
        assert result.outputs["msg"] == "검색어: AI 트렌드 2026"


# ============================================================
# WorkflowResult — 결과 + 알림
# ============================================================


class TestWorkflowResult:
    def test_success_property(self):
        r = WorkflowResult(
            workflow_id="test",
            status=WorkflowStatus.COMPLETED,
            steps_completed=2,
            steps_total=2,
            duration_ms=100,
        )
        assert r.success is True

    def test_failed_property(self):
        r = WorkflowResult(
            workflow_id="test",
            status=WorkflowStatus.FAILED,
            steps_completed=0,
            steps_total=2,
            duration_ms=100,
            error="something failed",
        )
        assert r.success is False

    def test_to_telegram(self):
        r = WorkflowResult(
            workflow_id="competitor-monitor",
            status=WorkflowStatus.COMPLETED,
            steps_completed=3,
            steps_total=3,
            duration_ms=1500,
        )
        text = r.to_telegram()
        assert "✅" in text
        assert "competitor-monitor" in text
        assert "3/3" in text

    def test_to_telegram_failed(self):
        r = WorkflowResult(
            workflow_id="test",
            status=WorkflowStatus.FAILED,
            steps_completed=1,
            steps_total=3,
            duration_ms=500,
            error="Step 2 failed",
            step_results=[
                StepResult(
                    step_name="bad-step",
                    step_type=StepType.TOOL_CALL,
                    success=False,
                    error="도구 실행 실패",
                ),
            ],
        )
        text = r.to_telegram()
        assert "❌" in text
        assert "bad-step" in text


# ============================================================
# WorkflowRegistry — 등록/조회/실행
# ============================================================


class TestWorkflowRegistry:
    @pytest.fixture
    def registry(self):
        runner = WorkflowRunner(template_engine=TemplateEngine("/tmp/nonexistent"))
        return WorkflowRegistry(runner)

    def _make_defn(self, wid: str, category: str = "general") -> WorkflowDefinition:
        return WorkflowDefinition(
            workflow_id=wid,
            name=f"Test {wid}",
            steps=[WorkflowStep(name="s1", step_type=StepType.TEMPLATE, config={"template_string": "ok"})],
            category=category,
            notify_on_complete=False,
        )

    def test_register_and_get(self, registry):
        defn = self._make_defn("test-wf")
        registry.register(defn)
        assert registry.get("test-wf") is defn
        assert registry.count == 1

    def test_register_duplicate(self, registry):
        defn = self._make_defn("dup")
        registry.register(defn)
        with pytest.raises(ValueError, match="이미 등록된"):
            registry.register(defn)

    def test_unregister(self, registry):
        registry.register(self._make_defn("to-remove"))
        assert registry.unregister("to-remove") is True
        assert registry.get("to-remove") is None
        assert registry.unregister("nonexistent") is False

    def test_list_by_category(self, registry):
        registry.register(self._make_defn("mkt-1", "marketing"))
        registry.register(self._make_defn("mkt-2", "marketing"))
        registry.register(self._make_defn("adm-1", "admin"))

        mkt = registry.list_by_category("marketing")
        assert len(mkt) == 2
        adm = registry.list_by_category("admin")
        assert len(adm) == 1

    def test_list_ids(self, registry):
        registry.register(self._make_defn("a"))
        registry.register(self._make_defn("b"))
        assert sorted(registry.list_ids()) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_execute(self, registry):
        registry.register(self._make_defn("exec-test"))
        result = await registry.execute("exec-test")
        assert result.success

    @pytest.mark.asyncio
    async def test_execute_not_found(self, registry):
        with pytest.raises(ValueError, match="등록되지 않은"):
            await registry.execute("nonexistent")

    def test_to_help_text(self, registry):
        registry.register(self._make_defn("mkt-1", "marketing"))
        registry.register(self._make_defn("adm-1", "admin"))
        text = registry.to_help_text()
        assert "mkt-1" in text
        assert "adm-1" in text

    def test_to_help_text_filtered(self, registry):
        registry.register(self._make_defn("mkt-1", "marketing"))
        registry.register(self._make_defn("adm-1", "admin"))
        text = registry.to_help_text("marketing")
        assert "mkt-1" in text
        assert "adm-1" not in text

    def test_to_help_text_empty(self, registry):
        text = registry.to_help_text()
        assert "없습니다" in text
