"""ARIA Engine - Product Connector

범용 제품 연동 모듈 (Phase 3.5)
어떤 제품이든 register_product() 한 번이면 자동 감시+분석+보고

핵심 원칙:
- Exit-Safe: 제품 코드에 ARIA 코드 탑재 금지 / API 호출만
- Scope 격리: 제품별 메모리/이벤트 완전 분리
- 탈부착: register_product() / unregister_product()
- 비차단: 모니터링 실패가 제품 서비스에 영향 없음
"""

from aria.products.types import (
    ProductConfig,
    ProductFeature,
    ProductStatus,
    ProductSummary,
)
from aria.products.registry import ProductRegistry

__all__ = [
    "ProductConfig",
    "ProductFeature",
    "ProductRegistry",
    "ProductStatus",
    "ProductSummary",
]
