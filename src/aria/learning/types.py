"""ARIA Engine - Learning System Type Definitions

Phase 5 Self-Learning 시스템의 핵심 스키마 정의

5가지 학습 축 공통 타입:
- LearningEventType: 학습 이벤트 유형 (이벤트 수집 시스템과 연동)
- Preference: 대화에서 감지된 사용자 선호 (축 1)
- CorrectionRecord: 교정/피드백 기록 (축 2)
- ToolMetrics: 도구 사용 통계 (축 3)
- DomainInsight: 저신뢰 도메인 분석 (축 4)
- BehaviorPattern: 반복 행동 패턴 (축 5)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# === Enums ===


class LearningEventType(str, Enum):
    """학습 이벤트 유형 — EventStore의 event_type과 매핑"""

    # 축 1: 대화 학습
    PREFERENCE_DETECTED = "preference_detected"
    PREFERENCE_UPDATED = "preference_updated"

    # 축 2: 피드백 루프
    CORRECTION_DETECTED = "correction_detected"
    PRAISE_DETECTED = "praise_detected"
    COMPLAINT_DETECTED = "complaint_detected"

    # 축 3: 도구 최적화
    TOOL_SUCCESS = "tool_success"
    TOOL_FAILURE = "tool_failure"
    TOOL_PRIORITY_ADJUSTED = "tool_priority_adjusted"

    # 축 4: 프롬프트 자기 개선
    LOW_CONFIDENCE_QUERY = "low_confidence_query"
    KNOWLEDGE_GAP_DETECTED = "knowledge_gap_detected"
    PROMPT_IMPROVEMENT_SUGGESTED = "prompt_improvement_suggested"

    # 축 5: 행동 패턴 예측
    PATTERN_DETECTED = "pattern_detected"
    PATTERN_TRIGGERED = "pattern_triggered"
    PROACTIVE_SUGGESTION_SENT = "proactive_suggestion_sent"


class PreferenceCategory(str, Enum):
    """선호도 카테고리"""

    COMMUNICATION_STYLE = "communication_style"  # 응답 스타일 (간결/상세/격식체 등)
    TOOL_PREFERENCE = "tool_preference"  # 특정 도구/서비스 선호
    DOMAIN_INTEREST = "domain_interest"  # 관심 도메인/분야
    SCHEDULE_PATTERN = "schedule_pattern"  # 일정/시간 관련 패턴
    WORKFLOW_HABIT = "workflow_habit"  # 작업 습관/프로세스
    RESPONSE_FORMAT = "response_format"  # 응답 형식 선호 (코드/표/목록 등)
    LANGUAGE_PREFERENCE = "language_preference"  # 언어/용어 선호


class CorrectionType(str, Enum):
    """교정 유형"""

    FACTUAL_ERROR = "factual_error"  # 사실 오류 (잘못된 정보)
    STYLE_CORRECTION = "style_correction"  # 스타일 교정 (답변 형식/길이/톤)
    TOOL_MISUSE = "tool_misuse"  # 도구 오용 (잘못된 도구 선택)
    MISSING_CONTEXT = "missing_context"  # 컨텍스트 누락 (알고 있어야 할 정보)
    WRONG_ASSUMPTION = "wrong_assumption"  # 잘못된 가정
    INCOMPLETE_RESPONSE = "incomplete_response"  # 불완전한 응답
    EXCESSIVE_RESPONSE = "excessive_response"  # 과도한 응답


class PatternType(str, Enum):
    """행동 패턴 유형"""

    TEMPORAL = "temporal"  # 시간 기반 (매일 아침 비용 확인 등)
    SEQUENTIAL = "sequential"  # 순서 기반 (A 후 항상 B를 요청)
    CONTEXTUAL = "contextual"  # 상황 기반 (월요일에 항상 브리핑 요청)
    REACTIVE = "reactive"  # 반응 기반 (에러 발생 시 항상 로그 확인)


# === Core Models ===


class Preference(BaseModel):
    """대화에서 감지된 사용자 선호 (축 1: 대화 학습)

    cheap 모델이 대화를 분석하여 추출한 선호도 정보
    confidence가 임계값 이상이면 메모리에 자동 upsert

    Attributes:
        category: 선호도 카테고리
        key: 선호 항목 식별자 (예: "response_length" / "preferred_search_tool")
        value: 선호 값 (예: "concise" / "naver_search")
        confidence: 감지 신뢰도 (0.0~1.0)
        evidence: 감지 근거 (대화 발췌)
        source_query: 감지 시점의 사용자 쿼리
        detected_at: 감지 시각
    """

    category: PreferenceCategory
    key: str = Field(min_length=1, max_length=100, description="선호 항목 식별자")
    value: str = Field(min_length=1, max_length=500, description="선호 값")
    confidence: float = Field(ge=0.0, le=1.0, description="감지 신뢰도")
    evidence: str = Field(max_length=500, default="", description="감지 근거")
    source_query: str = Field(max_length=500, default="", description="원본 쿼리")
    detected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    @field_validator("key")
    @classmethod
    def normalize_key(cls, v: str) -> str:
        return v.strip().lower().replace(" ", "_")


class CorrectionRecord(BaseModel):
    """교정/피드백 기록 (축 2: 피드백 루프)

    사용자의 교정/칭찬/불만을 감지하여 기록
    같은 실수 반복 방지를 위해 correction_log 이벤트로 저장

    Attributes:
        correction_type: 교정 유형
        original_response: ARIA의 원래 응답 (요약)
        user_feedback: 사용자의 피드백 원문 (요약)
        corrected_behavior: 학습해야 할 올바른 행동
        domain: 관련 도메인 (메모리 토픽과 매핑)
        severity: 심각도 (minor/major/critical)
        query_context: 원래 쿼리 (컨텍스트)
        detected_at: 감지 시각
    """

    correction_type: CorrectionType
    original_response: str = Field(max_length=1000, default="", description="원래 응답 요약")
    user_feedback: str = Field(max_length=1000, description="사용자 피드백 요약")
    corrected_behavior: str = Field(max_length=1000, description="학습해야 할 올바른 행동")
    domain: str = Field(max_length=64, default="", description="관련 도메인")
    severity: str = Field(default="minor", description="심각도 (minor/major/critical)")
    query_context: str = Field(max_length=500, default="", description="원래 쿼리")
    detected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("minor", "major", "critical"):
            raise ValueError(f"유효하지 않은 severity: '{v}' (허용: minor/major/critical)")
        return v


class ToolMetrics(BaseModel):
    """도구 사용 통계 (축 3: 도구 사용 최적화)

    도구별 성공/실패 집계 — 우선순위 자동 조정의 근거
    EventStore에서 tool_success/tool_failure 이벤트를 집계하여 생성

    Attributes:
        tool_name: 도구 이름
        success_count: 성공 횟수
        failure_count: 실패 횟수
        timeout_count: 타임아웃 횟수
        avg_latency_ms: 평균 응답시간 (밀리초)
        last_used_at: 마지막 사용 시각
        error_patterns: 빈번한 에러 패턴 (상위 3개)
        priority_score: 계산된 우선순위 점수 (0.0~1.0)
    """

    tool_name: str = Field(min_length=1, max_length=100)
    success_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    timeout_count: int = Field(default=0, ge=0)
    avg_latency_ms: float = Field(default=0.0, ge=0.0)
    last_used_at: datetime | None = None
    error_patterns: list[str] = Field(default_factory=list, max_length=5)
    priority_score: float = Field(default=0.5, ge=0.0, le=1.0)

    @property
    def total_calls(self) -> int:
        return self.success_count + self.failure_count

    @property
    def success_rate(self) -> float:
        """성공률 (0.0~1.0 / 호출 없으면 0.0)"""
        total = self.total_calls
        if total == 0:
            return 0.0
        return self.success_count / total

    def calculate_priority(self) -> float:
        """우선순위 점수 계산

        공식: success_rate * 0.6 + recency_bonus * 0.2 + speed_bonus * 0.2
        - success_rate: 성공률 (가중치 60%)
        - recency_bonus: 최근 사용 여부 (가중치 20% / 7일 이내 = 1.0)
        - speed_bonus: 응답 속도 (가중치 20% / 1초 이내 = 1.0)
        """
        if self.total_calls == 0:
            return 0.5  # 기본값 — 미사용 도구는 중립

        # 성공률 (60%)
        sr = self.success_rate

        # 최근 사용 보너스 (20%) — 7일 이내면 1.0 / 30일 이상이면 0.0
        recency = 0.0
        if self.last_used_at:
            days_ago = (datetime.now(timezone.utc) - self.last_used_at).total_seconds() / 86400
            recency = max(0.0, min(1.0, 1.0 - (days_ago / 30.0)))

        # 속도 보너스 (20%) — 1초 이하면 1.0 / 10초 이상이면 0.0
        speed = max(0.0, min(1.0, 1.0 - (self.avg_latency_ms / 10000.0)))

        score = sr * 0.6 + recency * 0.2 + speed * 0.2
        self.priority_score = round(score, 4)
        return self.priority_score


class DomainInsight(BaseModel):
    """저신뢰 도메인 분석 결과 (축 4: 프롬프트 자기 개선)

    confidence가 낮은 쿼리들을 도메인별로 분석하여
    지식 보강이 필요한 영역과 프롬프트 개선 방향 제시

    Attributes:
        domain: 분석 도메인 (예: "legal" / "medical" / "finance")
        avg_confidence: 평균 confidence
        query_count: 해당 도메인 쿼리 수
        sample_queries: 대표 쿼리 (최대 5개)
        knowledge_gaps: 식별된 지식 공백
        improvement_suggestions: 프롬프트 개선 제안
        analyzed_at: 분석 시각
    """

    domain: str = Field(min_length=1, max_length=100)
    avg_confidence: float = Field(ge=0.0, le=1.0)
    query_count: int = Field(ge=1)
    sample_queries: list[str] = Field(default_factory=list, max_length=5)
    knowledge_gaps: list[str] = Field(default_factory=list, max_length=10)
    improvement_suggestions: list[str] = Field(default_factory=list, max_length=5)
    analyzed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class BehaviorPattern(BaseModel):
    """반복 행동 패턴 (축 5: 행동 패턴 예측)

    승재의 반복적인 행동을 감지하여 선제적 제안에 활용

    Attributes:
        pattern_type: 패턴 유형
        pattern_id: 고유 식별자 (중복 등록 방지)
        description: 패턴 설명 (사람이 읽을 수 있는 형태)
        trigger: 트리거 조건 (JSON 직렬화 가능)
        frequency: 발생 빈도 (일 기준)
        occurrence_count: 총 발생 횟수
        last_triggered_at: 마지막 트리거 시각
        suggested_action: 선제적 제안 내용
        confidence: 패턴 신뢰도 (0.0~1.0)
        active: 패턴 활성 상태
    """

    pattern_type: PatternType
    pattern_id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    trigger: dict[str, Any] = Field(default_factory=dict)
    frequency: float = Field(default=0.0, ge=0.0, description="일 기준 발생 빈도")
    occurrence_count: int = Field(default=0, ge=0)
    last_triggered_at: datetime | None = None
    suggested_action: str = Field(max_length=500, default="")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    active: bool = Field(default=True)

    @field_validator("pattern_id")
    @classmethod
    def normalize_pattern_id(cls, v: str) -> str:
        return v.strip().lower().replace(" ", "-")

    @property
    def is_reliable(self) -> bool:
        """패턴이 신뢰할 수 있을 만큼 충분히 관측되었는지"""
        return self.occurrence_count >= 3 and self.confidence >= 0.6


# === Analysis Request/Response Models ===


class LearningAnalysisRequest(BaseModel):
    """학습 분석 요청 (내부 호출용)

    ReAct 에이전트의 /v1/query 응답 후 비동기로 학습 분석 실행 시 사용

    Attributes:
        query: 사용자 원본 쿼리
        response: ARIA 응답 (요약)
        confidence: 응답 신뢰도
        tool_calls: 사용된 도구 목록
        scope: 메모리 스코프
        metadata: 추가 메타데이터
    """

    query: str = Field(min_length=1, max_length=5000)
    response: str = Field(max_length=5000, default="")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    scope: str = Field(default="global")
    metadata: dict[str, Any] = Field(default_factory=dict)


class LearningAnalysisResult(BaseModel):
    """학습 분석 결과 (내부 반환용)

    Attributes:
        preferences: 감지된 선호도 목록
        corrections: 감지된 교정 기록 목록
        patterns_updated: 업데이트된 패턴 수
        events_stored: 저장된 이벤트 수
        memory_topics_updated: 업데이트된 메모리 토픽 목록
        analysis_cost_usd: 분석 비용 (USD)
    """

    preferences: list[Preference] = Field(default_factory=list)
    corrections: list[CorrectionRecord] = Field(default_factory=list)
    patterns_updated: int = Field(default=0)
    events_stored: int = Field(default=0)
    memory_topics_updated: list[str] = Field(default_factory=list)
    analysis_cost_usd: float = Field(default=0.0, ge=0.0)
