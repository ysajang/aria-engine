"""ARIA Engine - Feedback Loop (Phase 5 축 2)

교정/칭찬/불만을 감지하여 같은 실수 반복 방지

동작 플로우:
    1. /v1/query 응답 완료 후 비동기 호출
    2. cheap 모델(Haiku)로 사용자 쿼리 분석 (이전 응답 대비 피드백 감지)
    3. 피드백 유형 분류 (correction / praise / complaint)
    4. CorrectionRecord 생성 → EventStore 저장
    5. 교정 사항 → correction-log 메모리 토픽에 축적

핵심 가치:
    - 같은 실수를 반복하지 않음 (correction-log가 메모리에 로딩되어 참고)
    - 칭찬은 강화 학습 신호로 기록 (어떤 행동이 좋았는지 축적)
    - 불만은 패턴 분석 재료로 축적

비용: 분석 1회 ~$0.0003 (Haiku)
"""

from __future__ import annotations

from typing import Any

import structlog

from aria.events.event_store import EventStore
from aria.events.types import EventSeverity
from aria.learning.base import BaseLearner
from aria.learning.types import (
    CorrectionRecord,
    CorrectionType,
    LearningAnalysisRequest,
    LearningAnalysisResult,
    LearningEventType,
)
from aria.memory.index_manager import IndexManager
from aria.providers.llm_provider import LLMProvider

logger = structlog.get_logger()

# 메모리 토픽 도메인 (교정 기록 저장용)
CORRECTION_MEMORY_DOMAIN = "correction-log"

# 피드백 분석 시스템 프롬프트
FEEDBACK_ANALYSIS_SYSTEM = """당신은 대화 피드백 분석 전문가입니다.

사용자의 현재 메시지가 AI 어시스턴트의 이전 응답에 대한 피드백(교정/칭찬/불만)인지 분석하세요.

JSON 형태로만 응답하세요 (다른 텍스트 없이):
{
    "has_feedback": true/false,
    "feedback_type": "correction" | "praise" | "complaint" | null,
    "correction_type": "factual_error" | "style_correction" | "tool_misuse" | "missing_context" | "wrong_assumption" | "incomplete_response" | "excessive_response" | null,
    "severity": "minor" | "major" | "critical",
    "original_issue": "AI가 잘못한 점 요약 (교정/불만 시)",
    "corrected_behavior": "올바른 행동 설명 (교정 시 — AI가 앞으로 어떻게 해야 하는지)",
    "positive_behavior": "잘한 점 설명 (칭찬 시 — 앞으로도 유지해야 할 행동)",
    "confidence": 0.0~1.0
}

피드백 감지 기준:
- correction: "아니" "틀렸어" "그게 아니라" "다시 해줘" "잘못됐어" + 수정 내용
- praise: "잘했어" "완벽" "고마워" "이거야" "좋아" + 이전 응답에 대한 긍정
- complaint: "왜 맨날" "또 이래" "답답하다" "이상해" + 반복적 문제 지적
- 단순 새 질문이나 주제 전환은 피드백이 아님 (has_feedback=false)

규칙:
1. 피드백이 아닌 경우 has_feedback=false + 나머지 null
2. confidence는 보수적으로 — 명시적 교정은 0.8+ / 암묵적 불만은 0.4~0.6
3. corrected_behavior는 구체적으로 — "다음에는 ~하세요" 형태
4. severity: minor=스타일/표현 / major=내용 오류 / critical=반복 오류+강한 불만"""


class FeedbackLoop(BaseLearner):
    """피드백 루프 — 교정/칭찬/불만 감지 + correction-log 축적

    Args:
        llm: LLM 프로바이더 (cheap 모델 사용)
        event_store: 이벤트 저장소
        index_manager: 메모리 인덱스 매니저
        enabled: 활성화 여부
        correction_memory_domain: 교정 기록 메모리 도메인
    """

    def __init__(
        self,
        llm: LLMProvider,
        event_store: EventStore,
        index_manager: IndexManager | None = None,
        enabled: bool = True,
        correction_memory_domain: str = CORRECTION_MEMORY_DOMAIN,
    ) -> None:
        super().__init__(
            llm=llm,
            event_store=event_store,
            index_manager=index_manager,
            enabled=enabled,
        )
        self._correction_memory_domain = correction_memory_domain
        self._total_corrections: int = 0
        self._total_praises: int = 0
        self._total_complaints: int = 0

    @property
    def stats(self) -> dict[str, Any]:
        base = super().stats
        base.update({
            "total_corrections": self._total_corrections,
            "total_praises": self._total_praises,
            "total_complaints": self._total_complaints,
        })
        return base

    async def analyze(
        self,
        request: LearningAnalysisRequest,
    ) -> LearningAnalysisResult:
        """피드백 분석 → 교정/칭찬/불만 감지 → 이벤트 + 메모리 저장

        Args:
            request: 분석 요청
                - query: 사용자 현재 메시지
                - response: ARIA의 이전 응답 (피드백 대상)
                - metadata.previous_query: 이전 사용자 쿼리 (선택)

        Returns:
            분석 결과 (감지된 교정 기록 + 이벤트 수)
        """
        # 이전 응답이 없으면 피드백 분석 불가
        if not request.response:
            return LearningAnalysisResult()

        # 너무 짧은 쿼리 스킵
        if len(request.query.strip()) < 3:
            return LearningAnalysisResult()

        # cheap 모델로 피드백 분석
        user_prompt = self._build_feedback_prompt(request)
        raw_response = await self._call_cheap_llm(
            FEEDBACK_ANALYSIS_SYSTEM,
            user_prompt,
        )

        # JSON 파싱
        parsed = self._parse_json_response(raw_response)
        if not parsed.get("has_feedback", False):
            return LearningAnalysisResult()

        # 피드백 유형별 처리
        feedback_type = parsed.get("feedback_type", "")
        events_stored = 0
        corrections: list[CorrectionRecord] = []
        memory_topics_updated: list[str] = []

        if feedback_type == "correction":
            record = self._build_correction_record(parsed, request)
            if record:
                corrections.append(record)
                self._total_corrections += 1

                # 이벤트 저장
                event_id = self._store_learning_event(
                    LearningEventType.CORRECTION_DETECTED,
                    {
                        "correction_type": record.correction_type.value,
                        "severity": record.severity,
                        "corrected_behavior": record.corrected_behavior,
                        "query_context": record.query_context,
                    },
                    severity=self._severity_to_event_severity(record.severity),
                )
                if event_id:
                    events_stored += 1

                # 메모리에 교정 기록 축적
                confidence = parsed.get("confidence", 0.0)
                if confidence >= 0.6:
                    updated = self._update_correction_memory(
                        scope=request.scope,
                        record=record,
                    )
                    if updated and self._correction_memory_domain not in memory_topics_updated:
                        memory_topics_updated.append(self._correction_memory_domain)

        elif feedback_type == "praise":
            self._total_praises += 1
            event_id = self._store_learning_event(
                LearningEventType.PRAISE_DETECTED,
                {
                    "positive_behavior": parsed.get("positive_behavior", ""),
                    "query_context": request.query[:500],
                },
            )
            if event_id:
                events_stored += 1

        elif feedback_type == "complaint":
            self._total_complaints += 1
            event_id = self._store_learning_event(
                LearningEventType.COMPLAINT_DETECTED,
                {
                    "original_issue": parsed.get("original_issue", ""),
                    "severity": parsed.get("severity", "minor"),
                    "query_context": request.query[:500],
                },
                severity=EventSeverity.WARNING,
            )
            if event_id:
                events_stored += 1

        return LearningAnalysisResult(
            corrections=corrections,
            events_stored=events_stored,
            memory_topics_updated=memory_topics_updated,
        )

    def _build_feedback_prompt(self, request: LearningAnalysisRequest) -> str:
        """피드백 분석용 프롬프트 생성"""
        parts = []

        # 이전 쿼리 (있으면)
        prev_query = request.metadata.get("previous_query", "")
        if prev_query:
            parts.append(f"이전 사용자 쿼리: {str(prev_query)[:500]}")

        # AI의 이전 응답 (피드백 대상)
        parts.append(f"AI 이전 응답 (요약): {request.response[:500]}")

        # 현재 사용자 메시지 (피드백 후보)
        parts.append(f"사용자 현재 메시지: {request.query[:2000]}")

        return "\n".join(parts)

    def _build_correction_record(
        self,
        parsed: dict[str, Any],
        request: LearningAnalysisRequest,
    ) -> CorrectionRecord | None:
        """파싱된 JSON에서 CorrectionRecord 생성"""
        try:
            correction_type_str = parsed.get("correction_type", "")
            try:
                correction_type = CorrectionType(correction_type_str)
            except ValueError:
                correction_type = CorrectionType.FACTUAL_ERROR  # fallback

            return CorrectionRecord(
                correction_type=correction_type,
                original_response=request.response[:1000],
                user_feedback=request.query[:1000],
                corrected_behavior=str(parsed.get("corrected_behavior", ""))[:1000],
                severity=str(parsed.get("severity", "minor")),
                query_context=request.metadata.get("previous_query", request.query)[:500],
            )
        except (ValueError, TypeError) as e:
            logger.debug(
                "feedback_correction_parse_error",
                error=str(e),
            )
            return None

    def _update_correction_memory(
        self,
        scope: str,
        record: CorrectionRecord,
    ) -> bool:
        """교정 기록을 메모리에 축적

        correction-log 토픽에 마크다운으로 추가
        ARIA가 다음 응답 시 이 기록을 참고하여 같은 실수를 반복하지 않음

        Args:
            scope: 메모리 스코프
            record: 교정 기록

        Returns:
            upsert 성공 여부
        """
        timestamp = record.detected_at.strftime("%Y-%m-%d %H:%M")
        entry = (
            f"- [{record.severity.upper()}] "
            f"**{record.correction_type.value}** | "
            f"{record.corrected_behavior} "
            f"({timestamp})"
        )

        return self._upsert_memory_topic(
            scope=scope,
            domain=self._correction_memory_domain,
            summary="교정 기록 — 같은 실수 반복 방지용 (자동 축적)",
            content=f"## 교정 기록\n\n{entry}",
        )

    @staticmethod
    def _severity_to_event_severity(severity: str) -> EventSeverity:
        """교정 severity → EventSeverity 변환"""
        if severity == "critical":
            return EventSeverity.ERROR
        elif severity == "major":
            return EventSeverity.WARNING
        return EventSeverity.INFO
