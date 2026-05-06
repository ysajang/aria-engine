"""#8 일정 관리 자동화

/admin schedule "내일 3시 미팅" 명령으로 트리거
정규식으로 한국어 날짜/시간 파싱 → Google Calendar 생성

LLM: 불필요 (정규식 파싱 + Calendar 도구)
도구: mcp_calendar_create_event
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any

from aria.workflows.types import (
    OnError,
    StepType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowFunc,
    WorkflowStep,
)

# KST = UTC+9
KST_OFFSET = timedelta(hours=9)


def _parse_korean_datetime(text: str) -> dict[str, Any]:
    """한국어 날짜/시간 텍스트 파싱

    지원 패턴:
    - "내일 3시", "모레 오후 2시"
    - "다음주 화요일 14시"
    - "5/10 10:30"
    - "2026-05-10 15:00"

    Returns:
        {"title": str, "start": str(ISO), "end": str(ISO)|None, "parsed": bool}
    """
    now_kst = datetime.now(timezone.utc) + KST_OFFSET
    parsed_date = None
    parsed_hour = None
    parsed_minute = 0
    title = text.strip()

    # 1. "내일", "모레" 키워드
    if "내일" in text:
        parsed_date = now_kst.date() + timedelta(days=1)
        title = text.replace("내일", "").strip()
    elif "모레" in text:
        parsed_date = now_kst.date() + timedelta(days=2)
        title = text.replace("모레", "").strip()
    elif "오늘" in text:
        parsed_date = now_kst.date()
        title = text.replace("오늘", "").strip()

    # 2. "다음주 X요일"
    day_map = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
    dow_match = re.search(r"다음주\s*([월화수목금토일])요일?", text)
    if dow_match:
        target_dow = day_map.get(dow_match.group(1), 0)
        days_ahead = (target_dow - now_kst.weekday() + 7) % 7 + 7
        parsed_date = now_kst.date() + timedelta(days=days_ahead)
        title = re.sub(r"다음주\s*[월화수목금토일]요일?\s*", "", title).strip()

    # 3. "M/D" 또는 "YYYY-MM-DD"
    date_match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if date_match:
        try:
            from datetime import date

            parsed_date = date(
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
            )
            title = text[:date_match.start()].strip() + " " + text[date_match.end():].strip()
            title = title.strip()
        except ValueError:
            pass

    if not parsed_date:
        md_match = re.search(r"(\d{1,2})/(\d{1,2})", text)
        if md_match:
            try:
                from datetime import date

                parsed_date = date(now_kst.year, int(md_match.group(1)), int(md_match.group(2)))
                title = text[:md_match.start()].strip() + " " + text[md_match.end():].strip()
                title = title.strip()
            except ValueError:
                pass

    # 4. 시간 파싱: "오후 3시", "15:30", "3시 30분"
    pm = "오후" in text or "pm" in text.lower()
    am = "오전" in text or "am" in text.lower()
    title = re.sub(r"(오전|오후|AM|PM|am|pm)\s*", "", title).strip()

    time_match = re.search(r"(\d{1,2}):(\d{2})", text)
    if time_match:
        parsed_hour = int(time_match.group(1))
        parsed_minute = int(time_match.group(2))
        title = re.sub(r"\d{1,2}:\d{2}", "", title).strip()
    else:
        hm_match = re.search(r"(\d{1,2})시\s*(?:(\d{1,2})분)?", text)
        if hm_match:
            parsed_hour = int(hm_match.group(1))
            parsed_minute = int(hm_match.group(2)) if hm_match.group(2) else 0
            title = re.sub(r"\d{1,2}시\s*(?:\d{1,2}분)?", "", title).strip()

    if parsed_hour is not None:
        if pm and parsed_hour < 12:
            parsed_hour += 12
        elif am and parsed_hour == 12:
            parsed_hour = 0

    # 날짜 기본값: 오늘
    if not parsed_date:
        parsed_date = now_kst.date()

    # 시간 기본값: 없으면 파싱 실패 표시
    if parsed_hour is None:
        return {"title": title or text, "parsed": False}

    # KST → UTC 변환
    kst_dt = datetime(
        parsed_date.year, parsed_date.month, parsed_date.day,
        parsed_hour, parsed_minute,
        tzinfo=timezone(KST_OFFSET),
    )
    utc_dt = kst_dt.astimezone(timezone.utc)

    # 기본 1시간 이벤트
    end_dt = utc_dt + timedelta(hours=1)

    return {
        "title": title or "일정",
        "start": utc_dt.isoformat(),
        "end": end_dt.isoformat(),
        "parsed": True,
    }


def build_schedule_manager(
    tool_registry: Any = None,
    **kwargs: Any,
) -> tuple[WorkflowDefinition, dict[str, WorkflowFunc]]:
    """일정 관리 워크플로우 빌드"""

    async def parse_schedule(ctx: WorkflowContext) -> dict[str, Any]:
        """텍스트에서 일정 파싱"""
        text = ctx.get("text", "")
        if not text:
            raise ValueError("일정 텍스트가 필요합니다 (예: '내일 3시 미팅')")

        result = _parse_korean_datetime(text)
        if not result.get("parsed"):
            raise ValueError(
                f"날짜/시간 파싱 실패: '{text}'. "
                "예: '내일 3시 미팅', '5/10 14:00 회의', '다음주 화요일 10시 미팅'"
            )
        return result

    async def create_calendar_event(ctx: WorkflowContext) -> dict[str, Any]:
        """Google Calendar 이벤트 생성"""
        event_data = ctx.get("event_data", {})
        if not event_data or not event_data.get("parsed"):
            raise ValueError("파싱된 일정 데이터가 없습니다")

        if not tool_registry:
            return {"status": "skipped", "reason": "ToolRegistry 미설정"}

        result = await tool_registry.execute(
            tool_name="mcp_calendar_create_event",
            arguments={
                "summary": event_data["title"],
                "start": {"dateTime": event_data["start"]},
                "end": {"dateTime": event_data["end"]},
            },
            context="schedule_manager",
        )

        if result.success:
            return result.output or {"status": "created"}
        raise RuntimeError(f"일정 생성 실패: {result.error}")

    definition = WorkflowDefinition(
        workflow_id="schedule-manager",
        name="일정 관리 자동화",
        description="텍스트 → 정규식 파싱 → Google Calendar 생성",
        category="admin",
        scope="global",
        steps=[
            WorkflowStep(
                name="parse",
                step_type=StepType.FUNCTION,
                config={"func_name": "schedule_parse"},
                output_key="event_data",
            ),
            WorkflowStep(
                name="create-event",
                step_type=StepType.FUNCTION,
                config={"func_name": "schedule_create"},
                output_key="calendar_result",
                on_error=OnError.SKIP,
            ),
            WorkflowStep(
                name="confirm",
                step_type=StepType.TEMPLATE,
                config={"template_name": "admin/schedule_confirm.md.j2"},
                output_key="confirmation",
            ),
        ],
    )

    functions = {
        "schedule_parse": parse_schedule,
        "schedule_create": create_calendar_event,
    }
    return definition, functions
