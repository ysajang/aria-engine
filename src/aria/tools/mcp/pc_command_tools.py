"""ARIA Engine - PC Command Tools (WSL Interop)

WSL에서 Windows 명령을 실행하는 도구 3종
- pc_open_app: 앱 열기 (whitelist 기반)
- pc_file_list: 파일/폴더 목록 조회
- pc_system_info: 시스템 정보 조회 (프로세스 / 디스크 / 네트워크)

안전장치:
- SafetyLevelHint.DESTRUCTIVE → 모든 명령 NEEDS_CONFIRMATION (HITL 강제)
- 화이트리스트 기반 앱 실행 (등록되지 않은 프로그램 차단)
- 파일 삭제/수정 명령 불가 (읽기 전용 도구만 제공)
- 경로 정규화 + 탈출 방지 (path traversal 차단)
- 셸 인젝션 방지 (shell=False 고정)

보안 원칙: 위험 명령(삭제/수정/이동/포맷 등)은 도구 자체를 제공하지 않음
→ 향후 필요 시 별도 DESTRUCTIVE 도구로 분리 + 이중 HITL 승인
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from typing import Any

import structlog

from aria.tools.tool_types import (
    SafetyLevelHint,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolResult,
)

logger = structlog.get_logger()


# ============================================================
# 화이트리스트 — 허용된 프로그램만 실행 가능
# ============================================================

# 이름(별칭) → 실제 실행 명령어 매핑
# cmd.exe /c start 으로 실행되므로 Windows 경로/이름 사용
APP_WHITELIST: dict[str, list[str]] = {
    # 브라우저
    "chrome": ["cmd.exe", "/c", "start", "chrome"],
    "edge": ["cmd.exe", "/c", "start", "msedge"],
    "firefox": ["cmd.exe", "/c", "start", "firefox"],
    # 개발 도구
    "vscode": ["cmd.exe", "/c", "start", "code"],
    "terminal": ["cmd.exe", "/c", "start", "wt"],  # Windows Terminal
    "notepad": ["cmd.exe", "/c", "start", "notepad"],
    # 생산성
    "explorer": ["explorer.exe", "."],
    "calculator": ["cmd.exe", "/c", "start", "calc"],
    "settings": ["cmd.exe", "/c", "start", "ms-settings:"],
    # 미디어
    "spotify": ["cmd.exe", "/c", "start", "spotify"],
    # 메신저
    "telegram": ["cmd.exe", "/c", "start", "telegram"],
    "kakaotalk": ["cmd.exe", "/c", "start", "", r"C:\Program Files (x86)\Kakao\KakaoTalk\KakaoTalk.exe"],
    "discord": ["cmd.exe", "/c", "start", "discord"],
    "slack": ["cmd.exe", "/c", "start", "slack"],
}


def _load_allowed_file_paths() -> list[str]:
    """환경변수에서 허용 경로 목록 로드

    ARIA_ALLOWED_FILE_PATHS: 쉼표(,) 구분 Windows 경로 목록
    예: C:\\Users\\John\\Desktop,C:\\Users\\John\\Documents
    """
    raw = os.environ.get("ARIA_ALLOWED_FILE_PATHS", "")
    if not raw.strip():
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


# 파일 접근 허용 경로 (환경변수 기반)
ALLOWED_FILE_PATHS: list[str] = _load_allowed_file_paths()

# 시스템 정보 허용 명령어
SYSTEM_INFO_COMMANDS: dict[str, list[str]] = {
    "processes": ["powershell.exe", "-Command",
                  "Get-Process | Sort-Object -Property CPU -Descending | Select-Object -First 15 Name, Id, CPU, WorkingSet64 | Format-Table -AutoSize"],
    "disk": ["powershell.exe", "-Command",
             "Get-PSDrive -PSProvider FileSystem | Select-Object Name, Used, Free, @{N='Total';E={$_.Used+$_.Free}} | Format-Table -AutoSize"],
    "network": ["powershell.exe", "-Command",
                "Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -ne '127.0.0.1'} | Select-Object InterfaceAlias, IPAddress | Format-Table -AutoSize"],
    "uptime": ["powershell.exe", "-Command",
               "(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime | Select-Object Days, Hours, Minutes | Format-List"],
    "battery": ["powershell.exe", "-Command",
                "Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining, BatteryStatus | Format-List"],
}

# subprocess 실행 시 최대 타임아웃 (초)
SUBPROCESS_TIMEOUT = 15


def _is_safe_path(path: str) -> bool:
    """경로 안전성 검증

    - 허용 경로 목록에 포함되는지 확인
    - path traversal (..) 차단
    - 셸 메타문자 차단
    """
    # 허용 경로 미설정 시 모든 접근 차단
    if not ALLOWED_FILE_PATHS:
        return False

    # path traversal 차단
    if ".." in path:
        return False

    # 셸 메타문자 차단
    dangerous_chars = set("|;&`$(){}[]!><\n\r")
    if any(c in path for c in dangerous_chars):
        return False

    # 허용 경로에 포함되는지 확인
    normalized = os.path.normpath(path).replace("/", "\\")
    for allowed in ALLOWED_FILE_PATHS:
        allowed_norm = os.path.normpath(allowed).replace("/", "\\")
        if normalized.startswith(allowed_norm):
            return True

    return False


def _wsl_path(win_path: str) -> str:
    """Windows 경로 → WSL 경로 변환

    예: C:\\Users\\User\\Desktop → /mnt/c/Users/User/Desktop
    """
    # 이미 WSL 경로면 그대로 반환
    if win_path.startswith("/"):
        return win_path

    # C:\path → /mnt/c/path
    match = re.match(r"^([A-Za-z]):\\(.*)$", win_path)
    if match:
        drive = match.group(1).lower()
        rest = match.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"

    return win_path


async def _run_subprocess(
    cmd: list[str],
    timeout: int = SUBPROCESS_TIMEOUT,
) -> tuple[bool, str]:
    """subprocess 비동기 실행 (thread pool)

    Args:
        cmd: 실행할 명령어 리스트
        timeout: 타임아웃 (초)

    Returns:
        (성공여부, 출력 또는 에러 메시지)
    """
    def _run() -> tuple[bool, str]:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,  # 셸 인젝션 방지 — 절대 True로 변경 금지
                encoding="utf-8",
                errors="replace",
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                error = result.stderr.strip() or f"exit code {result.returncode}"
                return False, error
            return True, output or "(실행 완료 — 출력 없음)"
        except subprocess.TimeoutExpired:
            return False, f"타임아웃 ({timeout}초 초과)"
        except FileNotFoundError as e:
            return False, f"명령어를 찾을 수 없음: {e}"
        except Exception as e:
            return False, f"실행 오류: {str(e)[:300]}"

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# ============================================================
# Tool 1: pc_open_app — 앱 열기
# ============================================================

class PCOpenAppTool(ToolExecutor):
    """PC 앱 열기 (whitelist 기반)

    텔레그램에서 "크롬 열어" → HITL 확인 → cmd.exe /c start chrome
    등록되지 않은 프로그램은 차단됨
    """

    def get_definition(self) -> ToolDefinition:
        available = ", ".join(sorted(APP_WHITELIST.keys()))
        return ToolDefinition(
            name="pc_open_app",
            description=(
                f"PC에서 프로그램을 실행합니다. "
                f"허용된 앱: {available}. "
                f"URL을 지정하면 브라우저에서 해당 URL을 엽니다."
            ),
            parameters=[
                ToolParameter(
                    name="app_name",
                    type="string",
                    description=f"실행할 앱 이름 ({available})",
                    required=True,
                ),
                ToolParameter(
                    name="url",
                    type="string",
                    description="브라우저로 열 URL (선택 — chrome/edge/firefox 전용)",
                    required=False,
                ),
            ],
            category=ToolCategory.CUSTOM,
            safety_hint=SafetyLevelHint.WRITE,  # HITL 확인 트리거
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        start = time.monotonic()
        app_name = parameters.get("app_name", "").strip().lower()
        url = parameters.get("url", "").strip()

        if not app_name:
            return ToolResult(
                tool_name="pc_open_app",
                success=False,
                error="app_name이 비어 있습니다",
            )

        # whitelist 검증
        if app_name not in APP_WHITELIST:
            available = ", ".join(sorted(APP_WHITELIST.keys()))
            return ToolResult(
                tool_name="pc_open_app",
                success=False,
                error=f"허용되지 않은 앱: '{app_name}'. 허용 목록: {available}",
            )

        cmd = list(APP_WHITELIST[app_name])  # 복사

        # URL이 있으면 브라우저 명령에 추가
        if url:
            browsers = {"chrome", "edge", "firefox"}
            if app_name not in browsers:
                return ToolResult(
                    tool_name="pc_open_app",
                    success=False,
                    error=f"URL은 브라우저 앱에서만 사용 가능합니다 ({', '.join(browsers)})",
                )
            # URL 안전성 검증 (기본)
            if not url.startswith(("http://", "https://")):
                return ToolResult(
                    tool_name="pc_open_app",
                    success=False,
                    error="URL은 http:// 또는 https://로 시작해야 합니다",
                )
            cmd.append(url)

        logger.info("pc_open_app", app=app_name, url=url or None, cmd=cmd)

        success, output = await _run_subprocess(cmd)
        elapsed = (time.monotonic() - start) * 1000

        return ToolResult(
            tool_name="pc_open_app",
            success=success,
            output=f"✅ {app_name} 실행됨" if success else None,
            error=output if not success else "",
            latency_ms=elapsed,
        )


# ============================================================
# Tool 2: pc_file_list — 파일/폴더 목록 (읽기 전용)
# ============================================================

class PCFileListTool(ToolExecutor):
    """PC 파일/폴더 목록 조회 (읽기 전용)

    허용된 경로 내에서만 동작 — path traversal 차단
    삭제/수정/이동 기능 없음 (읽기 전용)
    """

    def get_definition(self) -> ToolDefinition:
        paths = ", ".join(ALLOWED_FILE_PATHS) if ALLOWED_FILE_PATHS else "(ARIA_ALLOWED_FILE_PATHS 미설정)"
        return ToolDefinition(
            name="pc_file_list",
            description=(
                f"PC의 파일 및 폴더 목록을 조회합니다 (읽기 전용). "
                f"허용 경로: {paths}"
            ),
            parameters=[
                ToolParameter(
                    name="path",
                    type="string",
                    description="조회할 Windows 경로",
                    required=True,
                ),
                ToolParameter(
                    name="pattern",
                    type="string",
                    description="파일 필터 패턴 (예: *.pdf / *.py) — 기본: 전체",
                    required=False,
                ),
            ],
            category=ToolCategory.CUSTOM,
            safety_hint=SafetyLevelHint.WRITE,  # HITL 확인 트리거 (파일 시스템 접근)
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        start = time.monotonic()
        path = parameters.get("path", "").strip()
        pattern = parameters.get("pattern", "").strip()

        if not path:
            return ToolResult(
                tool_name="pc_file_list",
                success=False,
                error="path가 비어 있습니다",
            )

        # 경로 안전성 검증
        if not _is_safe_path(path):
            allowed = ", ".join(ALLOWED_FILE_PATHS) if ALLOWED_FILE_PATHS else "(미설정)"
            return ToolResult(
                tool_name="pc_file_list",
                success=False,
                error=f"허용되지 않은 경로입니다. 허용 경로: {allowed}",
            )

        # WSL 경로로 변환해서 ls 사용 (더 안전)
        wsl_path = _wsl_path(path)

        # 패턴이 있으면 ls에 glob 적용
        if pattern:
            # 패턴 안전성 — 알파벳/숫자/점/별표/물음표만 허용
            if not re.match(r"^[\w.*?]+$", pattern):
                return ToolResult(
                    tool_name="pc_file_list",
                    success=False,
                    error=f"안전하지 않은 패턴: '{pattern}'",
                )
            target = os.path.join(wsl_path, pattern)
            cmd = ["ls", "-la", target]
        else:
            cmd = ["ls", "-la", wsl_path]

        logger.info("pc_file_list", path=path, wsl_path=wsl_path, pattern=pattern or None)

        success, output = await _run_subprocess(cmd)
        elapsed = (time.monotonic() - start) * 1000

        # 출력 길이 제한 (토큰 절약)
        if success and len(output) > 3000:
            output = output[:3000] + "\n... (결과 잘림)"

        return ToolResult(
            tool_name="pc_file_list",
            success=success,
            output=output if success else None,
            error=output if not success else "",
            latency_ms=elapsed,
        )


# ============================================================
# Tool 3: pc_system_info — 시스템 정보 조회
# ============================================================

class PCSystemInfoTool(ToolExecutor):
    """PC 시스템 정보 조회

    허용된 PowerShell 명령만 실행 (읽기 전용)
    """

    def get_definition(self) -> ToolDefinition:
        info_types = ", ".join(sorted(SYSTEM_INFO_COMMANDS.keys()))
        return ToolDefinition(
            name="pc_system_info",
            description=(
                f"PC 시스템 정보를 조회합니다. "
                f"조회 유형: {info_types}"
            ),
            parameters=[
                ToolParameter(
                    name="info_type",
                    type="string",
                    description=f"조회 유형 ({info_types})",
                    required=True,
                    enum=sorted(SYSTEM_INFO_COMMANDS.keys()),
                ),
            ],
            category=ToolCategory.CUSTOM,
            safety_hint=SafetyLevelHint.WRITE,  # HITL 확인 트리거
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        start = time.monotonic()
        info_type = parameters.get("info_type", "").strip().lower()

        if not info_type:
            return ToolResult(
                tool_name="pc_system_info",
                success=False,
                error="info_type이 비어 있습니다",
            )

        if info_type not in SYSTEM_INFO_COMMANDS:
            available = ", ".join(sorted(SYSTEM_INFO_COMMANDS.keys()))
            return ToolResult(
                tool_name="pc_system_info",
                success=False,
                error=f"허용되지 않은 유형: '{info_type}'. 허용: {available}",
            )

        cmd = SYSTEM_INFO_COMMANDS[info_type]
        logger.info("pc_system_info", info_type=info_type)

        success, output = await _run_subprocess(cmd)
        elapsed = (time.monotonic() - start) * 1000

        if success and len(output) > 3000:
            output = output[:3000] + "\n... (결과 잘림)"

        return ToolResult(
            tool_name="pc_system_info",
            success=success,
            output=output if success else None,
            error=output if not success else "",
            latency_ms=elapsed,
        )


# ============================================================
# Tool 4: pc_close_app — 프로세스 종료
# ============================================================

# 종료 허용 프로세스 (시스템 프로세스 제외)
CLOSEABLE_APPS: dict[str, str] = {
    "chrome": "chrome.exe",
    "edge": "msedge.exe",
    "firefox": "firefox.exe",
    "vscode": "Code.exe",
    "notepad": "notepad.exe",
    "spotify": "Spotify.exe",
    "telegram": "Telegram.exe",
    "kakaotalk": "KakaoTalk.exe",
    "discord": "Discord.exe",
    "slack": "slack.exe",
    "calculator": "Calculator.exe",
    "explorer": "explorer.exe",
}


class PCCloseAppTool(ToolExecutor):
    """PC 앱 종료 (whitelist 기반)

    taskkill.exe를 사용하여 프로세스 종료
    시스템 프로세스 종료 불가 — CLOSEABLE_APPS에 등록된 것만 허용
    """

    def get_definition(self) -> ToolDefinition:
        available = ", ".join(sorted(CLOSEABLE_APPS.keys()))
        return ToolDefinition(
            name="pc_close_app",
            description=(
                f"PC에서 실행 중인 프로그램을 종료합니다. "
                f"종료 가능: {available}"
            ),
            parameters=[
                ToolParameter(
                    name="app_name",
                    type="string",
                    description=f"종료할 앱 이름 ({available})",
                    required=True,
                ),
            ],
            category=ToolCategory.CUSTOM,
            safety_hint=SafetyLevelHint.DESTRUCTIVE,  # HITL 강제
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        start = time.monotonic()
        app_name = parameters.get("app_name", "").strip().lower()

        if not app_name:
            return ToolResult(
                tool_name="pc_close_app",
                success=False,
                error="app_name이 비어 있습니다",
            )

        if app_name not in CLOSEABLE_APPS:
            available = ", ".join(sorted(CLOSEABLE_APPS.keys()))
            return ToolResult(
                tool_name="pc_close_app",
                success=False,
                error=f"종료 불가: '{app_name}'. 허용 목록: {available}",
            )

        process_name = CLOSEABLE_APPS[app_name]
        cmd = ["taskkill.exe", "/IM", process_name, "/F"]

        logger.info("pc_close_app", app=app_name, process=process_name)

        success, output = await _run_subprocess(cmd)
        elapsed = (time.monotonic() - start) * 1000

        return ToolResult(
            tool_name="pc_close_app",
            success=success,
            output=f"✅ {app_name} ({process_name}) 종료됨" if success else None,
            error=output if not success else "",
            latency_ms=elapsed,
        )
