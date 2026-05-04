"""ARIA Engine - Product Registry

제품 등록/해제/관리 로직

register_product():
  1. 설정 검증 + 기능 자동 결정
  2. products/registry.json에 저장
  3. 메모리 스코프 디렉토리 생성 (memory/{product_id}/)
  4. 이벤트 소스 동적 등록
  5. 모니터링 타겟 갱신
  6. 등록 이벤트 발행

unregister_product():
  1. 상태를 ARCHIVED로 변경
  2. 모니터링 비활성화
  3. 메모리 보존 (삭제하지 않음)
  4. 해제 이벤트 발행
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from aria.products.types import (
    ProductConfig,
    ProductFeature,
    ProductStatus,
    ProductSummary,
    ProductUpdateRequest,
)

logger = structlog.get_logger()


class ProductAlreadyExistsError(Exception):
    """이미 등록된 제품 ID"""

    def __init__(self, product_id: str) -> None:
        self.product_id = product_id
        super().__init__(f"이미 등록된 제품: '{product_id}'")


class ProductNotFoundError(Exception):
    """존재하지 않는 제품 ID"""

    def __init__(self, product_id: str) -> None:
        self.product_id = product_id
        super().__init__(f"등록되지 않은 제품: '{product_id}'")


class ProductRegistryError(Exception):
    """레지스트리 I/O 에러"""
    pass


class ProductRegistry:
    """범용 제품 연동 레지스트리

    파일 기반 저장소 (products/registry.json)
    - 사람이 읽고 편집 가능
    - git 버전관리 가능
    - 추가 DB 의존성 없음
    """

    def __init__(
        self,
        base_path: str = "./products",
        memory_base_path: str = "./memory",
    ) -> None:
        self._base_path = Path(base_path)
        self._memory_base_path = Path(memory_base_path)
        self._registry_file = self._base_path / "registry.json"
        self._products: dict[str, ProductConfig] = {}

        # 동적 이벤트 소스 관리
        self._dynamic_sources: set[str] = set()

        # 디렉토리 생성
        self._base_path.mkdir(parents=True, exist_ok=True)

        # 기존 레지스트리 로딩
        self._load()

    def _load(self) -> None:
        """레지스트리 파일 로딩"""
        if not self._registry_file.exists():
            self._products = {}
            return

        try:
            raw = self._registry_file.read_text(encoding="utf-8")
            data = json.loads(raw)

            if not isinstance(data, dict) or "products" not in data:
                logger.warning("product_registry_invalid_format", path=str(self._registry_file))
                self._products = {}
                return

            for product_data in data["products"]:
                try:
                    config = ProductConfig(**product_data)
                    self._products[config.id] = config
                    # 활성 제품은 동적 소스에 등록
                    if config.status == ProductStatus.ACTIVE:
                        self._dynamic_sources.add(config.id)
                except Exception as e:
                    logger.warning(
                        "product_load_skip",
                        product_id=product_data.get("id", "unknown"),
                        error=str(e),
                    )

            logger.info(
                "product_registry_loaded",
                total=len(self._products),
                active=len([p for p in self._products.values() if p.status == ProductStatus.ACTIVE]),
            )
        except json.JSONDecodeError as e:
            logger.error("product_registry_parse_error", error=str(e))
            self._products = {}
        except OSError as e:
            logger.error("product_registry_read_error", error=str(e))
            self._products = {}

    def _save(self) -> None:
        """레지스트리 파일 저장 (원자적 쓰기: tmpfile → rename)"""
        data = {
            "version": "1.0",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "products": [
                p.model_dump(mode="json") for p in self._products.values()
            ],
        }

        tmp_file = self._registry_file.with_suffix(".tmp")
        try:
            tmp_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_file.replace(self._registry_file)
        except OSError as e:
            # tmpfile 정리
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
            raise ProductRegistryError(f"레지스트리 저장 실패: {e}") from e

    # === CRUD ===

    def register(self, config: ProductConfig) -> ProductConfig:
        """제품 등록

        1. 중복 체크 (archived 상태면 재활성화 허용)
        2. 기능 자동 결정 (features=None이면)
        3. 메모리 스코프 디렉토리 생성
        4. 이벤트 소스 등록
        5. 레지스트리 파일 저장

        Returns:
            최종 ProductConfig (auto-populated features 포함)
        """
        product_id = config.id

        # 중복 체크 (archived면 재활성화)
        if product_id in self._products:
            existing = self._products[product_id]
            if existing.status == ProductStatus.ARCHIVED:
                logger.info(
                    "product_reactivate",
                    product_id=product_id,
                    previous_status="archived",
                )
                config.status = ProductStatus.ACTIVE
                config.updated_at = datetime.now(timezone.utc).isoformat()
                # 기존 created_at 보존
                config.created_at = existing.created_at
            else:
                raise ProductAlreadyExistsError(product_id)

        # 메모리 스코프 디렉토리 생성
        scope_dir = self._memory_base_path / product_id
        scope_dir.mkdir(parents=True, exist_ok=True)
        topics_dir = scope_dir / "topics"
        topics_dir.mkdir(parents=True, exist_ok=True)

        # 인덱스 파일 초기화 (없으면)
        index_file = scope_dir / "index.json"
        if not index_file.exists():
            index_file.write_text(
                json.dumps({"entries": {}, "scope": product_id}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # 제품 저장
        self._products[product_id] = config
        self._dynamic_sources.add(product_id)

        # 레지스트리 파일 저장
        self._save()

        logger.info(
            "product_registered",
            product_id=product_id,
            name=config.name,
            features=[f.value for f in config.get_active_features()],
            urls=config.urls,
        )

        return config

    def unregister(self, product_id: str) -> ProductConfig:
        """제품 해제 (ARCHIVED 상태 전환 — 메모리 보존)

        1. 상태 → ARCHIVED
        2. 모니터링 비활성화
        3. 동적 소스에서 제거
        4. 메모리 디렉토리는 보존 (삭제하지 않음)
        5. 레지스트리 파일 저장

        Returns:
            해제된 ProductConfig
        """
        if product_id not in self._products:
            raise ProductNotFoundError(product_id)

        config = self._products[product_id]
        config.status = ProductStatus.ARCHIVED
        config.updated_at = datetime.now(timezone.utc).isoformat()

        # 동적 소스에서 제거
        self._dynamic_sources.discard(product_id)

        # 레지스트리 저장
        self._save()

        logger.info(
            "product_unregistered",
            product_id=product_id,
            name=config.name,
            memory_preserved=True,
        )

        return config

    def update(self, product_id: str, update: ProductUpdateRequest) -> ProductConfig:
        """제품 설정 업데이트 (부분 업데이트)

        Returns:
            업데이트된 ProductConfig
        """
        if product_id not in self._products:
            raise ProductNotFoundError(product_id)

        config = self._products[product_id]

        # 부분 업데이트 적용
        update_data = update.model_dump(exclude_none=True)
        for field, value in update_data.items():
            if field == "features":
                config.features = [ProductFeature(f) for f in value]
            elif field == "status":
                config.status = ProductStatus(value)
                # 활성화/비활성화 시 동적 소스 관리
                if config.status == ProductStatus.ACTIVE:
                    self._dynamic_sources.add(product_id)
                else:
                    self._dynamic_sources.discard(product_id)
            elif hasattr(config, field):
                setattr(config, field, value)

        config.updated_at = datetime.now(timezone.utc).isoformat()

        self._save()

        logger.info(
            "product_updated",
            product_id=product_id,
            updated_fields=list(update_data.keys()),
        )

        return config

    def get(self, product_id: str) -> ProductConfig:
        """제품 설정 조회"""
        if product_id not in self._products:
            raise ProductNotFoundError(product_id)
        return self._products[product_id]

    def list_all(self) -> list[ProductSummary]:
        """전체 제품 목록 (요약)"""
        return [
            ProductSummary.from_config(config)
            for config in self._products.values()
        ]

    def list_active(self) -> list[ProductConfig]:
        """활성 제품 목록 (전체 설정)"""
        return [
            config for config in self._products.values()
            if config.status == ProductStatus.ACTIVE
        ]

    def has_product(self, product_id: str) -> bool:
        """제품 등록 여부"""
        return product_id in self._products

    @property
    def product_count(self) -> int:
        """전체 등록 제품 수"""
        return len(self._products)

    @property
    def active_count(self) -> int:
        """활성 제품 수"""
        return len([p for p in self._products.values() if p.status == ProductStatus.ACTIVE])

    # === 이벤트 소스 관리 ===

    def get_valid_sources(self) -> frozenset[str]:
        """현재 유효한 이벤트 소스 목록

        기본 소스(aria, trendbot 등) + 등록된 활성 제품 ID
        """
        from aria.events.types import DEFAULT_SOURCES
        return frozenset(DEFAULT_SOURCES | self._dynamic_sources)

    def is_valid_source(self, source: str) -> bool:
        """이벤트 소스 유효성 검증"""
        return source in self.get_valid_sources()

    # === 모니터링 타겟 관리 ===

    def get_health_targets(self) -> dict[str, list[str]]:
        """제품별 헬스체크 URL 매핑

        Returns:
            {"testorum": ["https://testorum.app"], "mystel": ["https://mystel.app"]}
        """
        targets: dict[str, list[str]] = {}
        for config in self.list_active():
            if config.urls and config.has_feature(ProductFeature.SERVER_HEALTH):
                targets[config.id] = config.urls
        return targets

    def get_all_health_urls(self) -> list[str]:
        """전체 활성 제품 헬스체크 URL 목록 (flat)

        기존 MonitoringConfig.targets 와 호환되는 형태
        """
        urls: list[str] = []
        for config in self.list_active():
            if config.urls and config.has_feature(ProductFeature.SERVER_HEALTH):
                urls.extend(config.urls)
        return urls

    def get_log_paths(self) -> dict[str, list[str]]:
        """제품별 에러 로그 경로"""
        paths: dict[str, list[str]] = {}
        for config in self.list_active():
            if config.log_paths and config.has_feature(ProductFeature.ERROR_WATCH):
                paths[config.id] = config.log_paths
        return paths

    def get_seo_targets(self) -> dict[str, list[str]]:
        """제품별 SEO 모니터링 URL"""
        targets: dict[str, list[str]] = {}
        for config in self.list_active():
            if config.urls and config.has_feature(ProductFeature.SEO_MONITORING):
                targets[config.id] = config.urls
        return targets

    def get_products_with_feature(self, feature: ProductFeature) -> list[ProductConfig]:
        """특정 기능이 활성화된 제품 목록"""
        return [
            config for config in self.list_active()
            if config.has_feature(feature)
        ]

    # === 제품 삭제 (영구 — 주의) ===

    def purge(self, product_id: str, delete_memory: bool = False) -> None:
        """제품 영구 삭제 (복구 불가)

        Args:
            product_id: 삭제할 제품 ID
            delete_memory: True면 메모리 디렉토리도 삭제 (기본: 보존)
        """
        if product_id not in self._products:
            raise ProductNotFoundError(product_id)

        config = self._products[product_id]

        # 메모리 삭제 (옵션)
        if delete_memory:
            scope_dir = self._memory_base_path / product_id
            if scope_dir.exists():
                shutil.rmtree(scope_dir)
                logger.warning(
                    "product_memory_deleted",
                    product_id=product_id,
                    path=str(scope_dir),
                )

        # 레지스트리에서 제거
        del self._products[product_id]
        self._dynamic_sources.discard(product_id)

        self._save()

        logger.info(
            "product_purged",
            product_id=product_id,
            name=config.name,
            memory_deleted=delete_memory,
        )
