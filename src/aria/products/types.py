"""ARIA Engine - Product Types

제품 등록/관리 스키마 정의 (Pydantic v2)

ProductFeature: 자동 활성화 가능한 모니터링 기능 목록
ProductConfig: 제품 등록 시 필요한 설정
ProductStatus: 제품 활성/비활성/보관 상태
ProductSummary: 제품 목록 조회용 요약
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class ProductFeature(str, Enum):
    """제품 등록 시 자동 활성화 가능한 모니터링 기능

    register_product() 호출 시 제공된 설정에 따라 자동 결정
    수동으로 features 리스트에 추가/제거도 가능
    """

    # --- 인프라 모니터링 (서버/네트워크) ---
    SERVER_HEALTH = "server_health"         # HTTP 응답시간/상태코드/SSL 만료/다운타임
    ERROR_WATCH = "error_watch"             # 에러 로그 패턴 분석/에러 스파이크 감지
    TRAFFIC_ANALYSIS = "traffic_analysis"   # 이상 트래픽/DDoS 징후/급증·급감/엔드포인트별 추적
    SECURITY_SCAN = "security_scan"         # 열린 포트/보안 헤더/의존성 CVE

    # --- 웹 품질 ---
    SEO_MONITORING = "seo_monitoring"       # 깨진 링크/메타태그 누락/Core Web Vitals/OG 이미지

    # --- 데이터 ---
    DB_MONITORING = "db_monitoring"         # Supabase 사용량/슬로우 쿼리/RLS 정책/함수 에러

    # --- 비즈니스 ---
    PAYMENT_ANOMALY = "payment_anomaly"     # 환불 급증/결제 실패율/구독 이탈률 변화
    USER_BEHAVIOR = "user_behavior"         # 이탈 구간/전환율 변화/퍼널 병목

    # --- 코드/비용 ---
    DEPENDENCY_AUDIT = "dependency_audit"   # npm audit/pip audit 취약점 자동 스캔
    COST_OPTIMIZE = "cost_optimize"         # Vercel/Supabase/API 사용량 추세 + 절감 제안

    # --- AI ---
    AI_PERSONALIZE = "ai_personalize"       # 사용자 프로필 축적 + 맞춤 추천 (scope별 격리)

    # --- 보고 ---
    WEEKLY_REPORT = "weekly_report"         # 주간 제품 리포트 (텔레그램/노션)


class ProductStatus(str, Enum):
    """제품 상태"""

    ACTIVE = "active"       # 모니터링 활성
    PAUSED = "paused"       # 일시 중지 (cron 중단 / 이벤트 수집은 유지)
    ARCHIVED = "archived"   # 보관 (unregister 시 / 메모리 보존 / 모니터링 중단)


# === Feature 자동 활성화 매핑 ===
# 조건이 충족되면 해당 feature가 자동 추가됨
# key: feature, value: 필요한 ProductConfig 필드 (None이면 무조건 활성화)
FEATURE_AUTO_ACTIVATE: dict[ProductFeature, str | None] = {
    ProductFeature.SERVER_HEALTH: "urls",                  # URL 있으면
    ProductFeature.ERROR_WATCH: None,                      # 항상
    ProductFeature.TRAFFIC_ANALYSIS: "urls",               # URL 있으면
    ProductFeature.SECURITY_SCAN: "urls",                  # URL 있으면
    ProductFeature.SEO_MONITORING: "urls",                 # URL 있으면
    ProductFeature.DB_MONITORING: "supabase_project_ref",  # Supabase 설정 있으면
    ProductFeature.PAYMENT_ANOMALY: "payment_provider",    # 결제 설정 있으면
    ProductFeature.USER_BEHAVIOR: "ga4_measurement_id",    # GA4 설정 있으면
    ProductFeature.DEPENDENCY_AUDIT: "repo_url",           # GitHub URL 있으면
    ProductFeature.COST_OPTIMIZE: None,                    # 항상
    ProductFeature.WEEKLY_REPORT: None,                    # 항상
    # AI_PERSONALIZE는 자동 활성화 안 함 (명시적 설정 필요)
}


class ProductConfig(BaseModel):
    """제품 등록 설정

    register_product()에 전달하는 제품 정보
    features 미지정 시 설정값 기반 자동 결정
    """

    id: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="제품 고유 ID (slug / 소문자+하이픈 / 예: testorum)",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="제품 표시명 (예: Testorum)",
    )

    # --- 인프라 ---
    urls: list[str] = Field(
        default_factory=list,
        description="헬스체크/SEO/보안 대상 URL (예: ['https://testorum.app'])",
    )
    log_paths: list[str] = Field(
        default_factory=list,
        description="에러 로그 파일 경로 (예: ['/var/log/nginx/testorum-error.log'])",
    )
    check_ports: list[int] = Field(
        default_factory=list,
        description="보안 스캔 포트 (미지정 시 기본: 80,443)",
    )

    # --- 데이터 ---
    supabase_project_ref: str | None = Field(
        default=None,
        description="Supabase 프로젝트 참조 ID (DB 감시용)",
    )
    supabase_service_role_key: str | None = Field(
        default=None,
        description="Supabase service_role key (슬로우 쿼리/RLS 점검용 / 환경변수 권장)",
    )

    # --- 결제 ---
    payment_provider: str | None = Field(
        default=None,
        description="결제 프로바이더 (lemonsqueezy / stripe)",
    )
    payment_webhook_events: list[str] = Field(
        default_factory=list,
        description="감시 대상 webhook 이벤트 유형 (예: ['order_refunded', 'subscription_cancelled'])",
    )

    # --- 분석 ---
    ga4_measurement_id: str | None = Field(
        default=None,
        description="GA4 Measurement ID (예: G-XXXXXXXXXX)",
    )

    # --- 코드 ---
    repo_url: str | None = Field(
        default=None,
        description="GitHub 레포 URL (의존성 감사용)",
    )
    package_manager: str = Field(
        default="npm",
        description="패키지 매니저 (npm / pip / both)",
    )

    # --- 기능 ---
    features: list[ProductFeature] | None = Field(
        default=None,
        description="활성화할 기능 목록 (미지정 시 설정 기반 자동 결정)",
    )

    # --- 메타 ---
    status: ProductStatus = Field(default=ProductStatus.ACTIVE)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="추가 메타데이터 (자유 형식)",
    )

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        """제품 ID 정규화: 소문자 + 영문/숫자/하이픈만"""
        v = v.strip().lower()
        if not re.match(r"^[a-z][a-z0-9-]*$", v):
            raise ValueError(
                f"유효하지 않은 제품 ID: '{v}'. "
                "소문자 영문으로 시작 / 영문·숫자·하이픈만 허용"
            )
        # 예약어 방지
        reserved = {"aria", "global", "admin", "api", "system"}
        if v in reserved:
            raise ValueError(f"예약된 ID: '{v}'. 사용 불가")
        return v

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, v: list[str]) -> list[str]:
        """URL 형식 기본 검증"""
        validated = []
        for url in v:
            url = url.strip().rstrip("/")
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"유효하지 않은 URL: '{url}'. http:// 또는 https://로 시작해야 합니다")
            validated.append(url)
        return validated

    @field_validator("payment_provider")
    @classmethod
    def validate_payment_provider(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.lower().strip()
            allowed = {"lemonsqueezy", "stripe", "toss"}
            if v not in allowed:
                raise ValueError(f"지원하지 않는 결제 프로바이더: '{v}'. 허용: {', '.join(sorted(allowed))}")
        return v

    @field_validator("package_manager")
    @classmethod
    def validate_package_manager(cls, v: str) -> str:
        v = v.lower().strip()
        allowed = {"npm", "pip", "both"}
        if v not in allowed:
            raise ValueError(f"지원하지 않는 패키지 매니저: '{v}'. 허용: {', '.join(sorted(allowed))}")
        return v

    @model_validator(mode="after")
    def auto_populate_features(self) -> "ProductConfig":
        """features 미지정 시 설정 기반 자동 결정"""
        if self.features is not None:
            return self

        auto_features: list[ProductFeature] = []

        for feature, required_field in FEATURE_AUTO_ACTIVATE.items():
            if required_field is None:
                # 무조건 활성화
                auto_features.append(feature)
            else:
                # 해당 필드에 값이 있으면 활성화
                field_value = getattr(self, required_field, None)
                if field_value:
                    # 빈 리스트가 아닌 경우만
                    if isinstance(field_value, list) and len(field_value) == 0:
                        continue
                    auto_features.append(feature)

        self.features = auto_features
        return self

    def get_active_features(self) -> list[ProductFeature]:
        """현재 활성화된 기능 목록"""
        return list(self.features or [])

    def has_feature(self, feature: ProductFeature) -> bool:
        """특정 기능 활성화 여부"""
        return feature in (self.features or [])


class ProductSummary(BaseModel):
    """제품 목록 조회용 요약"""

    id: str
    name: str
    status: ProductStatus
    features: list[str]
    urls: list[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_config(cls, config: ProductConfig) -> "ProductSummary":
        return cls(
            id=config.id,
            name=config.name,
            status=config.status,
            features=[f.value for f in (config.features or [])],
            urls=config.urls,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )


class ProductRegisterRequest(BaseModel):
    """제품 등록 API 요청"""

    id: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=100)
    urls: list[str] = Field(default_factory=list)
    log_paths: list[str] = Field(default_factory=list)
    check_ports: list[int] = Field(default_factory=list)
    supabase_project_ref: str | None = None
    supabase_service_role_key: str | None = None
    payment_provider: str | None = None
    payment_webhook_events: list[str] = Field(default_factory=list)
    ga4_measurement_id: str | None = None
    repo_url: str | None = None
    package_manager: str = "npm"
    features: list[str] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_product_config(self) -> ProductConfig:
        """ProductConfig로 변환"""
        feature_list = None
        if self.features is not None:
            feature_list = [ProductFeature(f) for f in self.features]

        return ProductConfig(
            id=self.id,
            name=self.name,
            urls=self.urls,
            log_paths=self.log_paths,
            check_ports=self.check_ports,
            supabase_project_ref=self.supabase_project_ref,
            supabase_service_role_key=self.supabase_service_role_key,
            payment_provider=self.payment_provider,
            payment_webhook_events=self.payment_webhook_events,
            ga4_measurement_id=self.ga4_measurement_id,
            repo_url=self.repo_url,
            package_manager=self.package_manager,
            features=feature_list,
            metadata=self.metadata,
        )


class ProductUpdateRequest(BaseModel):
    """제품 업데이트 API 요청 (부분 업데이트)"""

    name: str | None = None
    urls: list[str] | None = None
    log_paths: list[str] | None = None
    check_ports: list[int] | None = None
    supabase_project_ref: str | None = None
    payment_provider: str | None = None
    ga4_measurement_id: str | None = None
    repo_url: str | None = None
    features: list[str] | None = None
    status: str | None = None
    metadata: dict[str, Any] | None = None
