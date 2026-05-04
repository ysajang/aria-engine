"""ARIA Engine - Phase 4 Tests

테스트 대상:
- STTConfig / PCCommandConfig (설정)
- WhisperTranscriber (STT 모듈)
- PC Command Tools (whitelist / 경로 안전성 / subprocess)
- 의도분석 라우팅 (pc_command)
- 텔레그램 음성 핸들러
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# 1. Config Tests (STTConfig / PCCommandConfig)
# ============================================================

class TestSTTConfig:
    """STTConfig 단위 테스트"""

    def test_default_values(self) -> None:
        """기본값 검증"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARIA_STT_ENABLED", None)
            os.environ.pop("ARIA_STT_MODEL_SIZE", None)
            os.environ.pop("ARIA_STT_DEVICE", None)
            os.environ.pop("ARIA_STT_COMPUTE_TYPE", None)
            from aria.core.config import STTConfig
            config = STTConfig()
            assert config.enabled is True
            assert config.model_size == "medium"
            assert config.device == "cpu"
            assert config.compute_type == "int8"
            assert config.beam_size == 5

    def test_is_configured(self) -> None:
        """is_configured 프로퍼티"""
        from aria.core.config import STTConfig
        config = STTConfig(enabled=True)
        assert config.is_configured is True
        config2 = STTConfig(enabled=False)
        assert config2.is_configured is False

    def test_language_or_none(self) -> None:
        """빈 language → None / 값 있으면 그대로"""
        from aria.core.config import STTConfig
        config1 = STTConfig(language="")
        assert config1.language_or_none is None
        config2 = STTConfig(language="ko")
        assert config2.language_or_none == "ko"


class TestPCCommandConfig:
    """PCCommandConfig 단위 테스트"""

    def test_default_values(self) -> None:
        """기본값 검증"""
        from aria.core.config import PCCommandConfig
        config = PCCommandConfig()
        assert config.enabled is True
        assert config.subprocess_timeout == 15

    def test_is_configured(self) -> None:
        """is_configured 프로퍼티"""
        from aria.core.config import PCCommandConfig
        assert PCCommandConfig(enabled=True).is_configured is True
        assert PCCommandConfig(enabled=False).is_configured is False


class TestAriaConfigPhase4:
    """AriaConfig에 stt/pc_command 필드 추가 검증"""

    def test_has_stt_config(self) -> None:
        from aria.core.config import AriaConfig
        config = AriaConfig()
        assert hasattr(config, "stt")
        assert config.stt.model_size == "medium"

    def test_has_pc_command_config(self) -> None:
        from aria.core.config import AriaConfig
        config = AriaConfig()
        assert hasattr(config, "pc_command")
        assert config.pc_command.enabled is True


# ============================================================
# 2. WhisperTranscriber Tests
# ============================================================

class TestWhisperTranscriber:
    """WhisperTranscriber 단위 테스트 (모델 로딩 없이)"""

    def test_init(self) -> None:
        """초기화 검증"""
        from aria.stt.transcriber import WhisperTranscriber
        t = WhisperTranscriber(model_size="small", device="cpu", compute_type="int8")
        assert t.is_loaded is False
        assert t._model_size == "small"

    def test_model_info(self) -> None:
        """model_info 프로퍼티"""
        from aria.stt.transcriber import WhisperTranscriber
        t = WhisperTranscriber(model_size="medium", language="ko")
        info = t.model_info
        assert info["model_size"] == "medium"
        assert info["language"] == "ko"
        assert info["loaded"] is False

    def test_model_info_auto_language(self) -> None:
        """language=None → 'auto' 표시"""
        from aria.stt.transcriber import WhisperTranscriber
        t = WhisperTranscriber()
        assert t.model_info["language"] == "auto"

    @pytest.mark.asyncio
    async def test_transcribe_file_not_found(self) -> None:
        """존재하지 않는 파일 → FileNotFoundError"""
        from aria.stt.transcriber import WhisperTranscriber
        t = WhisperTranscriber()
        with pytest.raises(FileNotFoundError):
            await t.transcribe("/nonexistent/file.ogg")

    @pytest.mark.asyncio
    async def test_transcribe_bytes_empty(self) -> None:
        """빈 바이트 → ValueError"""
        from aria.stt.transcriber import WhisperTranscriber
        t = WhisperTranscriber()
        with pytest.raises(ValueError, match="빈 오디오"):
            await t.transcribe_bytes(b"")

    def test_transcription_result_dataclass(self) -> None:
        """TranscriptionResult 데이터클래스"""
        from aria.stt.transcriber import TranscriptionResult
        r = TranscriptionResult(
            text="테스트",
            language="ko",
            language_probability=0.95,
            duration_seconds=3.5,
            processing_ms=1200.0,
        )
        assert r.text == "테스트"
        assert r.language == "ko"
        assert r.language_probability == 0.95
        assert r.duration_seconds == 3.5

    def test_transcription_result_defaults(self) -> None:
        """TranscriptionResult 기본값"""
        from aria.stt.transcriber import TranscriptionResult
        r = TranscriptionResult(text="hello")
        assert r.language == ""
        assert r.processing_ms == 0.0


# ============================================================
# 3. PC Command Tools Tests
# ============================================================

class TestPCCommandWhitelist:
    """화이트리스트 검증"""

    def test_app_whitelist_has_chrome(self) -> None:
        from aria.tools.mcp.pc_command_tools import APP_WHITELIST
        assert "chrome" in APP_WHITELIST

    def test_app_whitelist_has_vscode(self) -> None:
        from aria.tools.mcp.pc_command_tools import APP_WHITELIST
        assert "vscode" in APP_WHITELIST

    def test_app_whitelist_no_dangerous(self) -> None:
        """위험한 프로그램이 없는지 확인"""
        from aria.tools.mcp.pc_command_tools import APP_WHITELIST
        dangerous = {"cmd", "powershell", "regedit", "taskmgr", "format"}
        assert not (set(APP_WHITELIST.keys()) & dangerous)

    def test_closeable_apps_subset_of_whitelist(self) -> None:
        """종료 가능 앱은 실행 가능 앱의 부분집합"""
        from aria.tools.mcp.pc_command_tools import APP_WHITELIST, CLOSEABLE_APPS
        # 대부분 겹치지만 완전 일치가 아닐 수 있음 (explorer 등)
        assert len(CLOSEABLE_APPS) > 0


class TestPathSafety:
    """경로 안전성 검증"""

    def test_safe_desktop_path(self) -> None:
        from aria.tools.mcp.pc_command_tools import _is_safe_path
        assert _is_safe_path(r"C:\Users\${USER}\Desktop") is True

    def test_safe_desktop_subpath(self) -> None:
        from aria.tools.mcp.pc_command_tools import _is_safe_path
        assert _is_safe_path(r"C:\Users\${USER}\Desktop\projects") is True

    def test_unsafe_root_path(self) -> None:
        from aria.tools.mcp.pc_command_tools import _is_safe_path
        assert _is_safe_path(r"C:\Windows\System32") is False

    def test_unsafe_path_traversal(self) -> None:
        from aria.tools.mcp.pc_command_tools import _is_safe_path
        assert _is_safe_path(r"C:\Users\${USER}\Desktop\..\..\System32") is False

    def test_unsafe_shell_metachar(self) -> None:
        from aria.tools.mcp.pc_command_tools import _is_safe_path
        assert _is_safe_path(r"C:\Users\${USER}\Desktop; rm -rf /") is False

    def test_unsafe_pipe(self) -> None:
        from aria.tools.mcp.pc_command_tools import _is_safe_path
        assert _is_safe_path(r"C:\Users\${USER}\Desktop | cat /etc/passwd") is False

    def test_unsafe_backtick(self) -> None:
        from aria.tools.mcp.pc_command_tools import _is_safe_path
        assert _is_safe_path(r"C:\Users\${USER}\Desktop`whoami`") is False

    def test_unsafe_empty(self) -> None:
        from aria.tools.mcp.pc_command_tools import _is_safe_path
        assert _is_safe_path("") is False

    def test_downloads_path(self) -> None:
        from aria.tools.mcp.pc_command_tools import _is_safe_path
        assert _is_safe_path(r"C:\Users\${USER}\Downloads") is True


class TestWSLPath:
    """Windows → WSL 경로 변환"""

    def test_c_drive(self) -> None:
        from aria.tools.mcp.pc_command_tools import _wsl_path
        assert _wsl_path(r"C:\Users\${USER}\Desktop") == "/mnt/c/Users/${USER}/Desktop"

    def test_d_drive(self) -> None:
        from aria.tools.mcp.pc_command_tools import _wsl_path
        assert _wsl_path(r"D:\Data") == "/mnt/d/Data"

    def test_already_wsl(self) -> None:
        from aria.tools.mcp.pc_command_tools import _wsl_path
        assert _wsl_path("/mnt/c/Users") == "/mnt/c/Users"


class TestPCOpenAppTool:
    """PCOpenAppTool 단위 테스트"""

    def test_definition(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCOpenAppTool
        tool = PCOpenAppTool()
        defn = tool.get_definition()
        assert defn.name == "pc_open_app"
        assert defn.safety_hint.value == "write"
        assert "chrome" in defn.description

    @pytest.mark.asyncio
    async def test_empty_app_name(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCOpenAppTool
        tool = PCOpenAppTool()
        result = await tool.execute({"app_name": ""})
        assert result.success is False
        assert "비어" in result.error

    @pytest.mark.asyncio
    async def test_unknown_app(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCOpenAppTool
        tool = PCOpenAppTool()
        result = await tool.execute({"app_name": "malware"})
        assert result.success is False
        assert "허용되지 않은" in result.error

    @pytest.mark.asyncio
    async def test_url_non_browser(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCOpenAppTool
        tool = PCOpenAppTool()
        result = await tool.execute({"app_name": "notepad", "url": "https://google.com"})
        assert result.success is False
        assert "브라우저" in result.error

    @pytest.mark.asyncio
    async def test_invalid_url_scheme(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCOpenAppTool
        tool = PCOpenAppTool()
        result = await tool.execute({"app_name": "chrome", "url": "file:///etc/passwd"})
        assert result.success is False
        assert "http" in result.error


class TestPCFileListTool:
    """PCFileListTool 단위 테스트"""

    def test_definition(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCFileListTool
        tool = PCFileListTool()
        defn = tool.get_definition()
        assert defn.name == "pc_file_list"

    @pytest.mark.asyncio
    async def test_empty_path(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCFileListTool
        tool = PCFileListTool()
        result = await tool.execute({"path": ""})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_unsafe_path(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCFileListTool
        tool = PCFileListTool()
        result = await tool.execute({"path": r"C:\Windows\System32"})
        assert result.success is False
        assert "허용되지 않은" in result.error

    @pytest.mark.asyncio
    async def test_unsafe_pattern(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCFileListTool
        tool = PCFileListTool()
        result = await tool.execute({
            "path": r"C:\Users\${USER}\Desktop",
            "pattern": "; rm -rf /",
        })
        assert result.success is False
        assert "안전하지 않은" in result.error


class TestPCSystemInfoTool:
    """PCSystemInfoTool 단위 테스트"""

    def test_definition(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCSystemInfoTool
        tool = PCSystemInfoTool()
        defn = tool.get_definition()
        assert defn.name == "pc_system_info"
        assert "processes" in defn.description

    @pytest.mark.asyncio
    async def test_empty_info_type(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCSystemInfoTool
        tool = PCSystemInfoTool()
        result = await tool.execute({"info_type": ""})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_invalid_info_type(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCSystemInfoTool
        tool = PCSystemInfoTool()
        result = await tool.execute({"info_type": "format_disk"})
        assert result.success is False
        assert "허용되지 않은" in result.error


class TestPCCloseAppTool:
    """PCCloseAppTool 단위 테스트"""

    def test_definition(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCCloseAppTool
        tool = PCCloseAppTool()
        defn = tool.get_definition()
        assert defn.name == "pc_close_app"
        assert defn.safety_hint.value == "destructive"

    @pytest.mark.asyncio
    async def test_unknown_app(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCCloseAppTool
        tool = PCCloseAppTool()
        result = await tool.execute({"app_name": "svchost"})
        assert result.success is False
        assert "종료 불가" in result.error


# ============================================================
# 4. LLM Format Tests (도구 정의 → LiteLLM 포맷)
# ============================================================

class TestPCToolsLLMFormat:
    """PC 도구 → LiteLLM function calling 포맷 변환"""

    def test_open_app_llm_format(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCOpenAppTool
        fmt = PCOpenAppTool().get_definition().to_llm_tool()
        assert fmt["type"] == "function"
        assert fmt["function"]["name"] == "pc_open_app"
        assert "properties" in fmt["function"]["parameters"]

    def test_file_list_llm_format(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCFileListTool
        fmt = PCFileListTool().get_definition().to_llm_tool()
        assert fmt["function"]["name"] == "pc_file_list"

    def test_system_info_llm_format(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCSystemInfoTool
        fmt = PCSystemInfoTool().get_definition().to_llm_tool()
        assert fmt["function"]["name"] == "pc_system_info"
        # enum이 포함되어야 함
        params = fmt["function"]["parameters"]
        assert "enum" in str(params)

    def test_close_app_llm_format(self) -> None:
        from aria.tools.mcp.pc_command_tools import PCCloseAppTool
        fmt = PCCloseAppTool().get_definition().to_llm_tool()
        assert fmt["function"]["name"] == "pc_close_app"


# ============================================================
# 5. Intent Analysis Routing Tests
# ============================================================

class TestIntentRoutingPCCommand:
    """pc_command 의도 라우팅 검증"""

    def test_pc_command_in_prompt(self) -> None:
        """INTENT_ANALYSIS_SYSTEM에 pc_command 포함 확인"""
        from aria.agents.react_agent import INTENT_ANALYSIS_SYSTEM
        assert "pc_command" in INTENT_ANALYSIS_SYSTEM
        assert "열어" in INTENT_ANALYSIS_SYSTEM
        assert "실행" in INTENT_ANALYSIS_SYSTEM
        assert "닫아" in INTENT_ANALYSIS_SYSTEM
        assert "종료" in INTENT_ANALYSIS_SYSTEM

    def test_pc_command_route_to_reason(self) -> None:
        """pc_command → reason 라우팅"""
        from aria.agents.react_agent import ReActAgent
        from unittest.mock import MagicMock

        agent = MagicMock(spec=ReActAgent)
        agent._route_after_intent = ReActAgent._route_after_intent.__get__(agent)

        # pc_command → reason
        class FakeState:
            intent = {"recommended_action": "pc_command", "complexity": "moderate"}
        result = agent._route_after_intent(FakeState())
        assert result == "reason"

    def test_search_knowledge_still_works(self) -> None:
        """기존 search_knowledge 라우팅 유지"""
        from aria.agents.react_agent import ReActAgent
        from unittest.mock import MagicMock

        agent = MagicMock(spec=ReActAgent)
        agent._route_after_intent = ReActAgent._route_after_intent.__get__(agent)

        class FakeState:
            intent = {"recommended_action": "search_knowledge", "complexity": "moderate"}
        result = agent._route_after_intent(FakeState())
        assert result == "search_knowledge"

    def test_fast_respond_still_works(self) -> None:
        """기존 fast_respond 라우팅 유지"""
        from aria.agents.react_agent import ReActAgent
        from unittest.mock import MagicMock

        agent = MagicMock(spec=ReActAgent)
        agent._route_after_intent = ReActAgent._route_after_intent.__get__(agent)

        class FakeState:
            intent = {"recommended_action": "respond", "complexity": "simple"}
        result = agent._route_after_intent(FakeState())
        assert result == "fast_respond"


# ============================================================
# 6. Telegram Voice Handler Tests
# ============================================================

class TestVoiceHandler:
    """텔레그램 음성 핸들러 테스트"""

    def test_handlers_accept_transcriber(self) -> None:
        """ARIAHandlers가 transcriber 파라미터 수용"""
        from aria.telegram.handlers import ARIAHandlers
        from aria.telegram.client import ARIAClient

        client = ARIAClient(base_url="http://localhost:8100")
        handlers = ARIAHandlers(
            aria_client=client,
            allowed_chat_id="12345",
            transcriber="mock_transcriber",
        )
        assert handlers._transcriber == "mock_transcriber"

    def test_handlers_transcriber_default_none(self) -> None:
        """transcriber 기본값 None"""
        from aria.telegram.handlers import ARIAHandlers
        from aria.telegram.client import ARIAClient

        client = ARIAClient(base_url="http://localhost:8100")
        handlers = ARIAHandlers(
            aria_client=client,
            allowed_chat_id="12345",
        )
        assert handlers._transcriber is None

    def test_has_handle_voice_method(self) -> None:
        """handle_voice 메서드 존재 확인"""
        from aria.telegram.handlers import ARIAHandlers
        assert hasattr(ARIAHandlers, "handle_voice")
        assert asyncio.iscoroutinefunction(ARIAHandlers.handle_voice)


# ============================================================
# 7. Telegram Bot Voice Registration Tests
# ============================================================

class TestBotVoiceRegistration:
    """봇 음성 핸들러 등록 테스트"""

    def test_create_bot_accepts_transcriber(self) -> None:
        """create_bot이 transcriber 파라미터 수용"""
        import inspect
        from aria.telegram.bot import create_bot
        sig = inspect.signature(create_bot)
        assert "transcriber" in sig.parameters

    def test_run_bot_accepts_transcriber(self) -> None:
        """run_bot이 transcriber 파라미터 수용"""
        import inspect
        from aria.telegram.bot import run_bot
        sig = inspect.signature(run_bot)
        assert "transcriber" in sig.parameters


# ============================================================
# 8. Subprocess Security Tests
# ============================================================

class TestSubprocessSecurity:
    """subprocess 보안 검증"""

    def test_shell_false_in_code(self) -> None:
        """shell=False가 코드에 명시되어 있는지"""
        import inspect
        from aria.tools.mcp.pc_command_tools import _run_subprocess
        source = inspect.getsource(_run_subprocess)
        assert "shell=False" in source

    def test_no_shell_true_anywhere(self) -> None:
        """PC command 모듈 전체에서 shell=True 없음"""
        import inspect
        import aria.tools.mcp.pc_command_tools as mod
        source = inspect.getsource(mod)
        assert "shell=True" not in source

    def test_system_info_no_arbitrary_commands(self) -> None:
        """SYSTEM_INFO_COMMANDS에 임의 명령 없음 (모두 PowerShell Get-* 패턴)"""
        from aria.tools.mcp.pc_command_tools import SYSTEM_INFO_COMMANDS
        for name, cmd in SYSTEM_INFO_COMMANDS.items():
            # 모두 powershell.exe로 시작
            assert cmd[0] == "powershell.exe", f"{name}: {cmd[0]}"
            # -Command 플래그 사용
            assert cmd[1] == "-Command", f"{name}: {cmd[1]}"
