"""ARIA Engine - Custom Exceptions

모든 ARIA 에러를 구조화하여 일관된 에러 응답 제공
- AriaError: 베이스 예외
- KillSwitchError: 비용 상한 초과
- LLMProviderError: LLM 호출 실패 (모든 fallback 소진)
- VectorStoreError: 벡터DB 관련 에러
- AgentError: 에이전트 실행 에러
- MemoryError: 메모리 시스템 베이스 예외
- VersionConflictError: 낙관적 락 충돌 (read-before-write 위반)
- MemoryNotFoundError: 토픽/인덱스 미존재
- MemoryStorageError: 파일 I/O 실패
- MemoryScopeError: 유효하지 않은 스코프
"""

from __future__ import annotations


class AriaError(Exception):
    """ARIA 엔진 베이스 예외"""

    def __init__(self, message: str, *, code: str = "ARIA_ERROR", details: dict | None = None) -> None:
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(message)


class KillSwitchError(AriaError):
    """비용 상한 초과 시 발생"""

    def __init__(self, message: str, *, daily_cost: float = 0.0, monthly_cost: float = 0.0) -> None:
        super().__init__(
            message,
            code="KILLSWITCH_TRIGGERED",
            details={"daily_cost_usd": daily_cost, "monthly_cost_usd": monthly_cost},
        )


class LLMProviderError(AriaError):
    """LLM 호출 실패 (모든 fallback 소진 포함)"""

    def __init__(self, message: str, *, model: str = "", attempts: list[dict] | None = None) -> None:
        super().__init__(
            message,
            code="LLM_PROVIDER_ERROR",
            details={"model": model, "attempts": attempts or []},
        )


class LLMAllProvidersExhaustedError(LLMProviderError):
    """모든 LLM 프로바이더/모델이 실패"""

    def __init__(self, attempts: list[dict]) -> None:
        models_tried = [a.get("model", "unknown") for a in attempts]
        super().__init__(
            f"모든 LLM 프로바이더 실패: {', '.join(models_tried)}",
            attempts=attempts,
        )
        self.code = "ALL_PROVIDERS_EXHAUSTED"


class VectorStoreError(AriaError):
    """벡터DB 관련 에러"""

    def __init__(self, message: str, *, collection: str = "") -> None:
        super().__init__(
            message,
            code="VECTOR_STORE_ERROR",
            details={"collection": collection},
        )


class CollectionNotFoundError(VectorStoreError):
    """컬렉션 미존재"""

    def __init__(self, collection: str) -> None:
        super().__init__(f"컬렉션을 찾을 수 없습니다: {collection}", collection=collection)
        self.code = "COLLECTION_NOT_FOUND"


class AgentError(AriaError):
    """에이전트 실행 에러"""

    def __init__(self, message: str, *, query: str = "", iteration: int = 0) -> None:
        super().__init__(
            message,
            code="AGENT_ERROR",
            details={"query": query[:200], "iteration": iteration},
        )


class NoAPIKeyError(AriaError):
    """LLM 호출에 필요한 API 키 미설정"""

    def __init__(self, model: str, env_var: str) -> None:
        super().__init__(
            f"'{model}' 호출에 필요한 API 키가 설정되지 않았습니다. "
            f".env 파일에 {env_var}를 설정하세요.",
            code="NO_API_KEY",
            details={"model": model, "required_env_var": env_var},
        )


# === Memory System Exceptions ===


class MemoryError(AriaError):
    """메모리 시스템 베이스 예외"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "MEMORY_ERROR",
        scope: str = "",
        domain: str = "",
    ) -> None:
        super().__init__(
            message,
            code=code,
            details={"scope": scope, "domain": domain},
        )


class VersionConflictError(MemoryError):
    """낙관적 락 충돌 — read-before-write 위반

    기대 버전과 실제 버전이 불일치할 때 발생
    클라이언트는 최신 버전을 다시 읽고 재시도해야 함
    """

    def __init__(
        self,
        *,
        scope: str,
        domain: str,
        expected_version: int,
        actual_version: int,
    ) -> None:
        super().__init__(
            f"버전 충돌: '{domain}' (scope={scope}) "
            f"expected_version={expected_version} / actual_version={actual_version} — "
            f"최신 버전을 읽고 재시도하세요",
            code="VERSION_CONFLICT",
            scope=scope,
            domain=domain,
        )
        self.details["expected_version"] = expected_version
        self.details["actual_version"] = actual_version


class MemoryNotFoundError(MemoryError):
    """토픽 또는 인덱스 미존재"""

    def __init__(self, *, scope: str, domain: str = "") -> None:
        target = f"토픽 '{domain}'" if domain else "인덱스"
        super().__init__(
            f"{target}을(를) 찾을 수 없습니다 (scope={scope})",
            code="MEMORY_NOT_FOUND",
            scope=scope,
            domain=domain,
        )


class MemoryStorageError(MemoryError):
    """파일 I/O 실패 (읽기/쓰기/삭제)"""

    def __init__(self, message: str, *, scope: str = "", domain: str = "") -> None:
        super().__init__(
            message,
            code="MEMORY_STORAGE_ERROR",
            scope=scope,
            domain=domain,
        )


class MemoryScopeError(MemoryError):
    """유효하지 않은 스코프"""

    def __init__(self, scope: str) -> None:
        super().__init__(
            f"유효하지 않은 메모리 스코프: '{scope}'",
            code="MEMORY_SCOPE_INVALID",
            scope=scope,
        )


# === Tool Safety Exceptions ===


class ToolExecutionBlockedError(AriaError):
    """Critic이 UNSAFE로 판단하여 도구 실행 차단

    Phase 3 Tool Integration에서 사용
    """

    def __init__(self, *, tool_name: str, reason: str, risk_factors: list[str] | None = None) -> None:
        super().__init__(
            f"도구 실행 차단: '{tool_name}' — {reason}",
            code="TOOL_EXECUTION_BLOCKED",
            details={
                "tool_name": tool_name,
                "reason": reason,
                "risk_factors": risk_factors or [],
            },
        )


# === Learning System Exceptions (Phase 5) ===


class LearningError(AriaError):
    """자기 학습 시스템 베이스 예외

    학습 실패는 메인 파이프라인을 방해하면 안 됨
    BaseLearner.safe_analyze()에서 catch → graceful degradation
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "LEARNING_ERROR",
        learner: str = "",
    ) -> None:
        super().__init__(
            message,
            code=code,
            details={"learner": learner},
        )


class LearningAnalysisError(LearningError):
    """학습 분석 실패 (LLM 호출 실패 / JSON 파싱 실패 등)"""

    def __init__(self, message: str, *, learner: str = "") -> None:
        super().__init__(
            message,
            code="LEARNING_ANALYSIS_ERROR",
            learner=learner,
        )
