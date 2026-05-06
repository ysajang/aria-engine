"""ARIA Engine - Workflow Definitions

LLM 불필요 워크플로우 6종:
- competitor_monitor: 경쟁사 모니터링 (#3)
- viral_analysis: 바이럴 분석 (#4)
- tax_summary: 세금 자료 정리 (#7)
- schedule_manager: 일정 관리 (#8)
- kpi_briefing: 주간 KPI 브리핑 (#9)
- invoice_generator: 인보이스 자동 생성 (#10)
"""

from aria.workflows.definitions.competitor_monitor import build_competitor_monitor
from aria.workflows.definitions.viral_analysis import build_viral_analysis
from aria.workflows.definitions.tax_summary import build_tax_summary
from aria.workflows.definitions.schedule_manager import build_schedule_manager
from aria.workflows.definitions.kpi_briefing import build_kpi_briefing
from aria.workflows.definitions.invoice_generator import build_invoice_generator

ALL_BUILDERS = [
    build_competitor_monitor,
    build_viral_analysis,
    build_tax_summary,
    build_schedule_manager,
    build_kpi_briefing,
    build_invoice_generator,
]

__all__ = [
    "ALL_BUILDERS",
    "build_competitor_monitor",
    "build_viral_analysis",
    "build_tax_summary",
    "build_schedule_manager",
    "build_kpi_briefing",
    "build_invoice_generator",
]
