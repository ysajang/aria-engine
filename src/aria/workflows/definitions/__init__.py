"""ARIA Engine - Workflow Definitions

LLM 불필요 워크플로우 7종:
- competitor_monitor: 경쟁사 모니터링 (#3)
- viral_analysis: 바이럴 분석 (#4)
- revenue_report: 매출/비용 자동 집계 (#6)
- tax_summary: 세금 자료 정리 (#7)
- schedule_manager: 일정 관리 (#8)
- kpi_briefing: 주간 KPI 브리핑 (#9)
- invoice_generator: 인보이스 자동 생성 (#10)

LLM 사용 워크플로우 3종 (Haiku):
- seo_content_pipeline: SEO 콘텐츠 파이프라인 (#1)
- sns_posting: SNS 자동 포스팅 준비 (#2)
- email_marketing: 이메일 마케팅 (#5)
"""

from aria.workflows.definitions.competitor_monitor import build_competitor_monitor
from aria.workflows.definitions.viral_analysis import build_viral_analysis
from aria.workflows.definitions.revenue_report import build_revenue_report
from aria.workflows.definitions.tax_summary import build_tax_summary
from aria.workflows.definitions.schedule_manager import build_schedule_manager
from aria.workflows.definitions.kpi_briefing import build_kpi_briefing
from aria.workflows.definitions.invoice_generator import build_invoice_generator
from aria.workflows.definitions.seo_content_pipeline import build_seo_content_pipeline
from aria.workflows.definitions.sns_posting import build_sns_posting
from aria.workflows.definitions.email_marketing import build_email_marketing

ALL_BUILDERS = [
    build_competitor_monitor,
    build_viral_analysis,
    build_revenue_report,
    build_tax_summary,
    build_schedule_manager,
    build_kpi_briefing,
    build_invoice_generator,
    build_seo_content_pipeline,
    build_sns_posting,
    build_email_marketing,
]

__all__ = [
    "ALL_BUILDERS",
    "build_competitor_monitor",
    "build_viral_analysis",
    "build_revenue_report",
    "build_tax_summary",
    "build_schedule_manager",
    "build_kpi_briefing",
    "build_invoice_generator",
    "build_seo_content_pipeline",
    "build_sns_posting",
    "build_email_marketing",
]
