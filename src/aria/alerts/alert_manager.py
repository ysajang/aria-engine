"""ARIA Engine - Alert Manager

능동 알림 관리자
- 조건 평가 → 알림 생성 → 쿨다운 체크 → 텔레그램 발송
- 비차단: 알림 실패가 메인 로직에 영향 없음
- 쿨다운: 같은 유형 알림 반복 방지

사용법:
    alert_mgr = AlertManager(bot_token, chat_id)

    # 비용 체크 (LLM 호출 후)
    await alert_mgr.check_cost(daily=5.0, monthly=150.0, daily_limit=10.0, monthly_limit=300.0)

    # confidence 체크 (에이전트 응답 후)
    await alert_mgr.check_confidence(confidence=0.3, query="질문 내용")

    # 에러 체크 (예외 발생 시)
    await alert_mgr.check_error(error_type="AgentError", message="에이전트 실패")
"""

from __future__ import annotations

import asyncio
import time
import threading
from typing import Any

import structlog

from aria.alerts.alert_types import (
    Alert,
    AlertLevel,
    AlertType,
    DEFAULT_COOLDOWNS,
)
from aria.telegram.notifier import send_message

logger = structlog.get_logger()


class AlertManager:
    """능동 알림 관리자

    Thread-safe: 쿨다운 타임스탬프 접근 시 lock 사용
    """

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        *,
        enabled: bool = True,
        cooldowns: dict[AlertType, int] | None = None,
        cost_warning_threshold: float = 0.7,   # 70%
        cost_critical_threshold: float = 0.9,   # 90%
        confidence_threshold: float = 0.3,      # 0.3 미만이면 알림
        consecutive_error_threshold: int = 3,   # 3회 연속이면 알림
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._enabled = enabled
        self._cooldowns = {**DEFAULT_COOLDOWNS, **(cooldowns or {})}
        self._cost_warning_threshold = cost_warning_threshold
        self._cost_critical_threshold = cost_critical_threshold
        self._confidence_threshold = confidence_threshold
        self._consecutive_error_threshold = consecutive_error_threshold

        # 쿨다운 추적: {AlertType: last_sent_timestamp}
        self._last_sent: dict[AlertType, float] = {}
        self._lock = threading.Lock()

        # 연속 에러 카운터
        self._consecutive_errors: int = 0

        # 발송 이력 (디버깅/통계용)
        self._sent_count: int = 0

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._bot_token) and bool(self._chat_id)

    @property
    def sent_count(self) -> int:
        return self._sent_count

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    def reset_error_counter(self) -> None:
        """성공 시 에러 카운터 리셋"""
        self._consecutive_errors = 0

    # === 비용 알림 ===

    async def check_cost(
        self,
        daily: float,
        monthly: float,
        daily_limit: float,
        monthly_limit: float,
    ) -> Alert | None:
        """비용 임계치 체크 → 초과 시 알림

        Returns:
            발송된 Alert 또는 None (미발송)
        """
        if not self.enabled:
            return None

        # 일일 비용 체크
        daily_ratio = daily / daily_limit if daily_limit > 0 else 0
        monthly_ratio = monthly / monthly_limit if monthly_limit > 0 else 0

        # 가장 높은 임계치부터 체크
        if daily_ratio >= self._cost_critical_threshold or monthly_ratio >= self._cost_critical_threshold:
            alert = Alert(
                alert_type=AlertType.COST_CRITICAL,
                level=AlertLevel.CRITICAL,
                title="비용 90% 초과",
                message=(
                    f"일일: ${daily:.4f} / ${daily_limit} ({daily_ratio:.0%})\n"
                    f"월간: ${monthly:.4f} / ${monthly_limit} ({monthly_ratio:.0%})"
                ),
                data={"daily_usd": round(daily, 4), "monthly_usd": round(monthly, 4)},
            )
            return await self._send_alert(alert)

        if daily_ratio >= self._cost_warning_threshold or monthly_ratio >= self._cost_warning_threshold:
            alert = Alert(
                alert_type=AlertType.COST_WARNING,
                level=AlertLevel.WARNING,
                title="비용 70% 도달",
                message=(
                    f"일일: ${daily:.4f} / ${daily_limit} ({daily_ratio:.0%})\n"
                    f"월간: ${monthly:.4f} / ${monthly_limit} ({monthly_ratio:.0%})"
                ),
                data={"daily_usd": round(daily, 4), "monthly_usd": round(monthly, 4)},
            )
            return await self._send_alert(alert)

        return None

    async def check_killswitch(
        self,
        daily_cost: float,
        monthly_cost: float,
    ) -> Alert | None:
        """KillSwitch 발동 알림"""
        if not self.enabled:
            return None

        alert = Alert(
            alert_type=AlertType.KILLSWITCH,
            level=AlertLevel.CRITICAL,
            title="KillSwitch 발동 — API 호출 차단됨",
            message=(
                f"비용 상한 초과로 모든 LLM 호출이 차단되었습니다\n"
                f"일일: ${daily_cost:.4f} / 월간: ${monthly_cost:.4f}\n\n"
                f"조치: .env에서 한도 조정 또는 서버 재시작"
            ),
            data={"daily_usd": round(daily_cost, 4), "monthly_usd": round(monthly_cost, 4)},
        )
        return await self._send_alert(alert)

    # === Confidence 알림 ===

    async def check_confidence(
        self,
        confidence: float,
        query: str,
    ) -> Alert | None:
        """낮은 confidence 알림

        Args:
            confidence: 에이전트 응답 confidence (0.0~1.0)
            query: 원본 질문 (일부만 포함)
        """
        if not self.enabled:
            return None

        if confidence >= self._confidence_threshold:
            return None

        alert = Alert(
            alert_type=AlertType.LOW_CONFIDENCE,
            level=AlertLevel.WARNING,
            title=f"낮은 응답 신뢰도 ({confidence:.2f})",
            message=(
                f"질문: {query[:100]}{'...' if len(query) > 100 else ''}\n"
                f"confidence: {confidence:.2f} (임계값: {self._confidence_threshold})\n\n"
                f"검색 결과 부족 또는 도메인 지식 부재 가능성"
            ),
            data={"confidence": confidence, "query_preview": query[:50]},
        )
        return await self._send_alert(alert)

    # === 에러 알림 ===

    async def check_error(
        self,
        error_type: str,
        message: str,
    ) -> Alert | None:
        """에러 발생 시 카운터 증가 → 연속 N회 시 알림

        Returns:
            연속 에러 임계치 도달 시 Alert / 미도달 시 None
        """
        if not self.enabled:
            return None

        self._consecutive_errors += 1

        if self._consecutive_errors >= self._consecutive_error_threshold:
            alert = Alert(
                alert_type=AlertType.CONSECUTIVE_ERRORS,
                level=AlertLevel.CRITICAL,
                title=f"연속 에러 {self._consecutive_errors}회 발생",
                message=(
                    f"유형: {error_type}\n"
                    f"최근 에러: {message[:200]}\n\n"
                    f"서버 상태 점검이 필요합니다"
                ),
                data={
                    "error_type": error_type,
                    "consecutive_count": self._consecutive_errors,
                },
            )
            return await self._send_alert(alert)

        return None

    # === 메모리 충돌 알림 ===

    async def check_memory_conflict(
        self,
        scope: str,
        domain: str,
        expected_version: int,
        actual_version: int,
    ) -> Alert | None:
        """메모리 버전 충돌 알림"""
        if not self.enabled:
            return None

        alert = Alert(
            alert_type=AlertType.MEMORY_CONFLICT,
            level=AlertLevel.WARNING,
            title=f"메모리 충돌: {scope}/{domain}",
            message=(
                f"기대 버전: v{expected_version} / 실제 버전: v{actual_version}\n"
                f"동시 쓰기 또는 외부 수정 감지"
            ),
            data={
                "scope": scope,
                "domain": domain,
                "expected": expected_version,
                "actual": actual_version,
            },
        )
        return await self._send_alert(alert)

    # === 서버 에러 알림 ===

    async def check_server_error(
        self,
        path: str,
        error: str,
    ) -> Alert | None:
        """서버 내부 에러 (500) 알림"""
        if not self.enabled:
            return None

        alert = Alert(
            alert_type=AlertType.SERVER_ERROR,
            level=AlertLevel.CRITICAL,
            title="서버 내부 에러 발생",
            message=(
                f"경로: {path}\n"
                f"에러: {error[:200]}"
            ),
            data={"path": path},
        )
        return await self._send_alert(alert)

    # === 통계 ===

    def get_stats(self) -> dict[str, Any]:
        """알림 통계"""
        with self._lock:
            last_sent_info = {
                k.value: time.time() - v
                for k, v in self._last_sent.items()
            }

        return {
            "enabled": self.enabled,
            "sent_count": self._sent_count,
            "consecutive_errors": self._consecutive_errors,
            "cooldowns": {k.value: v for k, v in self._cooldowns.items()},
            "seconds_since_last": last_sent_info,
        }

    # === 서버 모니터링 알림 ===

    async def check_health(
        self,
        url: str,
        status: str,
        status_code: int = 0,
        response_time_ms: float = 0.0,
        ssl_expiry_days: int | None = None,
        error: str | None = None,
    ) -> Alert | None:
        """헬스체크 실패 알림

        Args:
            url: 체크한 URL
            status: 상태 (healthy / unreachable / timeout / server_error)
            status_code: HTTP 상태코드
            response_time_ms: 응답시간 (ms)
            ssl_expiry_days: SSL 만료까지 남은 일수
            error: 에러 메시지
        """
        if not self.enabled:
            return None

        # 정상이면 SSL 만료 경고만 체크
        if status == "healthy":
            if ssl_expiry_days is not None and ssl_expiry_days < 14:
                alert = Alert(
                    alert_type=AlertType.HEALTH_CHECK_FAILED,
                    level=AlertLevel.WARNING,
                    title=f"SSL 인증서 곧 만료 — {url}",
                    message=(
                        f"SSL 만료까지 {ssl_expiry_days}일 남음\n"
                        f"갱신이 필요합니다"
                    ),
                    data={"url": url, "ssl_expiry_days": ssl_expiry_days},
                )
                return await self._send_alert(alert)
            return None

        # 비정상 상태
        detail_parts = [f"상태: {status}"]
        if status_code:
            detail_parts.append(f"HTTP {status_code}")
        if response_time_ms > 0:
            detail_parts.append(f"응답시간: {response_time_ms:.0f}ms")
        if error:
            detail_parts.append(f"에러: {error[:150]}")

        level = AlertLevel.CRITICAL if status in ("unreachable", "timeout") else AlertLevel.WARNING

        alert = Alert(
            alert_type=AlertType.HEALTH_CHECK_FAILED,
            level=level,
            title=f"서버 다운 감지 — {url}",
            message="\n".join(detail_parts),
            data={"url": url, "status": status, "status_code": status_code},
        )
        return await self._send_alert(alert)

    async def check_traffic_anomaly(
        self,
        log_path: str,
        current_rpm: float,
        baseline_rpm: float,
        ratio: float,
        top_ips: list[dict] | None = None,
    ) -> Alert | None:
        """트래픽 이상 알림"""
        if not self.enabled:
            return None

        level = AlertLevel.CRITICAL if ratio >= 5.0 else AlertLevel.WARNING
        ip_info = ""
        if top_ips:
            ip_info = "\n상위 IP: " + " / ".join(
                f"{ip['ip']}({ip['count']})" for ip in top_ips[:3]
            )

        alert = Alert(
            alert_type=AlertType.TRAFFIC_ANOMALY,
            level=level,
            title=f"트래픽 이상 감지 ({ratio:.1f}x)",
            message=(
                f"현재: {current_rpm:.1f} rpm / 평균: {baseline_rpm:.1f} rpm\n"
                f"비율: {ratio:.1f}x (임계치 초과){ip_info}"
            ),
            data={
                "current_rpm": current_rpm,
                "baseline_rpm": baseline_rpm,
                "ratio": ratio,
            },
        )
        return await self._send_alert(alert)

    async def check_security_issue(
        self,
        url: str,
        headers_score: int,
        headers_max_score: int,
        issues: list[dict],
    ) -> Alert | None:
        """보안 취약점 알림 (high severity 이슈가 있을 때만)"""
        if not self.enabled:
            return None

        high_issues = [i for i in issues if i.get("severity") == "high"]
        if not high_issues:
            return None

        issue_summary = "\n".join(
            f"• {i['detail']}" for i in high_issues[:5]
        )

        alert = Alert(
            alert_type=AlertType.SECURITY_ISSUE,
            level=AlertLevel.CRITICAL if len(high_issues) >= 3 else AlertLevel.WARNING,
            title=f"보안 취약점 발견 — {url}",
            message=(
                f"보안 점수: {headers_score}/{headers_max_score}\n"
                f"고위험 이슈 {len(high_issues)}건:\n{issue_summary}"
            ),
            data={
                "url": url,
                "score": f"{headers_score}/{headers_max_score}",
                "high_issues": len(high_issues),
            },
        )
        return await self._send_alert(alert)

    async def check_error_spike(
        self,
        log_path: str,
        error_count: int,
        top_errors: list[dict] | None = None,
    ) -> Alert | None:
        """에러 로그 급증 알림"""
        if not self.enabled:
            return None

        if error_count < 10:
            return None

        level = AlertLevel.CRITICAL if error_count >= 50 else AlertLevel.WARNING
        error_detail = ""
        if top_errors:
            error_detail = "\n주요 에러:\n" + "\n".join(
                f"  {e['message'][:80]} ({e['count']}회)" for e in top_errors[:3]
            )

        alert = Alert(
            alert_type=AlertType.ERROR_SPIKE,
            level=level,
            title=f"에러 급증 — {error_count}건",
            message=(
                f"로그: {log_path}\n"
                f"최근 분석 구간에서 에러 {error_count}건 감지{error_detail}"
            ),
            data={"log_path": log_path, "error_count": error_count},
        )
        return await self._send_alert(alert)

    # === Internal ===

    def _is_in_cooldown(self, alert_type: AlertType) -> bool:
        """쿨다운 기간 내인지 확인"""
        with self._lock:
            last = self._last_sent.get(alert_type)
            if last is None:
                return False

            cooldown = self._cooldowns.get(alert_type, 3600)
            return (time.time() - last) < cooldown

    def _mark_sent(self, alert_type: AlertType) -> None:
        """발송 시간 기록"""
        with self._lock:
            self._last_sent[alert_type] = time.time()
            self._sent_count += 1

    async def _send_alert(self, alert: Alert) -> Alert | None:
        """알림 발송 (쿨다운 체크 + 텔레그램 전송)

        Returns:
            발송 성공 시 Alert / 쿨다운 또는 실패 시 None
        """
        # 쿨다운 체크
        if self._is_in_cooldown(alert.alert_type):
            logger.debug(
                "alert_cooldown",
                alert_type=alert.alert_type.value,
            )
            return None

        # 텔레그램 발송
        text = alert.to_telegram()
        try:
            result = await send_message(
                self._bot_token,
                self._chat_id,
                text,
            )
            if result.get("ok"):
                self._mark_sent(alert.alert_type)
                logger.info(
                    "alert_sent",
                    alert_type=alert.alert_type.value,
                    level=alert.level.value,
                )
                return alert
            else:
                logger.warning(
                    "alert_send_failed",
                    alert_type=alert.alert_type.value,
                    error=result.get("error", "unknown"),
                )
                return None
        except Exception as e:
            # 알림 실패는 절대 메인 로직에 영향 주지 않음
            logger.warning(
                "alert_send_error",
                alert_type=alert.alert_type.value,
                error=str(e)[:200],
            )
            return None
