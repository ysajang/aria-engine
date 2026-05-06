"""ARIA Engine - External Context Bridge

외부 AI 도구(Cursor/Claude Desktop/ChatGPT)와 ARIA 메모리 간
컨텍스트 교환 모듈

Push: 외부 대화 로그 → ARIA 메모리 흡수
Pull: ARIA 메모리 → 외부 도구에 주입할 마크다운 반환
"""

from aria.context.bridge import ContextBridge
from aria.context.analyzer import analyze_conversation, build_memory_content
from aria.context.types import (
    ContextPullRequest,
    ContextPullResponse,
    ContextPushRequest,
    ContextPushResponse,
    ConversationMessage,
    ExternalSource,
    AnalysisResult,
    ExtractedInsight,
    SessionRecord,
)

__all__ = [
    "ContextBridge",
    "analyze_conversation",
    "build_memory_content",
    "ContextPullRequest",
    "ContextPullResponse",
    "ContextPushRequest",
    "ContextPushResponse",
    "ConversationMessage",
    "ExternalSource",
    "AnalysisResult",
    "ExtractedInsight",
    "SessionRecord",
]
