"""ARIA Engine - Self-Learning System (Phase 5)

5가지 학습 축:
1. ConversationLearner — 대화에서 선호/패턴 자동 감지 → 메모리 토픽 자동 upsert
2. FeedbackLoop — 교정/칭찬/불만 감지 → correction_log 이벤트 → 반복 실수 방지
3. ToolOptimizer — 도구별 성공/실패율 축적 → 자동 우선순위 조정
4. PromptImprover — confidence 낮은 도메인 패턴 분석 → 지식 보강/프롬프트 개선 제안
5. PatternPredictor — 승재의 반복 패턴 감지 → 선제적 제안

공통 인프라:
- types.py — 학습 이벤트/분석 결과 스키마
- base.py — BaseLearner ABC (cheap 모델 분석 공통 로직)
"""

from aria.learning.types import (
    CorrectionRecord,
    CorrectionType,
    DomainInsight,
    LearningEventType,
    BehaviorPattern,
    PatternType,
    Preference,
    PreferenceCategory,
    ToolMetrics,
)
from aria.learning.base import BaseLearner
from aria.learning.conversation_learner import ConversationLearner
from aria.learning.feedback_loop import FeedbackLoop
from aria.learning.tool_optimizer import ToolOptimizer
from aria.learning.prompt_improver import PromptImprover
from aria.learning.pattern_predictor import PatternPredictor

__all__ = [
    "BaseLearner",
    "BehaviorPattern",
    "ConversationLearner",
    "CorrectionRecord",
    "CorrectionType",
    "DomainInsight",
    "FeedbackLoop",
    "LearningEventType",
    "PatternPredictor",
    "PatternType",
    "Preference",
    "PreferenceCategory",
    "PromptImprover",
    "ToolMetrics",
    "ToolOptimizer",
]
