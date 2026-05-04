"""ARIA Engine - Product Registry Tests

Product Connector Step 1 테스트
- ProductConfig 스키마 검증
- ProductFeature 자동 활성화
- ProductRegistry CRUD
- 이벤트 소스 동적 관리
- 모니터링 타겟 관리
- 파일 I/O + 원자적 쓰기
- 엣지 케이스 (중복/미존재/재활성화/영구삭제)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from aria.products.types import (
    ProductConfig,
    ProductFeature,
    ProductRegisterRequest,
    ProductStatus,
    ProductSummary,
    ProductUpdateRequest,
    FEATURE_AUTO_ACTIVATE,
)
from aria.products.registry import (
    ProductAlreadyExistsError,
    ProductNotFoundError,
    ProductRegistry,
    ProductRegistryError,
)


# === Fixtures ===


@pytest.fixture
def tmp_dirs(tmp_path: Path):
    """임시 products + memory 디렉토리"""
    products_dir = tmp_path / "products"
    memory_dir = tmp_path / "memory"
    products_dir.mkdir()
    memory_dir.mkdir()
    return products_dir, memory_dir


@pytest.fixture
def registry(tmp_dirs):
    """빈 ProductRegistry"""
    products_dir, memory_dir = tmp_dirs
    return ProductRegistry(
        base_path=str(products_dir),
        memory_base_path=str(memory_dir),
    )


@pytest.fixture
def testorum_config() -> ProductConfig:
    """Testorum 제품 설정"""
    return ProductConfig(
        id="testorum",
        name="Testorum",
        urls=["https://testorum.app"],
        supabase_project_ref="testorum-ref-123",
        payment_provider="lemonsqueezy",
        ga4_measurement_id="G-TEST12345",
        repo_url="https://github.com/Testorum/testorum.git",
        package_manager="npm",
    )


@pytest.fixture
def mystel_config() -> ProductConfig:
    """Mystel 제품 설정"""
    return ProductConfig(
        id="mystel",
        name="Mystel",
        urls=["https://mystel.app"],
        supabase_project_ref="mystel-ref-456",
        payment_provider="lemonsqueezy",
        repo_url="https://github.com/mystel-app/mystel.git",
    )


@pytest.fixture
def minimal_config() -> ProductConfig:
    """최소 설정 (URL 없음)"""
    return ProductConfig(
        id="test-minimal",
        name="Minimal Product",
    )


# ===================================================================
# 1. ProductConfig 스키마 검증
# ===================================================================


class TestProductConfigSchema:
    """ProductConfig Pydantic 모델 검증"""

    def test_valid_id_lowercase(self):
        config = ProductConfig(id="testorum", name="Testorum")
        assert config.id == "testorum"

    def test_id_auto_lowercase(self):
        config = ProductConfig(id="Testorum", name="Testorum")
        assert config.id == "testorum"

    def test_id_with_hyphens(self):
        config = ProductConfig(id="my-product", name="My Product")
        assert config.id == "my-product"

    def test_invalid_id_starts_with_number(self):
        with pytest.raises(ValueError, match="유효하지 않은 제품 ID"):
            ProductConfig(id="1product", name="Bad")

    def test_invalid_id_special_chars(self):
        with pytest.raises(ValueError, match="유효하지 않은 제품 ID"):
            ProductConfig(id="my_product", name="Bad")  # 언더스코어 불허

    def test_reserved_id_aria(self):
        with pytest.raises(ValueError, match="예약된 ID"):
            ProductConfig(id="aria", name="Bad")

    def test_reserved_id_global(self):
        with pytest.raises(ValueError, match="예약된 ID"):
            ProductConfig(id="global", name="Bad")

    def test_url_validation_valid(self):
        config = ProductConfig(
            id="test", name="Test",
            urls=["https://example.com", "http://localhost:3000"],
        )
        assert len(config.urls) == 2

    def test_url_validation_invalid(self):
        with pytest.raises(ValueError, match="유효하지 않은 URL"):
            ProductConfig(id="test", name="Test", urls=["example.com"])

    def test_url_trailing_slash_stripped(self):
        config = ProductConfig(id="test", name="Test", urls=["https://example.com/"])
        assert config.urls == ["https://example.com"]

    def test_payment_provider_valid(self):
        config = ProductConfig(id="test", name="Test", payment_provider="lemonsqueezy")
        assert config.payment_provider == "lemonsqueezy"

    def test_payment_provider_invalid(self):
        with pytest.raises(ValueError, match="지원하지 않는 결제 프로바이더"):
            ProductConfig(id="test", name="Test", payment_provider="paypal")

    def test_package_manager_default(self):
        config = ProductConfig(id="test", name="Test")
        assert config.package_manager == "npm"

    def test_package_manager_both(self):
        config = ProductConfig(id="test", name="Test", package_manager="both")
        assert config.package_manager == "both"

    def test_default_status_active(self):
        config = ProductConfig(id="test", name="Test")
        assert config.status == ProductStatus.ACTIVE

    def test_created_at_auto(self):
        config = ProductConfig(id="test", name="Test")
        assert config.created_at is not None
        assert "T" in config.created_at  # ISO format

    def test_metadata_default_empty(self):
        config = ProductConfig(id="test", name="Test")
        assert config.metadata == {}

    def test_metadata_custom(self):
        config = ProductConfig(id="test", name="Test", metadata={"version": "1.0"})
        assert config.metadata["version"] == "1.0"


# ===================================================================
# 2. Feature 자동 활성화
# ===================================================================


class TestFeatureAutoActivate:
    """features=None일 때 설정 기반 자동 결정"""

    def test_full_config_all_features(self, testorum_config: ProductConfig):
        """모든 설정 제공 → 대부분의 기능 활성화"""
        features = testorum_config.get_active_features()
        assert ProductFeature.SERVER_HEALTH in features
        assert ProductFeature.ERROR_WATCH in features
        assert ProductFeature.TRAFFIC_ANALYSIS in features
        assert ProductFeature.SECURITY_SCAN in features
        assert ProductFeature.SEO_MONITORING in features
        assert ProductFeature.DB_MONITORING in features
        assert ProductFeature.PAYMENT_ANOMALY in features
        assert ProductFeature.USER_BEHAVIOR in features
        assert ProductFeature.DEPENDENCY_AUDIT in features
        assert ProductFeature.COST_OPTIMIZE in features
        assert ProductFeature.WEEKLY_REPORT in features
        # AI_PERSONALIZE는 자동 활성화 안 됨
        assert ProductFeature.AI_PERSONALIZE not in features

    def test_minimal_config_few_features(self, minimal_config: ProductConfig):
        """최소 설정 → 무조건 활성화 기능만"""
        features = minimal_config.get_active_features()
        assert ProductFeature.ERROR_WATCH in features
        assert ProductFeature.COST_OPTIMIZE in features
        assert ProductFeature.WEEKLY_REPORT in features
        # URL 없으므로 비활성화
        assert ProductFeature.SERVER_HEALTH not in features
        assert ProductFeature.SEO_MONITORING not in features
        assert ProductFeature.TRAFFIC_ANALYSIS not in features

    def test_urls_only(self):
        """URL만 있으면 서버 관련 기능만"""
        config = ProductConfig(id="test", name="Test", urls=["https://example.com"])
        features = config.get_active_features()
        assert ProductFeature.SERVER_HEALTH in features
        assert ProductFeature.TRAFFIC_ANALYSIS in features
        assert ProductFeature.SECURITY_SCAN in features
        assert ProductFeature.SEO_MONITORING in features
        assert ProductFeature.DB_MONITORING not in features
        assert ProductFeature.PAYMENT_ANOMALY not in features

    def test_explicit_features_override(self):
        """features 명시 시 자동 결정 안 함"""
        config = ProductConfig(
            id="test",
            name="Test",
            urls=["https://example.com"],
            features=[ProductFeature.SERVER_HEALTH],
        )
        features = config.get_active_features()
        assert features == [ProductFeature.SERVER_HEALTH]
        # 자동으로 추가되지 않음
        assert ProductFeature.TRAFFIC_ANALYSIS not in features

    def test_has_feature(self, testorum_config: ProductConfig):
        assert testorum_config.has_feature(ProductFeature.SERVER_HEALTH) is True
        assert testorum_config.has_feature(ProductFeature.AI_PERSONALIZE) is False

    def test_empty_urls_no_server_features(self):
        """빈 리스트 urls → 서버 기능 비활성화"""
        config = ProductConfig(id="test", name="Test", urls=[])
        assert ProductFeature.SERVER_HEALTH not in config.get_active_features()


# ===================================================================
# 3. ProductRegistry CRUD
# ===================================================================


class TestProductRegistryCRUD:
    """제품 등록/조회/수정/해제"""

    def test_register(self, registry: ProductRegistry, testorum_config: ProductConfig):
        result = registry.register(testorum_config)
        assert result.id == "testorum"
        assert result.status == ProductStatus.ACTIVE
        assert registry.product_count == 1
        assert registry.active_count == 1

    def test_register_duplicate_raises(self, registry: ProductRegistry, testorum_config: ProductConfig):
        registry.register(testorum_config)
        with pytest.raises(ProductAlreadyExistsError):
            registry.register(testorum_config)

    def test_register_multiple(
        self,
        registry: ProductRegistry,
        testorum_config: ProductConfig,
        mystel_config: ProductConfig,
    ):
        registry.register(testorum_config)
        registry.register(mystel_config)
        assert registry.product_count == 2
        assert registry.active_count == 2

    def test_get(self, registry: ProductRegistry, testorum_config: ProductConfig):
        registry.register(testorum_config)
        result = registry.get("testorum")
        assert result.name == "Testorum"
        assert result.urls == ["https://testorum.app"]

    def test_get_not_found(self, registry: ProductRegistry):
        with pytest.raises(ProductNotFoundError):
            registry.get("nonexistent")

    def test_has_product(self, registry: ProductRegistry, testorum_config: ProductConfig):
        assert registry.has_product("testorum") is False
        registry.register(testorum_config)
        assert registry.has_product("testorum") is True

    def test_list_all(
        self,
        registry: ProductRegistry,
        testorum_config: ProductConfig,
        mystel_config: ProductConfig,
    ):
        registry.register(testorum_config)
        registry.register(mystel_config)
        summaries = registry.list_all()
        assert len(summaries) == 2
        ids = {s.id for s in summaries}
        assert ids == {"testorum", "mystel"}

    def test_list_active(self, registry: ProductRegistry, testorum_config: ProductConfig):
        registry.register(testorum_config)
        active = registry.list_active()
        assert len(active) == 1
        assert active[0].id == "testorum"

    def test_unregister(self, registry: ProductRegistry, testorum_config: ProductConfig):
        registry.register(testorum_config)
        result = registry.unregister("testorum")
        assert result.status == ProductStatus.ARCHIVED
        assert registry.active_count == 0
        assert registry.product_count == 1  # 레지스트리에는 남아있음

    def test_unregister_not_found(self, registry: ProductRegistry):
        with pytest.raises(ProductNotFoundError):
            registry.unregister("nonexistent")

    def test_update(self, registry: ProductRegistry, testorum_config: ProductConfig):
        registry.register(testorum_config)
        update = ProductUpdateRequest(name="Testorum v2", urls=["https://new.testorum.app"])
        result = registry.update("testorum", update)
        assert result.name == "Testorum v2"
        assert result.urls == ["https://new.testorum.app"]

    def test_update_status(self, registry: ProductRegistry, testorum_config: ProductConfig):
        registry.register(testorum_config)
        update = ProductUpdateRequest(status="paused")
        result = registry.update("testorum", update)
        assert result.status == ProductStatus.PAUSED
        assert registry.active_count == 0

    def test_update_not_found(self, registry: ProductRegistry):
        with pytest.raises(ProductNotFoundError):
            registry.update("nonexistent", ProductUpdateRequest(name="X"))


# ===================================================================
# 4. 재활성화 + 영구삭제
# ===================================================================


class TestProductLifecycle:
    """아카이브 → 재활성화 / 영구삭제"""

    def test_reactivate_archived(self, registry: ProductRegistry, testorum_config: ProductConfig):
        """archived 상태 제품 재등록 → 재활성화"""
        registry.register(testorum_config)
        registry.unregister("testorum")
        assert registry.active_count == 0

        # 같은 ID로 다시 등록 → 재활성화
        new_config = ProductConfig(
            id="testorum",
            name="Testorum Reborn",
            urls=["https://testorum.app"],
        )
        result = registry.register(new_config)
        assert result.status == ProductStatus.ACTIVE
        assert result.name == "Testorum Reborn"
        assert registry.active_count == 1

    def test_purge_without_memory(self, registry: ProductRegistry, testorum_config: ProductConfig, tmp_dirs):
        """영구삭제 (메모리 보존)"""
        _, memory_dir = tmp_dirs
        registry.register(testorum_config)

        scope_dir = memory_dir / "testorum"
        assert scope_dir.exists()

        registry.purge("testorum", delete_memory=False)
        assert registry.product_count == 0
        assert scope_dir.exists()  # 메모리 보존

    def test_purge_with_memory(self, registry: ProductRegistry, testorum_config: ProductConfig, tmp_dirs):
        """영구삭제 (메모리도 삭제)"""
        _, memory_dir = tmp_dirs
        registry.register(testorum_config)

        scope_dir = memory_dir / "testorum"
        assert scope_dir.exists()

        registry.purge("testorum", delete_memory=True)
        assert registry.product_count == 0
        assert not scope_dir.exists()  # 메모리 삭제됨

    def test_purge_not_found(self, registry: ProductRegistry):
        with pytest.raises(ProductNotFoundError):
            registry.purge("nonexistent")


# ===================================================================
# 5. 파일 I/O (영속성)
# ===================================================================


class TestProductPersistence:
    """레지스트리 파일 저장/로딩"""

    def test_save_and_reload(self, tmp_dirs, testorum_config: ProductConfig):
        """저장 후 새 인스턴스에서 로딩"""
        products_dir, memory_dir = tmp_dirs

        # 등록
        reg1 = ProductRegistry(str(products_dir), str(memory_dir))
        reg1.register(testorum_config)

        # 새 인스턴스로 로딩
        reg2 = ProductRegistry(str(products_dir), str(memory_dir))
        assert reg2.product_count == 1
        assert reg2.get("testorum").name == "Testorum"

    def test_registry_file_format(self, registry: ProductRegistry, testorum_config: ProductConfig, tmp_dirs):
        """레지스트리 JSON 파일 형식 검증"""
        products_dir, _ = tmp_dirs
        registry.register(testorum_config)

        registry_file = products_dir / "registry.json"
        assert registry_file.exists()

        data = json.loads(registry_file.read_text())
        assert data["version"] == "1.0"
        assert "updated_at" in data
        assert len(data["products"]) == 1
        assert data["products"][0]["id"] == "testorum"

    def test_empty_registry_file(self, tmp_dirs):
        """빈 레지스트리 → 정상 초기화"""
        products_dir, memory_dir = tmp_dirs
        reg = ProductRegistry(str(products_dir), str(memory_dir))
        assert reg.product_count == 0

    def test_corrupted_registry_file(self, tmp_dirs):
        """손상된 JSON → 빈 레지스트리로 초기화"""
        products_dir, memory_dir = tmp_dirs
        registry_file = products_dir / "registry.json"
        registry_file.write_text("invalid json{{{")

        reg = ProductRegistry(str(products_dir), str(memory_dir))
        assert reg.product_count == 0

    def test_invalid_product_in_registry(self, tmp_dirs):
        """잘못된 제품 데이터 → skip"""
        products_dir, memory_dir = tmp_dirs
        registry_file = products_dir / "registry.json"
        data = {
            "version": "1.0",
            "products": [
                {"id": "valid", "name": "Valid Product"},
                {"id": "1invalid", "name": "Bad ID"},  # 숫자로 시작
            ],
        }
        registry_file.write_text(json.dumps(data))

        reg = ProductRegistry(str(products_dir), str(memory_dir))
        assert reg.product_count == 1
        assert reg.has_product("valid")


# ===================================================================
# 6. 메모리 스코프 관리
# ===================================================================


class TestMemoryScope:
    """제품 등록 시 메모리 디렉토리 자동 생성"""

    def test_scope_dir_created(self, registry: ProductRegistry, testorum_config: ProductConfig, tmp_dirs):
        _, memory_dir = tmp_dirs
        registry.register(testorum_config)

        scope_dir = memory_dir / "testorum"
        assert scope_dir.exists()
        assert (scope_dir / "topics").exists()
        assert (scope_dir / "index.json").exists()

    def test_index_file_content(self, registry: ProductRegistry, testorum_config: ProductConfig, tmp_dirs):
        _, memory_dir = tmp_dirs
        registry.register(testorum_config)

        index_file = memory_dir / "testorum" / "index.json"
        data = json.loads(index_file.read_text())
        assert data["scope"] == "testorum"
        assert data["entries"] == {}

    def test_scope_dir_preserved_on_unregister(
        self, registry: ProductRegistry, testorum_config: ProductConfig, tmp_dirs
    ):
        _, memory_dir = tmp_dirs
        registry.register(testorum_config)
        registry.unregister("testorum")

        # 메모리 보존
        assert (memory_dir / "testorum").exists()
        assert (memory_dir / "testorum" / "index.json").exists()


# ===================================================================
# 7. 이벤트 소스 동적 관리
# ===================================================================


class TestDynamicEventSources:
    """ProductRegistry → 이벤트 소스 동적 등록/해제"""

    def test_register_adds_source(self, registry: ProductRegistry, mystel_config: ProductConfig):
        registry.register(mystel_config)
        sources = registry.get_valid_sources()
        assert "mystel" in sources

    def test_unregister_removes_source(self, registry: ProductRegistry, mystel_config: ProductConfig):
        registry.register(mystel_config)
        registry.unregister("mystel")
        # 아카이브 후 동적 소스에서 제거
        assert "mystel" not in registry._dynamic_sources

    def test_default_sources_always_present(self, registry: ProductRegistry):
        sources = registry.get_valid_sources()
        assert "aria" in sources
        assert "trendbot" in sources
        assert "testorum" in sources  # DEFAULT_SOURCES에 포함

    def test_is_valid_source(self, registry: ProductRegistry, mystel_config: ProductConfig):
        assert registry.is_valid_source("aria") is True
        assert registry.is_valid_source("mystel") is False
        registry.register(mystel_config)
        assert registry.is_valid_source("mystel") is True


# ===================================================================
# 8. 모니터링 타겟 관리
# ===================================================================


class TestMonitoringTargets:
    """제품별 모니터링 URL/경로 관리"""

    def test_health_targets(
        self,
        registry: ProductRegistry,
        testorum_config: ProductConfig,
        mystel_config: ProductConfig,
    ):
        registry.register(testorum_config)
        registry.register(mystel_config)
        targets = registry.get_health_targets()
        assert "testorum" in targets
        assert "mystel" in targets
        assert targets["testorum"] == ["https://testorum.app"]

    def test_all_health_urls_flat(
        self,
        registry: ProductRegistry,
        testorum_config: ProductConfig,
        mystel_config: ProductConfig,
    ):
        registry.register(testorum_config)
        registry.register(mystel_config)
        urls = registry.get_all_health_urls()
        assert "https://testorum.app" in urls
        assert "https://mystel.app" in urls

    def test_no_urls_no_health(self, registry: ProductRegistry, minimal_config: ProductConfig):
        registry.register(minimal_config)
        targets = registry.get_health_targets()
        assert "test-minimal" not in targets

    def test_archived_excluded(self, registry: ProductRegistry, testorum_config: ProductConfig):
        registry.register(testorum_config)
        registry.unregister("testorum")
        targets = registry.get_health_targets()
        assert "testorum" not in targets

    def test_seo_targets(self, registry: ProductRegistry, testorum_config: ProductConfig):
        registry.register(testorum_config)
        seo = registry.get_seo_targets()
        assert "testorum" in seo
        assert seo["testorum"] == ["https://testorum.app"]

    def test_products_with_feature(
        self,
        registry: ProductRegistry,
        testorum_config: ProductConfig,
        minimal_config: ProductConfig,
    ):
        registry.register(testorum_config)
        registry.register(minimal_config)
        db_products = registry.get_products_with_feature(ProductFeature.DB_MONITORING)
        assert len(db_products) == 1
        assert db_products[0].id == "testorum"

    def test_log_paths(self, registry: ProductRegistry):
        config = ProductConfig(
            id="test-logs",
            name="Log Test",
            urls=["https://example.com"],
            log_paths=["/var/log/test-error.log"],
        )
        registry.register(config)
        paths = registry.get_log_paths()
        assert "test-logs" in paths
        assert paths["test-logs"] == ["/var/log/test-error.log"]


# ===================================================================
# 9. ProductSummary + ProductRegisterRequest
# ===================================================================


class TestProductModels:
    """보조 모델 테스트"""

    def test_summary_from_config(self, testorum_config: ProductConfig):
        summary = ProductSummary.from_config(testorum_config)
        assert summary.id == "testorum"
        assert summary.name == "Testorum"
        assert summary.status == ProductStatus.ACTIVE
        assert "server_health" in summary.features

    def test_register_request_to_config(self):
        req = ProductRegisterRequest(
            id="test",
            name="Test",
            urls=["https://test.com"],
            payment_provider="stripe",
        )
        config = req.to_product_config()
        assert config.id == "test"
        assert config.urls == ["https://test.com"]
        assert config.payment_provider == "stripe"
        # features 자동 결정됨
        assert config.features is not None

    def test_register_request_explicit_features(self):
        req = ProductRegisterRequest(
            id="test",
            name="Test",
            features=["server_health", "error_watch"],
        )
        config = req.to_product_config()
        assert len(config.features) == 2
        assert ProductFeature.SERVER_HEALTH in config.features

    def test_update_request_partial(self):
        update = ProductUpdateRequest(name="New Name")
        data = update.model_dump(exclude_none=True)
        assert data == {"name": "New Name"}

    def test_update_request_empty(self):
        update = ProductUpdateRequest()
        data = update.model_dump(exclude_none=True)
        assert data == {}


# ===================================================================
# 10. 이벤트 소스 모듈 레벨 함수 (events/types.py)
# ===================================================================


class TestEventSourceDynamic:
    """events/types.py의 동적 소스 함수"""

    def test_register_event_source(self):
        from aria.events.types import register_event_source, get_valid_sources, unregister_event_source

        register_event_source("new-product")
        assert "new-product" in get_valid_sources()

        # cleanup
        unregister_event_source("new-product")
        assert "new-product" not in get_valid_sources()

    def test_default_sources_immutable(self):
        from aria.events.types import DEFAULT_SOURCES
        # frozenset이므로 수정 불가
        with pytest.raises(AttributeError):
            DEFAULT_SOURCES.add("hack")

    def test_get_valid_sources_includes_defaults(self):
        from aria.events.types import get_valid_sources
        sources = get_valid_sources()
        assert "aria" in sources
        assert "trendbot" in sources
        assert "testorum" in sources

    def test_event_input_with_registered_source(self):
        """동적으로 등록된 소스로 이벤트 생성"""
        from aria.events.types import EventInput, register_event_source, unregister_event_source

        register_event_source("mystel")
        try:
            event = EventInput(
                event_type="tarot_reading",
                source="mystel",
                data={"reading_type": "three_card"},
            )
            assert event.source == "mystel"
        finally:
            unregister_event_source("mystel")

    def test_event_input_unregistered_source_fails(self):
        """미등록 소스 → ValidationError"""
        from aria.events.types import EventInput
        with pytest.raises(ValueError, match="유효하지 않은 소스"):
            EventInput(
                event_type="test",
                source="unknown-product",
            )


# ===================================================================
# 11. ProductConnectorConfig
# ===================================================================


class TestProductConnectorConfig:
    """config.py ProductConnectorConfig"""

    def test_default_values(self):
        from aria.core.config import ProductConnectorConfig
        config = ProductConnectorConfig()
        assert config.enabled is True
        assert config.registry_path == "./products"
        assert config.auto_monitor is True
        assert config.healthcheck_interval_minutes == 5
        assert config.seo_check_interval_hours == 24

    def test_is_configured(self):
        from aria.core.config import ProductConnectorConfig
        config = ProductConnectorConfig()
        assert config.is_configured is True

        config2 = ProductConnectorConfig(enabled=False)
        assert config2.is_configured is False

    def test_aria_config_has_product(self):
        """AriaConfig에 product 필드 존재"""
        from aria.core.config import AriaConfig, ProductConnectorConfig
        # AriaConfig 필드 확인
        assert "product" in AriaConfig.model_fields
