"""ARIA Engine - Configuration Management

pydantic-settings 기반 환경변수 관리
모든 설정은 .env 파일 또는 환경변수에서 로드

테스트 시 .env 파일 격리:
    ARIA_ENV_FILE="" pytest tests/ -v
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _get_env_file() -> str | None:
    """환경변수 ARIA_ENV_FILE로 .env 파일 경로 결정

    - 미설정: ".env" (기본값)
    - 빈 문자열: None (.env 파일 읽지 않음 — 테스트 격리용)
    - 경로 지정: 해당 경로 사용
    """
    val = os.environ.get("ARIA_ENV_FILE", ".env")
    return val or None


class Environment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LLMConfig(BaseSettings):
    """LLM Provider 설정"""

    model_config = SettingsConfigDict(env_prefix="ARIA_", env_file=_get_env_file(), extra="ignore")

    default_model: str = Field(default="claude-sonnet-4-20250514", description="기본 LLM 모델")
    fallback_model: str = Field(default="claude-haiku-4-5-20251001", description="장애 시 대체 모델 (Sonnet과 다른 rate limit 풀)")
    cheap_model: str = Field(default="claude-haiku-4-5-20251001", description="저비용 작업용 모델")
    embedding_model: str = Field(
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        description="로컬 임베딩 모델 (다국어 / FastEmbed 기본 지원)",
    )
    max_tokens_per_request: int = Field(default=4096, description="요청당 최대 토큰")


class QdrantConfig(BaseSettings):
    """Qdrant Vector DB 설정"""

    model_config = SettingsConfigDict(env_prefix="QDRANT_", env_file=_get_env_file(), extra="ignore")

    host: str = Field(default="localhost")
    port: int = Field(default=6333)
    api_key: str = Field(default="")
    url: str = Field(default="", description="Qdrant Cloud URL (설정 시 host/port 무시)")


class CostControlConfig(BaseSettings):
    """비용 제어 (KillSwitch) 설정"""

    model_config = SettingsConfigDict(env_prefix="ARIA_", env_file=_get_env_file(), extra="ignore")

    daily_cost_limit_usd: float = Field(default=10.0, description="일일 API 비용 상한 (USD)")
    monthly_cost_limit_usd: float = Field(default=300.0, description="월간 API 비용 상한 (USD)")


class APIConfig(BaseSettings):
    """FastAPI 서버 설정"""

    model_config = SettingsConfigDict(env_prefix="ARIA_", env_file=_get_env_file(), extra="ignore")

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8100)
    env: Environment = Field(default=Environment.DEVELOPMENT)
    log_level: str = Field(default="INFO")
    api_key: str = Field(default="aria-dev-key-change-me", description="API 인증 키")
    auth_disabled: bool = Field(
        default=False,
        description="True면 API 인증 스킵 (development 로컬 테스트용 / production에서는 무시됨)",
    )
    rate_limit_per_minute: int = Field(default=60)
    rate_limit_burst: int = Field(default=10, description="버스트 허용 횟수 (짧은 시간 집중 요청)")

    @model_validator(mode="after")
    def _enforce_production_auth(self) -> "APIConfig":
        """production/staging에서는 인증 스킵 불가 + 기본 키 사용 금지"""
        if self.env in (Environment.PRODUCTION, Environment.STAGING):
            if self.auth_disabled:
                raise ValueError(
                    f"ARIA_AUTH_DISABLED=true는 {self.env.value} 환경에서 사용할 수 없습니다"
                )
            if self.api_key == "aria-dev-key-change-me":
                raise ValueError(
                    f"기본 API 키를 {self.env.value} 환경에서 사용할 수 없습니다. "
                    "ARIA_API_KEY를 변경하세요"
                )
        return self


class MemoryConfig(BaseSettings):
    """3계층 메모리 시스템 설정"""

    model_config = SettingsConfigDict(env_prefix="ARIA_MEMORY_", env_file=_get_env_file(), extra="ignore")

    base_path: str = Field(default="./memory", description="메모리 파일 루트 경로")
    default_scope: str = Field(default="global", description="기본 스코프")
    token_budget: int = Field(default=4000, ge=100, le=32000, description="In-Context 토큰 예산")


class TelegramConfig(BaseSettings):
    """텔레그램 봇 설정"""

    model_config = SettingsConfigDict(env_prefix="ARIA_TELEGRAM_", env_file=_get_env_file(), extra="ignore")

    bot_token: str = Field(default="", description="텔레그램 봇 토큰 (@BotFather에서 발급)")
    chat_id: str = Field(default="", description="승재 전용 채팅 ID (다른 사용자 차단)")
    aria_base_url: str = Field(
        default="http://localhost:8100",
        description="ARIA API 서버 주소",
    )
    aria_api_key: str = Field(default="", description="ARIA API 인증 키")
    default_scope: str = Field(default="global", description="기본 메모리 스코프")
    default_collection: str = Field(default="default", description="기본 검색 컬렉션")
    request_timeout: int = Field(default=120, ge=10, le=300, description="ARIA API 요청 타임아웃 (초)")


class NotionConfig(BaseSettings):
    """Notion API 설정"""

    model_config = SettingsConfigDict(env_prefix="ARIA_NOTION_", env_file=_get_env_file(), extra="ignore")

    token: str = Field(default="", description="Notion Internal Integration Token")
    api_version: str = Field(default="2022-06-28", description="Notion API 버전")
    request_timeout: int = Field(default=30, ge=5, le=120, description="API 요청 타임아웃 (초)")

    @property
    def is_configured(self) -> bool:
        """Notion 토큰이 설정되어 있는지 확인"""
        return bool(self.token)


class KakaoMapConfig(BaseSettings):
    """카카오맵 로컬 API 설정

    REST API 키 기반 인증 (OAuth 불필요)
    발급: https://developers.kakao.com/console/app
    """

    model_config = SettingsConfigDict(env_prefix="ARIA_KAKAO_", env_file=_get_env_file(), extra="ignore")

    rest_api_key: str = Field(default="", description="카카오 REST API 키")
    request_timeout: int = Field(default=10, ge=3, le=60, description="API 요청 타임아웃 (초)")

    @property
    def is_configured(self) -> bool:
        return bool(self.rest_api_key)


class NaverSearchConfig(BaseSettings):
    """네이버 검색 + 지역 API 설정

    Client ID / Secret 기반 인증
    발급: https://developers.naver.com/apps/#/register
    """

    model_config = SettingsConfigDict(env_prefix="ARIA_NAVER_", env_file=_get_env_file(), extra="ignore")

    client_id: str = Field(default="", description="네이버 Client ID")
    client_secret: str = Field(default="", description="네이버 Client Secret")
    request_timeout: int = Field(default=10, ge=3, le=60, description="API 요청 타임아웃 (초)")

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


class TmapConfig(BaseSettings):
    """TMAP 대중교통 API 설정

    SK Open API App Key 기반 인증
    발급: https://openapi.sk.com/
    """

    model_config = SettingsConfigDict(env_prefix="ARIA_TMAP_", env_file=_get_env_file(), extra="ignore")

    app_key: str = Field(default="", description="SK Open API App Key")
    request_timeout: int = Field(default=15, ge=5, le=60, description="API 요청 타임아웃 (초)")

    @property
    def is_configured(self) -> bool:
        return bool(self.app_key)


class DuckDuckGoConfig(BaseSettings):
    """DuckDuckGo 웹 검색 설정

    API 키 불필요 — duckduckgo-search 패키지 사용
    글로벌/영어권 검색 + 한국어 검색 모두 지원
    """

    model_config = SettingsConfigDict(env_prefix="ARIA_DDG_", env_file=_get_env_file(), extra="ignore")

    enabled: bool = Field(default=True, description="DuckDuckGo 검색 도구 활성화")
    request_timeout: int = Field(default=10, ge=3, le=60, description="검색 타임아웃 (초)")

    @property
    def is_configured(self) -> bool:
        return self.enabled


class GoogleMapsConfig(BaseSettings):
    """Google Maps Platform API 설정

    API Key 기반 인증 (OAuth 불필요)
    발급: https://console.cloud.google.com/apis/credentials
    활성화 필요: Places API (New) / Geocoding API / Directions API
    """

    model_config = SettingsConfigDict(env_prefix="ARIA_GOOGLE_MAPS_", env_file=_get_env_file(), extra="ignore")

    api_key: str = Field(default="", description="Google Maps Platform API Key")
    request_timeout: int = Field(default=15, ge=5, le=60, description="API 요청 타임아웃 (초)")

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


class AlertConfig(BaseSettings):
    """능동 알림 시스템 설정"""

    model_config = SettingsConfigDict(env_prefix="ARIA_ALERT_", env_file=_get_env_file(), extra="ignore")

    enabled: bool = Field(default=True, description="능동 알림 활성화")
    cost_warning_threshold: float = Field(default=0.7, ge=0.1, le=1.0, description="비용 경고 임계치 (비율)")
    cost_critical_threshold: float = Field(default=0.9, ge=0.1, le=1.0, description="비용 긴급 임계치 (비율)")
    confidence_threshold: float = Field(default=0.3, ge=0.0, le=1.0, description="낮은 confidence 임계치")
    consecutive_error_threshold: int = Field(default=3, ge=1, le=20, description="연속 에러 알림 임계치")


class EventConfig(BaseSettings):
    """이벤트 수집 시스템 설정"""

    model_config = SettingsConfigDict(env_prefix="ARIA_EVENT_", env_file=_get_env_file(), extra="ignore")

    base_path: str = Field(default="./events", description="이벤트 파일 루트 경로")
    max_buffer_size: int = Field(default=1000, ge=100, le=10000, description="인메모리 버퍼 최대 크기")
    retention_days: int = Field(default=30, ge=1, le=365, description="이벤트 보관 기간 (일)")


class MonitoringConfig(BaseSettings):
    """서버 자동 모니터링 설정

    cron 스크립트 + ARIA Tool 공용
    환경변수 prefix: ARIA_MONITOR_
    """

    model_config = SettingsConfigDict(env_prefix="ARIA_MONITOR_", env_file=_get_env_file(), extra="ignore")

    enabled: bool = Field(default=True, description="모니터링 도구 활성화")
    targets: str = Field(
        default="",
        description="헬스체크 대상 URL 목록 (쉼표 구분 / 예: https://testorum.app,https://talksim.app)",
    )
    log_paths: str = Field(
        default="",
        description="에러 로그 파일 경로 (쉼표 구분 / 예: /var/log/nginx/error.log)",
    )
    check_ports: str = Field(
        default="22,80,443,3306,5432,6379,8100",
        description="보안 스캔 시 체크할 포트 목록 (쉼표 구분)",
    )
    healthcheck_timeout: float = Field(default=10.0, ge=1.0, le=60.0, description="헬스체크 타임아웃 (초)")
    anomaly_threshold: float = Field(default=3.0, ge=1.5, le=10.0, description="트래픽 이상 판정 배수")

    @property
    def is_configured(self) -> bool:
        return self.enabled

    @property
    def target_urls(self) -> list[str]:
        """파싱된 타겟 URL 목록"""
        if not self.targets:
            return []
        return [u.strip() for u in self.targets.split(",") if u.strip()]

    @property
    def log_path_list(self) -> list[str]:
        """파싱된 로그 경로 목록"""
        if not self.log_paths:
            return []
        return [p.strip() for p in self.log_paths.split(",") if p.strip()]

    @property
    def port_list(self) -> list[int]:
        """파싱된 포트 목록"""
        if not self.check_ports:
            return []
        try:
            return [int(p.strip()) for p in self.check_ports.split(",") if p.strip()]
        except ValueError:
            return [22, 80, 443, 8100]


class AriaConfig(BaseSettings):
    """ARIA 통합 설정"""

    model_config = SettingsConfigDict(env_file=_get_env_file(), extra="ignore")

    llm: LLMConfig = Field(default_factory=LLMConfig)
    qdrant: QdrantConfig = Field(default_factory=QdrantConfig)
    cost_control: CostControlConfig = Field(default_factory=CostControlConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    notion: NotionConfig = Field(default_factory=NotionConfig)
    kakao_map: KakaoMapConfig = Field(default_factory=KakaoMapConfig)
    naver_search: NaverSearchConfig = Field(default_factory=NaverSearchConfig)
    tmap: TmapConfig = Field(default_factory=TmapConfig)
    ddg: DuckDuckGoConfig = Field(default_factory=DuckDuckGoConfig)
    google_maps: GoogleMapsConfig = Field(default_factory=GoogleMapsConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    event: EventConfig = Field(default_factory=EventConfig)
    alert: AlertConfig = Field(default_factory=AlertConfig)

    # Provider API Keys (LiteLLM이 자동 참조)
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    google_api_key: str = Field(default="", alias="GOOGLE_API_KEY")
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")

    # 모델 prefix → API 키 필드 매핑
    _MODEL_KEY_MAP: dict[str, str] = {
        "claude": "anthropic_api_key",
        "anthropic": "anthropic_api_key",
        "gpt": "openai_api_key",
        "o1": "openai_api_key",
        "o3": "openai_api_key",
        "openai": "openai_api_key",
        "gemini": "google_api_key",
        "deepseek": "deepseek_api_key",
    }

    def get_api_key_for_model(self, model: str) -> str:
        """모델명에 대응하는 API 키 반환 (빈 문자열이면 미설정)"""
        model_lower = model.lower()
        for prefix, key_field in self._MODEL_KEY_MAP.items():
            if prefix in model_lower:
                return getattr(self, key_field, "")
        return ""

    def has_api_key_for_model(self, model: str) -> bool:
        """해당 모델의 API 키가 설정되어 있는지 확인"""
        return bool(self.get_api_key_for_model(model))

    def get_available_models(self) -> list[str]:
        """API 키가 설정된 모델 목록 반환"""
        all_models = [
            self.llm.default_model,
            self.llm.fallback_model,
            self.llm.cheap_model,
        ]
        return [m for m in all_models if self.has_api_key_for_model(m)]

    def get_missing_key_models(self) -> list[tuple[str, str]]:
        """API 키가 없는 모델과 필요한 환경변수명 반환 → [(model, env_var), ...]"""
        _KEY_FIELD_TO_ENV: dict[str, str] = {
            "anthropic_api_key": "ANTHROPIC_API_KEY",
            "openai_api_key": "OPENAI_API_KEY",
            "google_api_key": "GOOGLE_API_KEY",
            "deepseek_api_key": "DEEPSEEK_API_KEY",
        }
        missing: list[tuple[str, str]] = []
        for model in {self.llm.default_model, self.llm.fallback_model, self.llm.cheap_model}:
            model_lower = model.lower()
            for prefix, key_field in self._MODEL_KEY_MAP.items():
                if prefix in model_lower:
                    if not getattr(self, key_field, ""):
                        env_var = _KEY_FIELD_TO_ENV.get(key_field, key_field.upper())
                        missing.append((model, env_var))
                    break
        return missing


@lru_cache()
def get_config() -> AriaConfig:
    """싱글톤 설정 인스턴스 반환"""
    return AriaConfig()
