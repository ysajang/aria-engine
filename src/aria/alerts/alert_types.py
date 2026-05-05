"""ARIA Engine - Alert Types

능동 알림 스키마 정의
- AlertLevel: 알림 심각도 (info / warning / critical)
- AlertType: 알림 유형 (비용 / 에러 / confidence / 메모리 등)
- Alert: 발생한 알림 인스턴스
- AlertRule: 알림 규칙 설정
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AlertLevel(str, Enum):
    """알림 심각도"""

    INFO = "info"           # 참고 — 로그만
    WARNING = "warning"     # 주의 — 텔레그램 알림
    CRITICAL = "critical"   # 긴급 — 텔레그램 알림 + 강조


class AlertType(str, Enum):
    """알림 유형"""

    COST_WARNING = "cost_warning"           # 비용 70% 도달
    COST_CRITICAL = "cost_critical"         # 비용 90% 도달
    KILLSWITCH = "killswitch"               # KillSwitch 발동
    CONSECUTIVE_ERRORS = "consecutive_errors"  # 연속 에러 N회
    LOW_CONFIDENCE = "low_confidence"       # 낮은 confidence 응답
    MEMORY_CONFLICT = "memory_conflict"     # 메모리 버전 충돌
    SERVER_ERROR = "server_error"           # 서버 내부 에러
    # --- 서버 모니터링 (v0.3.0) ---
    HEALTH_CHECK_FAILED = "health_check_failed"     # 헬스체크 실패
    TRAFFIC_ANOMALY = "traffic_anomaly"             # 트래픽 이상 감지
    SECURITY_ISSUE = "security_issue"               # 보안 취약점 발견
    ERROR_SPIKE = "error_spike"                     # 에러 로그 급증
    # --- SEO 모니터링 (Phase 3.5) ---
    SEO_ISSUE = "seo_issue"                           # SEO 이슈 발견
    DB_ISSUE = "db_issue"                               # DB 이슈 발견
    DEPENDENCY_VULN = "dependency_vuln"                   # 의존성 취약점 발견
    # --- 결제 이상 감지 (Phase 3.5 Step 6) ---
    PAYMENT_REFUND_SPIKE = "payment_refund_spike"           # 환불율 급증
    PAYMENT_FAILURE_RATE = "payment_failure_rate"             # 결제 실패율 높음
    SUBSCRIPTION_CHURN = "subscription_churn"                 # 구독 이탈률 높음
    # --- 사용자 행동 분석 (Phase 3.5 Step 7) ---
    USER_BOUNCE_RATE = "user_bounce_rate"                     # 이탈률 높음
    USER_CONVERSION_DROP = "user_conversion_drop"             # 전환율 하락
    USER_FUNNEL_BOTTLENECK = "user_funnel_bottleneck"         # 퍼널 병목
    # --- 비용 최적화 (Phase 3.5 Step 8) ---
    INFRA_COST_HIGH = "infra_cost_high"                       # 인프라 사용량 높음 (Vercel/Supabase)
    # --- 추가 모니터링 v2 ---
    FRONTEND_ERROR_SPIKE = "frontend_error_spike"             # 프론트엔드 JS 에러 급증
    WEBHOOK_MISSING = "webhook_missing"                       # 결제 웹훅 누락 감지
    API_CONTRACT_FAIL = "api_contract_fail"                   # API 응답 스키마 위반


# 알림별 텔레그램 이모지 매핑
ALERT_EMOJI: dict[AlertType, str] = {
    AlertType.COST_WARNING: "💰",
    AlertType.COST_CRITICAL: "🔴",
    AlertType.KILLSWITCH: "🚨",
    AlertType.CONSECUTIVE_ERRORS: "⚠️",
    AlertType.LOW_CONFIDENCE: "🤔",
    AlertType.MEMORY_CONFLICT: "🔄",
    AlertType.SERVER_ERROR: "💥",
    AlertType.HEALTH_CHECK_FAILED: "🔌",
    AlertType.TRAFFIC_ANOMALY: "📈",
    AlertType.SECURITY_ISSUE: "🛡️",
    AlertType.ERROR_SPIKE: "📊",
    AlertType.SEO_ISSUE: "🔍",
    AlertType.DB_ISSUE: "🗄️",
    AlertType.DEPENDENCY_VULN: "📦",
    AlertType.PAYMENT_REFUND_SPIKE: "💸",
    AlertType.PAYMENT_FAILURE_RATE: "❌",
    AlertType.SUBSCRIPTION_CHURN: "📉",
    AlertType.USER_BOUNCE_RATE: "🚪",
    AlertType.USER_CONVERSION_DROP: "📉",
    AlertType.USER_FUNNEL_BOTTLENECK: "🔻",
    AlertType.INFRA_COST_HIGH: "💲",
    AlertType.FRONTEND_ERROR_SPIKE: "🖥️",
    AlertType.WEBHOOK_MISSING: "🔗",
    AlertType.API_CONTRACT_FAIL: "📋",
}

# 알림별 기본 쿨다운 (초)
DEFAULT_COOLDOWNS: dict[AlertType, int] = {
    AlertType.COST_WARNING: 3600,       # 1시간
    AlertType.COST_CRITICAL: 1800,      # 30분
    AlertType.KILLSWITCH: 300,          # 5분
    AlertType.CONSECUTIVE_ERRORS: 1800, # 30분
    AlertType.LOW_CONFIDENCE: 3600,     # 1시간
    AlertType.MEMORY_CONFLICT: 3600,    # 1시간
    AlertType.SERVER_ERROR: 600,        # 10분
    AlertType.HEALTH_CHECK_FAILED: 300, # 5분 (cron 5분 주기와 동일)
    AlertType.TRAFFIC_ANOMALY: 900,     # 15분 (cron 15분 주기와 동일)
    AlertType.SECURITY_ISSUE: 86400,    # 24시간 (cron 1일 1회)
    AlertType.ERROR_SPIKE: 1800,        # 30분 (cron 30분 주기와 동일)
    AlertType.SEO_ISSUE: 86400,         # 24시간 (cron 1일 1회)
    AlertType.DB_ISSUE: 3600,            # 1시간
    AlertType.DEPENDENCY_VULN: 86400,    # 24시간 (일 1회 스캔)
    AlertType.PAYMENT_REFUND_SPIKE: 3600,  # 1시간
    AlertType.PAYMENT_FAILURE_RATE: 1800,   # 30분
    AlertType.SUBSCRIPTION_CHURN: 86400,    # 24시간 (변화 느림)
    AlertType.USER_BOUNCE_RATE: 86400,      # 24시간 (일 1회)
    AlertType.USER_CONVERSION_DROP: 86400,   # 24시간
    AlertType.USER_FUNNEL_BOTTLENECK: 86400, # 24시간
    AlertType.INFRA_COST_HIGH: 86400,       # 24시간 (일 1회)
    AlertType.FRONTEND_ERROR_SPIKE: 600,    # 10분 (실시간 감지)
    AlertType.WEBHOOK_MISSING: 300,         # 5분 (결제 누락은 긴급)
    AlertType.API_CONTRACT_FAIL: 3600,      # 1시간
}


class Alert(BaseModel):
    """발생한 알림 인스턴스"""

    alert_type: AlertType
    level: AlertLevel
    title: str = Field(description="알림 제목 (한 줄)")
    message: str = Field(description="알림 상세 메시지")
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def to_telegram(self) -> str:
        """텔레그램 메시지 포맷"""
        emoji = ALERT_EMOJI.get(self.alert_type, "🔔")
        level_tag = ""
        if self.level == AlertLevel.CRITICAL:
            level_tag = " *[긴급]*"
        elif self.level == AlertLevel.WARNING:
            level_tag = " *[주의]*"

        lines = [
            f"{emoji}{level_tag} {self.title}",
            "",
            self.message,
        ]

        # 데이터 요약 (있으면)
        if self.data:
            data_lines = []
            for k, v in self.data.items():
                data_lines.append(f"  {k}: {v}")
            if data_lines:
                lines.append("")
                lines.extend(data_lines)

        lines.append(f"\n_ARIA Alert — {self.alert_type.value}_")
        return "\n".join(lines)
