"""ARIA Engine - Learning Manager (Phase 5 통합)

5개 학습 모듈을 통합 관리하는 오케스트레이터

역할:
    1. 5개 학습 모듈 초기화/생명주기 관리
    2. post_query_hook() — /v1/query 응답 후 비동기 학습 실행
    3. 통합 통계 제공

post_query_hook 플로우:
    /v1/query 응답 완료
      → asyncio.create_task(learning_manager.post_query_hook(...))
        → 축 1: ConversationLearner.safe_analyze() (선호 감지)
        → 축 2: FeedbackLoop.safe_analyze() (교정/칭찬/불만)
        → 축 3: ToolOptimizer.record_execution() (도구 기록 — LLM 없음)
        → 축 4: PromptImprover.record_low_confidence() (저신뢰 기록 — LLM 없음)
        → 축 5: PatternPredictor.record_query() (쿼리 기록 — LLM 없음)

비용: post_query_hook 1회 ~$0.0006 (축 1+2 Haiku 2회)
      축 3~5 기록은 LLM 없음 ($0)

안전장치: 학습 실패 → 메인 파이프라인 영향 없음 (비차단 asyncio.create_task)
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from aria.core.config import AriaConfig
from aria.events.event_store import EventStore
from aria.learning.conversation_learner import ConversationLearner
from aria.learning.feedback_loop import FeedbackLoop
from aria.learning.pattern_predictor import PatternPredictor
from aria.learning.prompt_improver import PromptImprover
from aria.learning.tool_optimizer import ToolOptimizer
from aria.learning.types import LearningAnalysisRequest
from aria.memory.index_manager import IndexManager
from aria.providers.llm_provider import LLMProvider

logger = structlog.get_logger()


class LearningManager:
    """5개 학습 모듈 통합 관리자

    Args:
        llm: LLM 프로바이더 (cheap 모델 사용)
        event_store: 이벤트 저장소
        index_manager: 메모리 인덱스 매니저
        config: ARIA 통합 설정 (LearningConfig 포함)
    """

    def __init__(
        self,
        llm: LLMProvider,
        event_store: EventStore,
        index_manager: IndexManager | None = None,
        config: AriaConfig | None = None,
    ) -> None:
        lc = config.learning if config else None
        enabled = lc.enabled if lc else True

        # 축 1: 대화 학습
        self.conversation_learner = ConversationLearner(
            llm=llm,
            event_store=event_store,
            index_manager=index_manager,
            enabled=enabled and (lc.conversation_learning if lc else True),
            confidence_threshold=(
                lc.preference_confidence_threshold if lc else 0.7
            ),
            max_preferences_per_query=(
                lc.max_preferences_per_query if lc else 3
            ),
        )

        # 축 2: 피드백 루프
        self.feedback_loop = FeedbackLoop(
            llm=llm,
            event_store=event_store,
            index_manager=index_manager,
            enabled=enabled and (lc.feedback_loop if lc else True),
            correction_memory_domain=(
                lc.correction_memory_domain if lc else "correction-log"
            ),
        )

        # 축 3: 도구 최적화
        self.tool_optimizer = ToolOptimizer(
            llm=llm,
            event_store=event_store,
            index_manager=index_manager,
            enabled=enabled and (lc.tool_optimizer if lc else True),
            metrics_window_days=(
                lc.tool_metrics_window_days if lc else 7
            ),
            min_calls_for_priority=(
                lc.min_calls_for_priority if lc else 5
            ),
        )

        # 축 4: 프롬프트 개선
        self.prompt_improver = PromptImprover(
            llm=llm,
            event_store=event_store,
            index_manager=index_manager,
            enabled=enabled and (lc.prompt_improver if lc else True),
            low_confidence_threshold=(
                lc.low_confidence_threshold if lc else 0.5
            ),
        )

        # 축 5: 패턴 예측
        self.pattern_predictor = PatternPredictor(
            llm=llm,
            event_store=event_store,
            index_manager=index_manager,
            enabled=enabled and (lc.pattern_predictor if lc else True),
            min_occurrences=(
                lc.min_pattern_occurrences if lc else 3
            ),
            confidence_threshold=(
                lc.pattern_confidence_threshold if lc else 0.6
            ),
        )

        self._enabled = enabled
        logger.info(
            "learning_manager_initialized",
            enabled=enabled,
            axes={
                "conversation": self.conversation_learner.enabled,
                "feedback": self.feedback_loop.enabled,
                "tool_optimizer": self.tool_optimizer.enabled,
                "prompt_improver": self.prompt_improver.enabled,
                "pattern_predictor": self.pattern_predictor.enabled,
            },
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_stats(self) -> dict[str, Any]:
        """전체 학습 통계"""
        return {
            "enabled": self._enabled,
            "axes": {
                "conversation_learner": self.conversation_learner.stats,
                "feedback_loop": self.feedback_loop.stats,
                "tool_optimizer": self.tool_optimizer.stats,
                "prompt_improver": self.prompt_improver.stats,
                "pattern_predictor": self.pattern_predictor.stats,
            },
        }

    async def post_query_hook(
        self,
        query: str,
        answer: str,
        confidence: float,
        scope: str = "global",
        tool_calls_made: int = 0,
        tool_results: list[dict[str, Any]] | None = None,
        intent_action: str = "",
        previous_query: str = "",
    ) -> None:
        """쿼리 응답 후 비동기 학습 실행

        asyncio.create_task()로 호출 — 메인 응답 지연 없음

        Args:
            query: 사용자 쿼리
            answer: ARIA 응답
            confidence: 응답 신뢰도
            scope: 메모리 스코프
            tool_calls_made: 사용된 도구 수
            tool_results: 도구 실행 결과 [{name, success, latency_ms, error}, ...]
            intent_action: 의도분석 결과 (search_knowledge/respond 등)
            previous_query: 이전 쿼리 (피드백 분석용)
        """
        if not self._enabled:
            return

        try:
            # === 축 1: 대화 학습 (LLM 호출 — ~$0.0003) ===
            conv_request = LearningAnalysisRequest(
                query=query,
                response=answer,
                confidence=confidence,
                tool_calls=[{"name": tr.get("name", "")} for tr in (tool_results or [])],
                scope=scope,
                metadata={
                    "iterations": 1,
                    "confidence": confidence,
                },
            )
            await self.conversation_learner.safe_analyze(conv_request)

            # === 축 2: 피드백 루프 (LLM 호출 — ~$0.0003) ===
            if previous_query:
                fb_request = LearningAnalysisRequest(
                    query=query,
                    response=answer,  # 이전 응답 (피드백 대상)
                    confidence=confidence,
                    scope=scope,
                    metadata={"previous_query": previous_query},
                )
                await self.feedback_loop.safe_analyze(fb_request)

            # === 축 3: 도구 실행 기록 (LLM 없음 — $0) ===
            for tr in (tool_results or []):
                self.tool_optimizer.record_execution(
                    tool_name=tr.get("name", "unknown"),
                    success=tr.get("success", False),
                    latency_ms=tr.get("latency_ms", 0),
                    error=tr.get("error", ""),
                )

            # === 축 4: 저신뢰 기록 (LLM 없음 — $0) ===
            self.prompt_improver.record_low_confidence(
                query=query,
                confidence=confidence,
                domain=intent_action,
                tool_calls=tool_calls_made,
            )

            # === 축 5: 쿼리 기록 (LLM 없음 — $0) ===
            tool_names = [tr.get("name", "") for tr in (tool_results or [])]
            self.pattern_predictor.record_query(
                query=query,
                intent_action=intent_action,
                tool_calls=tool_names if tool_names else None,
            )

        except Exception as e:
            # 학습 실패는 절대로 메인 파이프라인을 방해하면 안 됨
            logger.warning(
                "learning_post_query_hook_failed",
                error=str(e),
                query=query[:100],
            )
