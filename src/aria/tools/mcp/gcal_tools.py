"""ARIA Engine - MCP Tool: Google Calendar

Calendar API v3 기반 도구 3종
- GCalListEventsTool: 일정 조회 (기간별 / 검색)
- GCalCreateEventTool: 일정 생성 (Critic NEEDS_CONFIRMATION)
- GCalUpdateEventTool: 일정 수정 (Critic NEEDS_CONFIRMATION)

인증: OAuth2 (GoogleTokenManager)
스코프: https://www.googleapis.com/auth/calendar
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from aria.auth.google_oauth import GoogleAuthError, GoogleTokenManager
from aria.tools.tool_types import (
    SafetyLevelHint,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolResult,
)

logger = structlog.get_logger()

CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"


class GCalClient:
    """Google Calendar API 클라이언트

    Args:
        token_manager: GoogleTokenManager 인스턴스
    """

    def __init__(self, token_manager: GoogleTokenManager) -> None:
        self._token_mgr = token_manager
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self._client

    async def _headers(self) -> dict[str, str]:
        token = await self._token_mgr.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """API 요청 + 401 자동 재인증"""
        client = await self._get_client()
        headers = await self._headers()

        resp = await client.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 401:
            self._token_mgr.invalidate()
            headers = await self._headers()
            resp = await client.request(method, url, headers=headers, **kwargs)

        resp.raise_for_status()
        return resp.json() if resp.text else {}

    async def list_events(
        self,
        calendar_id: str = "primary",
        time_min: str | None = None,
        time_max: str | None = None,
        query: str = "",
        max_results: int = 20,
        single_events: bool = True,
        order_by: str = "startTime",
    ) -> list[dict[str, Any]]:
        """일정 조회

        Args:
            calendar_id: 캘린더 ID (primary = 기본 캘린더)
            time_min: 시작 시간 (ISO 8601 / 예: 2026-05-04T00:00:00+09:00)
            time_max: 종료 시간
            query: 검색 쿼리 (제목/설명 검색)
            max_results: 최대 결과 수
            single_events: 반복 일정 개별 표시
            order_by: 정렬 (startTime / updated)
        """
        params: dict[str, Any] = {
            "maxResults": min(max(max_results, 1), 100),
            "singleEvents": str(single_events).lower(),
            "orderBy": order_by,
        }
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        if query:
            params["q"] = query

        data = await self._request(
            "GET",
            f"{CALENDAR_BASE}/calendars/{calendar_id}/events",
            params=params,
        )

        events = data.get("items", [])
        return [_simplify_event(e) for e in events]

    async def create_event(
        self,
        summary: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
        calendar_id: str = "primary",
        all_day: bool = False,
        timezone: str = "Asia/Seoul",
    ) -> dict[str, Any]:
        """일정 생성

        Args:
            summary: 일정 제목
            start: 시작 시간 (ISO 8601 또는 날짜 YYYY-MM-DD)
            end: 종료 시간
            description: 일정 설명
            location: 장소
            calendar_id: 캘린더 ID
            all_day: 종일 일정 여부
            timezone: 시간대
        """
        body: dict[str, Any] = {
            "summary": summary,
        }

        if all_day:
            body["start"] = {"date": start}
            body["end"] = {"date": end}
        else:
            body["start"] = {"dateTime": start, "timeZone": timezone}
            body["end"] = {"dateTime": end, "timeZone": timezone}

        if description:
            body["description"] = description
        if location:
            body["location"] = location

        data = await self._request(
            "POST",
            f"{CALENDAR_BASE}/calendars/{calendar_id}/events",
            json=body,
        )

        return _simplify_event(data)

    async def update_event(
        self,
        event_id: str,
        calendar_id: str = "primary",
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
        timezone: str = "Asia/Seoul",
    ) -> dict[str, Any]:
        """일정 수정 (PATCH — 지정한 필드만 변경)

        Args:
            event_id: 수정할 일정 ID
            나머지: 변경할 필드만 지정 (None이면 기존 유지)
        """
        body: dict[str, Any] = {}

        if summary is not None:
            body["summary"] = summary
        if start is not None:
            body["start"] = {"dateTime": start, "timeZone": timezone}
        if end is not None:
            body["end"] = {"dateTime": end, "timeZone": timezone}
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location

        if not body:
            raise ValueError("변경할 필드가 없습니다")

        data = await self._request(
            "PATCH",
            f"{CALENDAR_BASE}/calendars/{calendar_id}/events/{event_id}",
            json=body,
        )

        return _simplify_event(data)

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# === 유틸리티 ===


def _simplify_event(event: dict[str, Any]) -> dict[str, Any]:
    """Calendar API 이벤트를 LLM 친화 형태로 정제"""
    start = event.get("start", {})
    end = event.get("end", {})

    return {
        "id": event.get("id", ""),
        "summary": event.get("summary", "(제목 없음)"),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "start": start.get("dateTime") or start.get("date", ""),
        "end": end.get("dateTime") or end.get("date", ""),
        "all_day": "date" in start and "dateTime" not in start,
        "status": event.get("status", ""),
        "html_link": event.get("htmlLink", ""),
        "creator": event.get("creator", {}).get("email", ""),
        "attendees": [
            {"email": a.get("email", ""), "status": a.get("responseStatus", "")}
            for a in event.get("attendees", [])
        ][:10],
    }


# === Tool Executors ===


class GCalListEventsTool(ToolExecutor):
    """Google Calendar 일정 조회"""

    def __init__(self, client: GCalClient) -> None:
        self._client = client

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="gcal_list_events",
            description=(
                "Google Calendar에서 일정을 조회합니다. "
                "기간 지정 (time_min/time_max) 또는 키워드 검색이 가능합니다. "
                "오늘/이번 주/다음 주 일정 확인에 사용합니다."
            ),
            parameters=[
                ToolParameter(name="time_min", type="string", description="시작 시간 (ISO 8601 / 예: 2026-05-04T00:00:00+09:00)", required=False),
                ToolParameter(name="time_max", type="string", description="종료 시간 (ISO 8601)", required=False),
                ToolParameter(name="query", type="string", description="검색 쿼리 (제목/설명 검색)", required=False),
                ToolParameter(name="max_results", type="integer", description="최대 결과 수 (1~100 / 기본값 20)", required=False, default=20),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        time_min = parameters.get("time_min")
        time_max = parameters.get("time_max")
        query = parameters.get("query", "")
        max_results = int(parameters.get("max_results", 20))

        try:
            events = await self._client.list_events(
                time_min=time_min,
                time_max=time_max,
                query=query,
                max_results=max_results,
            )
            logger.info("gcal_list_success", count=len(events))
            return ToolResult(
                tool_name="gcal_list_events",
                success=True,
                output={"events": events, "event_count": len(events)},
            )
        except GoogleAuthError as e:
            return ToolResult(tool_name="gcal_list_events", success=False, error=f"인증 실패: {str(e)}")
        except Exception as e:
            return ToolResult(tool_name="gcal_list_events", success=False, error=f"일정 조회 실패: {str(e)[:300]}")


class GCalCreateEventTool(ToolExecutor):
    """Google Calendar 일정 생성 (HITL 확인 필요)"""

    def __init__(self, client: GCalClient) -> None:
        self._client = client

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="gcal_create_event",
            description=(
                "Google Calendar에 새 일정을 생성합니다. "
                "제목, 시작/종료 시간, 설명, 장소를 지정합니다. "
                "종일 일정도 지원합니다."
            ),
            parameters=[
                ToolParameter(name="summary", type="string", description="일정 제목", required=True),
                ToolParameter(name="start", type="string", description="시작 시간 (ISO 8601 / 종일: YYYY-MM-DD)", required=True),
                ToolParameter(name="end", type="string", description="종료 시간 (ISO 8601 / 종일: YYYY-MM-DD)", required=True),
                ToolParameter(name="description", type="string", description="일정 설명", required=False),
                ToolParameter(name="location", type="string", description="장소", required=False),
                ToolParameter(name="all_day", type="boolean", description="종일 일정 여부 (기본값 false)", required=False),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.WRITE,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        summary = parameters.get("summary", "").strip()
        start = parameters.get("start", "").strip()
        end = parameters.get("end", "").strip()

        if not summary:
            return ToolResult(tool_name="gcal_create_event", success=False, error="제목(summary)이 비어있습니다")
        if not start or not end:
            return ToolResult(tool_name="gcal_create_event", success=False, error="시작/종료 시간이 필요합니다")

        description = parameters.get("description", "")
        location = parameters.get("location", "")
        all_day = bool(parameters.get("all_day", False))

        try:
            event = await self._client.create_event(
                summary=summary, start=start, end=end,
                description=description, location=location, all_day=all_day,
            )
            logger.info("gcal_create_success", summary=summary[:30], event_id=event["id"])
            return ToolResult(
                tool_name="gcal_create_event",
                success=True,
                output={"status": "created", **event},
            )
        except GoogleAuthError as e:
            return ToolResult(tool_name="gcal_create_event", success=False, error=f"인증 실패: {str(e)}")
        except Exception as e:
            return ToolResult(tool_name="gcal_create_event", success=False, error=f"일정 생성 실패: {str(e)[:300]}")


class GCalUpdateEventTool(ToolExecutor):
    """Google Calendar 일정 수정 (HITL 확인 필요)"""

    def __init__(self, client: GCalClient) -> None:
        self._client = client

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="gcal_update_event",
            description=(
                "Google Calendar의 기존 일정을 수정합니다. "
                "gcal_list_events로 얻은 event_id를 사용합니다. "
                "변경할 필드만 지정하면 나머지는 기존 값 유지됩니다."
            ),
            parameters=[
                ToolParameter(name="event_id", type="string", description="수정할 일정 ID (gcal_list_events 결과에서 획득)", required=True),
                ToolParameter(name="summary", type="string", description="변경할 제목", required=False),
                ToolParameter(name="start", type="string", description="변경할 시작 시간 (ISO 8601)", required=False),
                ToolParameter(name="end", type="string", description="변경할 종료 시간 (ISO 8601)", required=False),
                ToolParameter(name="description", type="string", description="변경할 설명", required=False),
                ToolParameter(name="location", type="string", description="변경할 장소", required=False),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.WRITE,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        event_id = parameters.get("event_id", "").strip()
        if not event_id:
            return ToolResult(tool_name="gcal_update_event", success=False, error="event_id가 비어있습니다")

        try:
            event = await self._client.update_event(
                event_id=event_id,
                summary=parameters.get("summary"),
                start=parameters.get("start"),
                end=parameters.get("end"),
                description=parameters.get("description"),
                location=parameters.get("location"),
            )
            logger.info("gcal_update_success", event_id=event_id)
            return ToolResult(
                tool_name="gcal_update_event",
                success=True,
                output={"status": "updated", **event},
            )
        except ValueError as e:
            return ToolResult(tool_name="gcal_update_event", success=False, error=str(e))
        except GoogleAuthError as e:
            return ToolResult(tool_name="gcal_update_event", success=False, error=f"인증 실패: {str(e)}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return ToolResult(tool_name="gcal_update_event", success=False, error="일정을 찾을 수 없습니다")
            return ToolResult(tool_name="gcal_update_event", success=False, error=f"Calendar API 에러 (HTTP {e.response.status_code})")
        except Exception as e:
            return ToolResult(tool_name="gcal_update_event", success=False, error=f"일정 수정 실패: {str(e)[:300]}")
