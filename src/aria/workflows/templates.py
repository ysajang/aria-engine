"""ARIA Engine - Template Engine

Jinja2 기반 출력물 템플릿 시스템
- 파일 기반 템플릿 (templates/ 디렉토리)
- 인라인 문자열 템플릿
- 내장 필터: datetime_kst, number_format, truncate_smart
- 마케팅/행정 공통 템플릿 내장

LLM 비종속 원칙: 고정 출력물은 반드시 템플릿 사용
LLM은 동적 콘텐츠 생성에만 사용 (Haiku)
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

# KST = UTC+9
KST = timezone(timedelta(hours=9))


def _datetime_kst(value: str | datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """UTC ISO 문자열 → KST 포맷팅"""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return str(value)
    if isinstance(value, datetime):
        kst_dt = value.astimezone(KST)
        return kst_dt.strftime(fmt)
    return str(value)


def _number_format(value: float | int, decimals: int = 0) -> str:
    """숫자 천 단위 콤마 포맷팅"""
    if isinstance(value, float):
        return f"{value:,.{decimals}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _usd(value: float | int, decimals: int = 4) -> str:
    """USD 금액 포맷팅"""
    return f"${float(value):,.{decimals}f}"


def _truncate_smart(value: str, length: int = 100) -> str:
    """한국어/영어 스마트 말줄임"""
    if len(value) <= length:
        return value
    return value[: length - 3].rstrip() + "..."


def _severity_emoji(severity: str) -> str:
    """심각도 이모지 변환"""
    mapping = {
        "info": "ℹ️",
        "warning": "⚠️",
        "error": "🔴",
        "critical": "🚨",
        "success": "✅",
        "low": "🟢",
        "medium": "🟡",
        "high": "🔴",
    }
    return mapping.get(severity.lower(), "❓")


class TemplateEngine:
    """Jinja2 기반 템플릿 엔진

    Args:
        templates_dir: 파일 기반 템플릿 디렉토리 (기본: ./templates)
    """

    def __init__(self, templates_dir: str = "./templates") -> None:
        try:
            import jinja2
        except ImportError as e:
            raise ImportError(
                "Jinja2가 필요합니다: pip install jinja2"
            ) from e

        self._templates_dir = Path(templates_dir)
        loaders = []

        # 파일 기반 로더 (디렉토리 존재 시)
        if self._templates_dir.exists():
            loaders.append(jinja2.FileSystemLoader(str(self._templates_dir)))

        # 내장 템플릿 로더
        loaders.append(jinja2.DictLoader(BUILTIN_TEMPLATES))

        self._env = jinja2.Environment(
            loader=jinja2.ChoiceLoader(loaders),
            autoescape=False,
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

        # 커스텀 필터 등록
        self._env.filters["datetime_kst"] = _datetime_kst
        self._env.filters["number_format"] = _number_format
        self._env.filters["usd"] = _usd
        self._env.filters["truncate_smart"] = _truncate_smart
        self._env.filters["severity_emoji"] = _severity_emoji

    def render(self, template_name: str, context: dict[str, Any]) -> str:
        """파일/내장 템플릿 렌더링

        Args:
            template_name: 템플릿 이름 (예: "marketing/competitor_report.md.j2")
            context: 템플릿 변수

        Returns:
            렌더링된 문자열
        """
        try:
            template = self._env.get_template(template_name)
            result = template.render(**context)
            logger.debug(
                "template_rendered",
                template=template_name,
                output_length=len(result),
            )
            return result
        except Exception as e:
            logger.error(
                "template_render_failed",
                template=template_name,
                error=str(e)[:300],
            )
            raise

    def render_string(self, template_str: str, context: dict[str, Any]) -> str:
        """인라인 문자열 템플릿 렌더링

        Args:
            template_str: Jinja2 템플릿 문자열
            context: 템플릿 변수

        Returns:
            렌더링된 문자열
        """
        try:
            import jinja2

            template = self._env.from_string(template_str)
            result = template.render(**context)
            return result
        except Exception as e:
            logger.error(
                "template_string_render_failed",
                error=str(e)[:300],
            )
            raise

    def has_template(self, template_name: str) -> bool:
        """템플릿 존재 여부 확인"""
        try:
            self._env.get_template(template_name)
            return True
        except Exception:
            return False

    def list_templates(self) -> list[str]:
        """사용 가능한 모든 템플릿 목록"""
        return sorted(self._env.list_templates())


# ============================================================
# 내장 템플릿 (파일 없이도 바로 사용 가능)
# ============================================================

BUILTIN_TEMPLATES: dict[str, str] = {
    # --- 마케팅 ---

    "marketing/competitor_report.md.j2": """\
🔍 *경쟁사 모니터링 리포트*
📅 {{ now | datetime_kst }}

{% for item in changes %}
---
*{{ item.competitor }}*
변화: {{ item.summary | truncate_smart(150) }}
소스: {{ item.source }}
발견: {{ item.detected_at | datetime_kst }}
{% endfor %}

{% if not changes %}
변화 감지 없음
{% endif %}
_총 {{ changes | length }}건 감지_
""",

    "marketing/viral_report.md.j2": """\
📊 *Testorum 바이럴 분석 리포트*
📅 {{ period }}

*핵심 지표*
  완료: {{ stats.completed | number_format }}건
  공유: {{ stats.shared | number_format }}건
  이탈: {{ stats.dropped | number_format }}건
  공유율: {{ "%.1f" | format(stats.share_rate * 100) }}%

{% if top_tests %}
*인기 테스트 TOP 3*
{% for t in top_tests[:3] %}
  {{ loop.index }}. {{ t.name }} — {{ t.completions | number_format }}회 완료
{% endfor %}
{% endif %}

{% if drop_points %}
*이탈 구간*
{% for d in drop_points[:3] %}
  ⚠️ {{ d.test_name }} Q{{ d.question_index }} — {{ "%.0f" | format(d.drop_rate * 100) }}% 이탈
{% endfor %}
{% endif %}
""",

    "marketing/sns_draft.md.j2": """\
{% if platform == "twitter" %}\
{{ content | truncate_smart(270) }}

{{ hashtags }}
{% elif platform == "instagram" %}\
{{ content }}

---
{{ hashtags }}
{% else %}\
{{ content }}

{{ hashtags }}
{% endif %}\
""",

    "marketing/seo_blog_outline.md.j2": """\
# {{ title }}

## 개요
{{ summary }}

## 키워드
{{ keywords | join(", ") }}

## 본문 구조
{% for section in sections %}
### {{ section.heading }}
{{ section.brief }}
{% endfor %}

## SEO 메타
- title: {{ seo_title | truncate_smart(60) }}
- description: {{ seo_description | truncate_smart(155) }}
""",

    # --- 행정 ---

    "admin/kpi_briefing.md.j2": """\
📋 *주간 KPI 브리핑*
📅 {{ period }}

{% for product in products %}
*{{ product.name }}*
{% for kpi in product.kpis %}
  {{ kpi.label }}: {{ kpi.value }}{% if kpi.change %} ({{ kpi.change }}){% endif %}
{% endfor %}

{% endfor %}

*비용*
  일 평균: {{ daily_avg | usd }}
  주간 합계: {{ weekly_total | usd }}
  월 누적: {{ monthly_total | usd }} / {{ monthly_limit | usd }}
""",

    "admin/invoice.md.j2": """\
## 인보이스

발행일: {{ issue_date | datetime_kst("%Y-%m-%d") }}
인보이스 번호: {{ invoice_number }}

*발행인*
{{ issuer.name }}
{{ issuer.business_number }}
{{ issuer.address }}

*수신인*
{{ recipient.name }}
{{ recipient.email }}

---

| 항목 | 수량 | 단가 | 금액 |
|------|------|------|------|
{% for item in items %}
| {{ item.description }} | {{ item.quantity }} | {{ item.unit_price | number_format }} | {{ item.total | number_format }} |
{% endfor %}

---
합계: {{ total | number_format }}원
{% if tax %}
부가세(10%): {{ tax | number_format }}원
총액: {{ grand_total | number_format }}원
{% endif %}

{{ payment_info }}
""",

    "admin/tax_summary.md.j2": """\
📋 *{{ period }} 세금 자료 정리*
사업자: {{ business.name }} ({{ business.number }})
과세유형: {{ business.tax_type }}

*매출 집계*
{% for cat in revenue_categories %}
  {{ cat.name }}: {{ cat.amount | number_format }}원 ({{ cat.count }}건)
{% endfor %}
  합계: {{ total_revenue | number_format }}원

*비용 집계*
{% for cat in expense_categories %}
  {{ cat.name }}: {{ cat.amount | number_format }}원
{% endfor %}
  합계: {{ total_expense | number_format }}원

*과세 요약*
  과세표준: {{ taxable_income | number_format }}원
  {% if vat %}부가세 예상: {{ vat | number_format }}원{% endif %}

_이 자료는 참고용이며 실제 신고는 세무사와 확인 필요_
""",

    "admin/schedule_confirm.md.j2": """\
📅 *일정 등록 완료*
{{ title }}
시작: {{ start | datetime_kst }}
{% if end is defined and end %}종료: {{ end | datetime_kst }}{% endif %}
{% if location is defined and location %}장소: {{ location }}{% endif %}
{% if description is defined and description %}메모: {{ description | truncate_smart(100) }}{% endif %}
""",

    # --- 공통 ---

    "common/event_summary.md.j2": """\
📊 *이벤트 요약*
소스: {{ source }}
기간: {{ since | datetime_kst }} ~ {{ until | datetime_kst }}

{% for event_type, count in event_counts.items() %}
  {{ event_type }}: {{ count | number_format }}건
{% endfor %}
합계: {{ total | number_format }}건
""",

    "common/workflow_notification.md.j2": """\
{% if success %}\
✅ *워크플로우 완료*
{% else %}\
❌ *워크플로우 실패*
{% endif %}\
ID: `{{ workflow_id }}`
이름: {{ workflow_name }}
스텝: {{ steps_completed }}/{{ steps_total }}
시간: {{ duration_ms | int }}ms
{% if error %}에러: {{ error | truncate_smart(200) }}{% endif %}
""",
}
