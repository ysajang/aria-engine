"""ARIA Engine - Telegram ARIA API Client

httpx 기반 ARIA API 클라이언트
텔레그램 봇에서 ARIA 서버(/v1/query, /v1/cost, /v1/memory)를 호출할 때 사용
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


class ARIAClient:
    """ARIA API HTTP 클라이언트

    Args:
        base_url: ARIA 서버 주소 (예: http://localhost:8100)
        api_key: API 인증 키
        timeout: 요청 타임아웃 (초)
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: int = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            self._headers["X-API-Key"] = api_key
        self._timeout = timeout

    async def query(
        self,
        query: str,
        *,
        scope: str = "global",
        collection: str = "default",
        memory_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """ARIA 에이전트에게 질문

        Returns:
            {"answer": str, "confidence": float, "tool_calls_made": int, ...}
            또는 에러 시 {"error": str, "message": str}
        """
        payload: dict[str, Any] = {
            "query": query,
            "scope": scope,
            "collection": collection,
        }
        if memory_domains:
            payload["memory_domains"] = memory_domains

        return await self._post("/v1/query", payload)

    async def get_cost(self) -> dict[str, Any]:
        """비용 현황 조회"""
        return await self._get("/v1/cost")

    async def get_memory_index(self, scope: str = "global") -> dict[str, Any]:
        """메모리 인덱스 조회"""
        return await self._get(f"/v1/memory/{scope}/index")

    async def health_check(self) -> dict[str, Any]:
        """헬스 체크"""
        return await self._get("/v1/health")

    async def execute_pending(self, confirmation_id: str) -> dict[str, Any]:
        """승인된 대기 도구 실행"""
        return await self._post(
            f"/v1/tools/pending/{confirmation_id}/execute",
            {},
        )

    async def deny_pending(self, confirmation_id: str) -> dict[str, Any]:
        """대기 도구 거부"""
        return await self._delete(f"/v1/tools/pending/{confirmation_id}")

    async def list_workflows(self, category: str | None = None) -> dict[str, Any]:
        """워크플로우 목록 조회"""
        path = "/v1/workflows"
        if category:
            path += f"?category={category}"
        return await self._get(path)

    async def execute_workflow(
        self,
        workflow_id: str,
        initial_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """워크플로우 실행"""
        payload = {"initial_data": initial_data or {}}
        return await self._post(f"/v1/workflows/{workflow_id}/execute", payload)

    async def _get(self, path: str) -> dict[str, Any]:
        """GET 요청"""
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, headers=self._headers)
                return self._handle_response(response, path)
        except httpx.TimeoutException:
            logger.error("aria_client_timeout", path=path, timeout=self._timeout)
            return {"error": "TIMEOUT", "message": f"ARIA 서버 응답 시간 초과 ({self._timeout}초)"}
        except httpx.ConnectError:
            logger.error("aria_client_connect_error", path=path, base_url=self.base_url)
            return {"error": "CONNECTION_ERROR", "message": f"ARIA 서버 연결 실패: {self.base_url}"}
        except Exception as e:
            logger.error("aria_client_error", path=path, error=str(e)[:200])
            return {"error": "CLIENT_ERROR", "message": str(e)[:300]}

    async def _delete(self, path: str) -> dict[str, Any]:
        """DELETE 요청"""
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.delete(url, headers=self._headers)
                return self._handle_response(response, path)
        except httpx.TimeoutException:
            logger.error("aria_client_timeout", path=path, timeout=self._timeout)
            return {"error": "TIMEOUT", "message": f"ARIA 서버 응답 시간 초과 ({self._timeout}초)"}
        except httpx.ConnectError:
            logger.error("aria_client_connect_error", path=path, base_url=self.base_url)
            return {"error": "CONNECTION_ERROR", "message": f"ARIA 서버 연결 실패: {self.base_url}"}
        except Exception as e:
            logger.error("aria_client_error", path=path, error=str(e)[:200])
            return {"error": "CLIENT_ERROR", "message": str(e)[:300]}

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST 요청"""
        import json as json_mod

        url = f"{self.base_url}{path}"
        # ensure_ascii=False: 한글을 \uXXXX로 이스케이프하지 않고 UTF-8로 직접 인코딩
        body = json_mod.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, content=body, headers=self._headers)
                return self._handle_response(response, path)
        except httpx.TimeoutException:
            logger.error("aria_client_timeout", path=path, timeout=self._timeout)
            return {"error": "TIMEOUT", "message": f"ARIA 서버 응답 시간 초과 ({self._timeout}초)"}
        except httpx.ConnectError:
            logger.error("aria_client_connect_error", path=path, base_url=self.base_url)
            return {"error": "CONNECTION_ERROR", "message": f"ARIA 서버 연결 실패: {self.base_url}"}
        except Exception as e:
            logger.error("aria_client_error", path=path, error=str(e)[:200])
            return {"error": "CLIENT_ERROR", "message": str(e)[:300]}

    @staticmethod
    def _handle_response(response: httpx.Response, path: str) -> dict[str, Any]:
        """HTTP 응답 처리"""
        if response.status_code == 200:
            return response.json()

        # 에러 응답
        try:
            error_body = response.json()
        except Exception:
            error_body = {"message": response.text[:300]}

        logger.warning(
            "aria_api_error",
            path=path,
            status_code=response.status_code,
            error=error_body.get("message", "")[:200],
        )
        return {
            "error": error_body.get("error", f"HTTP_{response.status_code}"),
            "message": error_body.get("message", f"HTTP {response.status_code}"),
        }
