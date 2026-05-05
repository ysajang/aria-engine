"""ARIA Engine - Weekly Product Report Generator

제품별 주간 건전성 리포트 생성 (Phase 3.5 Step 9)
- build_weekly_report: 모니터링 결과 통합 → Markdown 리포트
- generate_report: ProductConfig 기반 활성 체크 실행 → build 호출
- send_report_telegram: 텔레그램 전송

설계 원칙:
- 각 모니터링 모듈 결과를 직접 받아 포맷팅만 담당 (pull 아님)
- 체크 실행은 generate_report에서 조건부로 수행
- 텔레그램 4096자 제한 대응 (분할 전송)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger()

MAX_TELEGRAM_LENGTH = 4096


def _status_emoji(issues: list[dict]) -> str:
    """이슈 목록에서 최고 심각도 이모지 반환"""
    if not issues:
        return "🟢"
    severities = [i.get("severity", "low") for i in issues]
    if "high" in severities:
        return "🔴"
    if "medium" in severities:
        return "🟡"
    return "🟢"


def _format_section(
    title: str,
    data: dict[str, Any],
    fields: list[tuple[str, str, str]],
) -> str:
    """리포트 섹션 포맷

    Args:
        title: 섹션 제목
        data: 모니터링 결과 dict
        fields: [(key, label, format)] 목록

    Returns:
        Markdown 섹션 텍스트
    """
    issues = data.get("issues", [])
    emoji = _status_emoji(issues)
    lines = [f"\n{emoji} *{title}*"]

    for key, label, fmt in fields:
        val = data.get(key)
        if val is not None:
            if fmt == "int":
                lines.append(f"  {label}: {int(val):,}")
            elif fmt == "pct":
                lines.append(f"  {label}: {float(val):.1f}%")
            elif fmt == "dollar":
                lines.append(f"  {label}: ${float(val):.2f}")
            else:
                lines.append(f"  {label}: {val}")

    if issues:
        lines.append(f"  ⚠️ 이슈 {len(issues)}건")
        for issue in issues[:3]:
            lines.append(f"    · {issue.get('message', '?')[:80]}")

    recs = data.get("recommendations", [])
    if recs:
        lines.append(f"  💡 제안 {len(recs)}건")
        for r in recs[:2]:
            lines.append(f"    · {r[:80]}")

    return "\n".join(lines)


def build_weekly_report(
    product_name: str,
    period_label: str,
    seo: dict[str, Any] | None = None,
    db: dict[str, Any] | None = None,
    dep: dict[str, Any] | None = None,
    payment: dict[str, Any] | None = None,
    behavior: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
    custom_sections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """주간 리포트 빌드

    Args:
        product_name: 제품명 (예: "Testorum")
        period_label: 기간 레이블 (예: "2026-05-01 ~ 2026-05-07")
        seo~cost: 각 모니터링 결과 dict (None이면 스킵)
        custom_sections: 추가 커스텀 섹션 [{title, content}]

    Returns:
        {product_name, period, markdown, sections, total_issues,
         high_issues, total_recommendations, generated_at}
    """
    report: dict[str, Any] = {
        "product_name": product_name,
        "period": period_label,
        "markdown": "",
        "sections": [],
        "total_issues": 0,
        "high_issues": 0,
        "total_recommendations": 0,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    lines = [
        f"📊 *주간 제품 리포트 — {product_name}*",
        f"📅 {period_label}",
    ]

    all_issues: list[dict] = []
    all_recs: list[str] = []

    # SEO 섹션
    if seo:
        section = _format_section("SEO 모니터링", seo, [
            ("total_pages", "검사 페이지", "int"),
            ("healthy_pages", "정상 페이지", "int"),
        ])
        lines.append(section)
        report["sections"].append("seo")
        all_issues.extend(seo.get("issues", []))
        all_recs.extend(seo.get("recommendations", []))

    # DB 섹션
    if db:
        section = _format_section("DB 모니터링", db, [
            ("table_count", "테이블 수", "int"),
            ("total_rows", "전체 행", "int"),
            ("db_size_mb", "DB 크기(MB)", "int"),
        ])
        lines.append(section)
        report["sections"].append("db")
        all_issues.extend(db.get("issues", []))
        all_recs.extend(db.get("recommendations", []))

    # 의존성 섹션
    if dep:
        section = _format_section("의존성 감사", dep, [
            ("total_deps", "전체 패키지", "int"),
            ("vulnerable_count", "취약 패키지", "int"),
        ])
        lines.append(section)
        report["sections"].append("dep")
        all_issues.extend(dep.get("issues", []))
        all_recs.extend(dep.get("recommendations", []))

    # 결제 섹션
    if payment:
        refunds = payment.get("refunds", {})
        failures = payment.get("failures", {})
        churn = payment.get("churn", {})
        section = _format_section("결제 건전성", {
            "refund_rate": refunds.get("refund_rate"),
            "failure_rate": failures.get("failure_rate"),
            "churn_rate": churn.get("churn_rate"),
            "issues": payment.get("all_issues", []),
        }, [
            ("refund_rate", "환불율", "pct"),
            ("failure_rate", "실패율", "pct"),
            ("churn_rate", "이탈률", "pct"),
        ])
        lines.append(section)
        report["sections"].append("payment")
        all_issues.extend(payment.get("all_issues", []))

    # 사용자 행동 섹션
    if behavior:
        overview = behavior.get("overview", {})
        conversion = behavior.get("conversion", {})
        section = _format_section("사용자 행동", {
            "sessions": overview.get("sessions"),
            "bounce_rate": overview.get("bounce_rate"),
            "conversion_rate": conversion.get("current_rate"),
            "issues": behavior.get("all_issues", []),
        }, [
            ("sessions", "세션", "int"),
            ("bounce_rate", "이탈률", "pct"),
            ("conversion_rate", "전환율", "pct"),
        ])
        lines.append(section)
        report["sections"].append("behavior")
        all_issues.extend(behavior.get("all_issues", []))

    # 비용 섹션
    if cost:
        api_costs = cost.get("api_costs", {})
        section = _format_section("비용 현황", {
            "monthly_cost": api_costs.get("monthly_cost"),
            "monthly_pct": api_costs.get("monthly_pct"),
            "issues": cost.get("all_issues", []),
            "recommendations": cost.get("all_recommendations", []),
        }, [
            ("monthly_cost", "LLM 월 비용", "dollar"),
            ("monthly_pct", "한도 대비", "pct"),
        ])
        lines.append(section)
        report["sections"].append("cost")
        all_issues.extend(cost.get("all_issues", []))
        all_recs.extend(cost.get("all_recommendations", []))

    # 커스텀 섹션
    if custom_sections:
        for cs in custom_sections:
            lines.append(f"\n📌 *{cs.get('title', '추가 정보')}*")
            lines.append(f"  {cs.get('content', '')[:200]}")
            report["sections"].append(f"custom:{cs.get('title', '?')}")

    # 총평
    report["total_issues"] = len(all_issues)
    report["high_issues"] = len([i for i in all_issues if i.get("severity") == "high"])
    report["total_recommendations"] = len(all_recs)

    lines.append("\n" + "─" * 30)
    overall = _status_emoji(all_issues)
    lines.append(
        f"{overall} *총평*: 이슈 {report['total_issues']}건 "
        f"(심각 {report['high_issues']}건) / "
        f"제안 {report['total_recommendations']}건"
    )

    report["markdown"] = "\n".join(lines)
    return report


async def send_report_telegram(
    markdown: str,
    bot_token: str,
    chat_id: str,
) -> dict[str, Any]:
    """리포트 텔레그램 전송 (4096자 분할)

    Returns:
        {success, messages_sent, errors}
    """
    from aria.telegram.notifier import send_message

    result: dict[str, Any] = {
        "success": True,
        "messages_sent": 0,
        "errors": [],
    }

    # 4096자 분할
    chunks: list[str] = []
    if len(markdown) <= MAX_TELEGRAM_LENGTH:
        chunks = [markdown]
    else:
        current = ""
        for line in markdown.split("\n"):
            test = current + "\n" + line if current else line
            if len(test) > MAX_TELEGRAM_LENGTH - 100:
                chunks.append(current)
                current = line
            else:
                current = test
        if current:
            chunks.append(current)

    for i, chunk in enumerate(chunks):
        try:
            resp = await send_message(bot_token, chat_id, chunk)
            if resp.get("ok"):
                result["messages_sent"] += 1
            else:
                result["errors"].append(f"chunk {i}: {resp.get('error', 'unknown')}")
                result["success"] = False
        except Exception as e:
            result["errors"].append(f"chunk {i}: {str(e)[:100]}")
            result["success"] = False

    return result


async def generate_report(
    product_name: str,
    period_label: str,
    checks: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """사전 실행된 체크 결과를 받아 리포트 생성

    Args:
        product_name: 제품명
        period_label: 기간 레이블
        checks: {seo, db, dep, payment, behavior, cost} 결과 dict들
            (각 키는 선택적 — None이면 해당 섹션 스킵)

    Returns:
        build_weekly_report 결과
    """
    start = time.monotonic()

    report = build_weekly_report(
        product_name=product_name,
        period_label=period_label,
        seo=checks.get("seo"),
        db=checks.get("db"),
        dep=checks.get("dep"),
        payment=checks.get("payment"),
        behavior=checks.get("behavior"),
        cost=checks.get("cost"),
    )

    report["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)
    return report
