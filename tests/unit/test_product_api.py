"""ARIA Engine - Product API Integration Tests

Product Connector Step 2 테스트
- app.py lifespan에서 ProductRegistry 초기화
- Product API 엔드포인트 5개 (POST/GET/PATCH/DELETE)
- 이벤트 소스 동적 연동
- 모니터링 알림에 제품명 포함
- _resolve_product_label 헬퍼
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.products.types import (
    ProductConfig,
    ProductFeature,
    ProductRegisterRequest,
    ProductStatus,
    ProductUpdateRequest,
)
from aria.products.registry import (
    ProductAlreadyExistsError,
    ProductNotFoundError,
    ProductRegistry,
)


# === Fixtures ===


@pytest.fixture
def tmp_product_dirs(tmp_path: Path):
    """임시 products + memory 디렉토리"""
    products_dir = tmp_path / "products"
    memory_dir = tmp_path / "memory"
    products_dir.mkdir()
    memory_dir.mkdir()
    return products_dir, memory_dir


@pytest.fixture
def product_registry(tmp_product_dirs):
    """테스트용 ProductRegistry"""
    products_dir, memory_dir = tmp_product_dirs
    return ProductRegistry(
        base_path=str(products_dir),
        memory_base_path=str(memory_dir),
    )


# ===================================================================
# 1. _resolve_product_label 테스트
# ===================================================================


class TestResolveProductLabel:
    """모니터링 데이터에서 제품명 자동 해석"""

    def test_url_match(self, product_registry: ProductRegistry):
        """URL 도메인 매칭"""
        product_registry.register(ProductConfig(
            id="testorum", name="Testorum",
            urls=["https://testorum.app"],
        ))

        # app.py의 _resolve_product_label 로직 직접 테스트
        from aria.api.app import _resolve_product_label
        import aria.api.app as app_module

        original = app_module.product_registry
        try:
            app_module.product_registry = product_registry
            result = _resolve_product_label({"url": "https://testorum.app/api/health"})
            assert result == "Testorum"
        finally:
            app_module.product_registry = original

    def test_url_no_match(self, product_registry: ProductRegistry):
        """매칭되지 않는 URL"""
        product_registry.register(ProductConfig(
            id="testorum", name="Testorum",
            urls=["https://testorum.app"],
        ))

        from aria.api.app import _resolve_product_label
        import aria.api.app as app_module

        original = app_module.product_registry
        try:
            app_module.product_registry = product_registry
            result = _resolve_product_label({"url": "https://unknown.com"})
            assert result is None
        finally:
            app_module.product_registry = original

    def test_product_id_field(self, product_registry: ProductRegistry):
        """명시적 product_id 필드"""
        product_registry.register(ProductConfig(
            id="mystel", name="Mystel",
            urls=["https://mystel.app"],
        ))

        from aria.api.app import _resolve_product_label
        import aria.api.app as app_module

        original = app_module.product_registry
        try:
            app_module.product_registry = product_registry
            result = _resolve_product_label({"product_id": "mystel"})
            assert result == "Mystel"
        finally:
            app_module.product_registry = original

    def test_log_path_match(self, product_registry: ProductRegistry):
        """로그 경로 매칭"""
        product_registry.register(ProductConfig(
            id="testorum", name="Testorum",
            urls=["https://testorum.app"],
            log_paths=["/var/log/testorum-error.log"],
        ))

        from aria.api.app import _resolve_product_label
        import aria.api.app as app_module

        original = app_module.product_registry
        try:
            app_module.product_registry = product_registry
            result = _resolve_product_label({"log_path": "/var/log/testorum-error.log"})
            assert result == "Testorum"
        finally:
            app_module.product_registry = original

    def test_no_registry(self):
        """ProductRegistry 미초기화 → None"""
        from aria.api.app import _resolve_product_label
        import aria.api.app as app_module

        original = app_module.product_registry
        try:
            app_module.product_registry = None
            result = _resolve_product_label({"url": "https://testorum.app"})
            assert result is None
        finally:
            app_module.product_registry = original

    def test_empty_data(self, product_registry: ProductRegistry):
        """빈 데이터 → None"""
        from aria.api.app import _resolve_product_label
        import aria.api.app as app_module

        original = app_module.product_registry
        try:
            app_module.product_registry = product_registry
            result = _resolve_product_label({})
            assert result is None
        finally:
            app_module.product_registry = original


# ===================================================================
# 2. ProductRegisterRequest 변환 테스트
# ===================================================================


class TestProductRegisterRequest:
    """API 요청 모델 → ProductConfig 변환"""

    def test_basic_conversion(self):
        req = ProductRegisterRequest(
            id="testorum",
            name="Testorum",
            urls=["https://testorum.app"],
        )
        config = req.to_product_config()
        assert config.id == "testorum"
        assert config.name == "Testorum"
        assert config.urls == ["https://testorum.app"]
        # features 자동 결정됨
        assert config.features is not None
        assert ProductFeature.SERVER_HEALTH in config.features

    def test_full_conversion(self):
        req = ProductRegisterRequest(
            id="mystel",
            name="Mystel",
            urls=["https://mystel.app"],
            supabase_project_ref="mystel-ref",
            payment_provider="lemonsqueezy",
            ga4_measurement_id="G-MYSTEL123",
            repo_url="https://github.com/mystel-app/mystel.git",
            metadata={"version": "1.0"},
        )
        config = req.to_product_config()
        assert config.supabase_project_ref == "mystel-ref"
        assert config.payment_provider == "lemonsqueezy"
        assert config.metadata["version"] == "1.0"

    def test_explicit_features(self):
        req = ProductRegisterRequest(
            id="test",
            name="Test",
            features=["server_health", "error_watch"],
        )
        config = req.to_product_config()
        assert len(config.features) == 2


# ===================================================================
# 3. ProductUpdateRequest 테스트
# ===================================================================


class TestProductUpdateRequest:
    """부분 업데이트 모델"""

    def test_partial_name(self):
        update = ProductUpdateRequest(name="New Name")
        data = update.model_dump(exclude_none=True)
        assert data == {"name": "New Name"}

    def test_partial_status(self):
        update = ProductUpdateRequest(status="paused")
        data = update.model_dump(exclude_none=True)
        assert data == {"status": "paused"}

    def test_partial_urls(self):
        update = ProductUpdateRequest(urls=["https://new.app"])
        data = update.model_dump(exclude_none=True)
        assert data["urls"] == ["https://new.app"]

    def test_multiple_fields(self):
        update = ProductUpdateRequest(
            name="Updated",
            urls=["https://updated.app"],
            status="paused",
        )
        data = update.model_dump(exclude_none=True)
        assert len(data) == 3


# ===================================================================
# 4. Registry CRUD with Event Source Sync
# ===================================================================


class TestRegistryEventSourceSync:
    """ProductRegistry 등록/해제 시 이벤트 소스 동기화"""

    def test_register_adds_event_source(self, product_registry: ProductRegistry):
        """제품 등록 → 이벤트 소스 추가"""
        from aria.events.types import get_valid_sources, _registered_sources

        # cleanup
        _registered_sources.discard("test-sync")

        config = ProductConfig(id="test-sync", name="Sync Test")
        product_registry.register(config)

        # ProductRegistry 내부의 dynamic_sources에 추가됨
        assert "test-sync" in product_registry._dynamic_sources

        # cleanup
        _registered_sources.discard("test-sync")

    def test_unregister_removes_event_source(self, product_registry: ProductRegistry):
        """제품 해제 → 이벤트 소스 제거"""
        config = ProductConfig(id="test-unsync", name="Unsync Test")
        product_registry.register(config)
        assert "test-unsync" in product_registry._dynamic_sources

        product_registry.unregister("test-unsync")
        assert "test-unsync" not in product_registry._dynamic_sources


# ===================================================================
# 5. _require_product_registry 테스트
# ===================================================================


class TestRequireProductRegistry:
    """_require_product_registry 헬퍼"""

    def test_none_raises_503(self):
        from aria.api.app import _require_product_registry
        import aria.api.app as app_module

        original = app_module.product_registry
        try:
            app_module.product_registry = None
            with pytest.raises(Exception) as exc_info:
                _require_product_registry()
            # HTTPException with 503
            assert exc_info.value.status_code == 503
        finally:
            app_module.product_registry = original

    def test_initialized_returns_registry(self, product_registry: ProductRegistry):
        from aria.api.app import _require_product_registry
        import aria.api.app as app_module

        original = app_module.product_registry
        try:
            app_module.product_registry = product_registry
            result = _require_product_registry()
            assert result is product_registry
        finally:
            app_module.product_registry = original


# ===================================================================
# 6. Monitoring Alert with Product Label
# ===================================================================


class TestMonitoringAlertProductLabel:
    """모니터링 알림에 제품명 포함"""

    def test_health_check_with_product_label(self, product_registry: ProductRegistry):
        """헬스체크 알림에 [Testorum] 라벨 추가"""
        product_registry.register(ProductConfig(
            id="testorum", name="Testorum",
            urls=["https://testorum.app"],
        ))

        from aria.api.app import _resolve_product_label
        import aria.api.app as app_module

        original = app_module.product_registry
        try:
            app_module.product_registry = product_registry
            label = _resolve_product_label({"url": "https://testorum.app/v1/health"})
            assert label == "Testorum"

            # 알림 메시지에 라벨 포함
            url_display = f"[{label}] https://testorum.app/v1/health"
            assert "[Testorum]" in url_display
        finally:
            app_module.product_registry = original

    def test_multiple_products_correct_match(self, product_registry: ProductRegistry):
        """여러 제품 등록 시 올바른 매칭"""
        product_registry.register(ProductConfig(
            id="testorum", name="Testorum",
            urls=["https://testorum.app"],
        ))
        product_registry.register(ProductConfig(
            id="mystel", name="Mystel",
            urls=["https://mystel.app"],
        ))

        from aria.api.app import _resolve_product_label
        import aria.api.app as app_module

        original = app_module.product_registry
        try:
            app_module.product_registry = product_registry
            assert _resolve_product_label({"url": "https://testorum.app"}) == "Testorum"
            assert _resolve_product_label({"url": "https://mystel.app"}) == "Mystel"
        finally:
            app_module.product_registry = original


# ===================================================================
# 7. Config Integration
# ===================================================================


class TestProductConfigIntegration:
    """AriaConfig.product 통합"""

    def test_product_config_in_aria_config(self):
        from aria.core.config import AriaConfig, ProductConnectorConfig
        assert "product" in AriaConfig.model_fields

    def test_default_enabled(self):
        from aria.core.config import ProductConnectorConfig
        config = ProductConnectorConfig()
        assert config.enabled is True
        assert config.is_configured is True

    def test_custom_registry_path(self):
        from aria.core.config import ProductConnectorConfig
        config = ProductConnectorConfig(registry_path="/custom/path")
        assert config.registry_path == "/custom/path"
