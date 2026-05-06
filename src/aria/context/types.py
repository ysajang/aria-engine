"""ARIA Engine - External Context Bridge Types

외부 AI 도구(Cursor/Claude Desktop/ChatGPT)와 ARIA 메모리 간
컨텍스트 교환을 위한 스키마 정의

Push: 외부 대화 로그 → ARIA 메모리 흡수
Pull: ARIA 메모리 → 외부 도구에 주입할 마크다운 반환

설계 원칙:
    - LLM 호출 최소화 (규칙 기반 분석 우선)
    - 스코프 격리 유지
    - 비차단 (분석 실패해도 원본 이벤트는 저장)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ExternalSource(str, Enum):
    """외부 AI 도구 식별자"""

    CURSOR = "cursor"
    CLAUDE_DESKTOP = "claude-desktop"
    CHATGPT = "chatgpt"
    CLAUDE_CODE = "claude-code"
    CUSTOM = "custom"


class ConversationMessage(BaseModel):
    """대화 메시지 한 건"""

    role: str = Field(
        ...,
        description="메시지 역할 (user/assistant/system/tool)",
    )
    content: str = Field(
        ...,
        min_length=1,
        max_length=50_000,
        description="메시지 내용",
    )
    timestamp: str | None = Field(
        default=None,
        description="메시지 생성 시각 (ISO 8601)",
    )

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        v = v.strip().lower()
        allowed = {"user", "assistant", "system", "tool"}
        if v not in allowed:
            raise ValueError(f"허용되지 않은 role: '{v}' (허용: {', '.join(sorted(allowed))})")
        return v


class ContextPushRequest(BaseModel):
    """외부 대화 로그 인입 요청

    POST /v1/context/push
    """

    source: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="외부 도구 식별자 (cursor/claude-desktop/chatgpt/custom)",
    )
    scope: str = Field(
        default="global",
        min_length=1,
        max_length=50,
        description="저장 대상 메모리 스코프",
    )
    messages: list[ConversationMessage] = Field(
        ...,
        min_length=1,
        max_length=200,
        description="대화 메시지 목록",
    )
    session_id: str | None = Field(
        default=None,
        max_length=100,
        description="세션 식별자 (중복 인입 방지용)",
    )
    tags: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="분류 태그 (예: ['coding', 'testorum', 'bugfix'])",
    )
    auto_analyze: bool = Field(
        default=True,
        description="규칙 기반 자동 분석 수행 여부",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="추가 메타데이터",
    )

    @field_validator("source")
    @classmethod
    def normalize_source(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("scope")
    @classmethod
    def normalize_scope(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, v: list[str]) -> list[str]:
        return [t.strip().lower() for t in v if t.strip()]


class ExtractedInsight(BaseModel):
    """분석으로 추출된 인사이트 한 건"""

    category: str = Field(
        ...,
        description="카테고리 (decision/preference/fact/todo/learning/issue)",
    )
    content: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="추출된 내용 요약",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="추출 신뢰도",
    )
    source_index: int | None = Field(
        default=None,
        description="원본 메시지 인덱스",
    )


# 인사이트 카테고리
INSIGHT_CATEGORIES = frozenset({
    "decision",     # 결정사항 ("~로 결정", "~로 가자", "~하기로 했다")
    "preference",   # 선호 ("~가 좋다", "~를 선호", "~말고 ~로")
    "fact",         # 사실 정보 ("~는 ~이다", 기술적 사실)
    "todo",         # 할 일 ("~해야 한다", "다음에 ~하자")
    "learning",     # 학습 ("~를 알게 됐다", "~가 원인이었다")
    "issue",        # 문제 ("~가 안 된다", "버그", "에러")
})


class AnalysisResult(BaseModel):
    """대화 로그 분석 결과"""

    insights: list[ExtractedInsight] = Field(default_factory=list)
    summary: str = Field(
        default="",
        max_length=500,
        description="대화 요약 (한 줄)",
    )
    primary_topic: str = Field(
        default="general",
        description="주요 토픽 도메인명",
    )
    message_count: int = Field(default=0)
    user_message_count: int = Field(default=0)
    total_chars: int = Field(default=0)

    @property
    def has_insights(self) -> bool:
        return len(self.insights) > 0


class ContextPushResponse(BaseModel):
    """Push 응답"""

    status: str = Field(default="accepted")
    event_id: str | None = Field(
        default=None,
        description="저장된 이벤트 ID",
    )
    session_id: str | None = None
    insights_extracted: int = Field(
        default=0,
        description="추출된 인사이트 수",
    )
    memory_updated: bool = Field(
        default=False,
        description="메모리 upsert 수행 여부",
    )
    memory_domain: str | None = Field(
        default=None,
        description="upsert된 메모리 도메인",
    )
    analysis: AnalysisResult | None = None


class ContextPullRequest(BaseModel):
    """컨텍스트 Pull 요청

    GET /v1/context/pull (query params)
    """

    scope: str = Field(
        default="global",
        description="메모리 스코프",
    )
    domains: list[str] | None = Field(
        default=None,
        description="특정 도메인만 로딩 (None이면 전체)",
    )
    token_budget: int = Field(
        default=4000,
        ge=500,
        le=32000,
        description="토큰 예산",
    )
    format: str = Field(
        default="markdown",
        description="출력 포맷 (markdown/json)",
    )

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"markdown", "json"}:
            raise ValueError(f"지원하지 않는 포맷: '{v}' (허용: markdown, json)")
        return v


class ContextPullResponse(BaseModel):
    """Pull 응답"""

    scope: str
    loaded_domains: list[str] = Field(default_factory=list)
    content: str = Field(
        default="",
        description="마크다운 또는 JSON 문자열",
    )
    token_count: int = Field(default=0)
    format: str = Field(default="markdown")
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


# === 세션 중복 감지 ===

class SessionRecord(BaseModel):
    """세션 인입 기록 (중복 방지용)"""

    session_id: str
    source: str
    scope: str
    received_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    message_count: int = 0
