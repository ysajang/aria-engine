"""ARIA Engine - Google OAuth2 Token Manager

refresh_token 기반 access_token 자동 갱신
- 최초 인증: scripts/google_auth_setup.py로 refresh_token 획득
- 런타임: refresh_token → access_token 자동 갱신 (1시간 만료)
- 메모리 캐시: 만료 전까지 동일 access_token 재사용

사용법:
    token_mgr = GoogleTokenManager(config)
    access_token = await token_mgr.get_access_token()
    # → 유효한 access_token 반환 (만료 시 자동 갱신)
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from aria.core.config import GoogleOAuthConfig

logger = structlog.get_logger()

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# 만료 5분 전에 선제적 갱신 (네트워크 지연 대비)
EXPIRY_BUFFER_SECONDS = 300


class GoogleTokenManager:
    """Google OAuth2 액세스 토큰 관리자

    Thread-safe하지 않음 (ARIA는 단일 asyncio 루프)
    refresh_token은 .env에서 로드 → access_token은 메모리 캐시

    Args:
        config: GoogleOAuthConfig (client_id + client_secret + refresh_token)
    """

    def __init__(self, config: GoogleOAuthConfig) -> None:
        self._config = config
        self._access_token: str = ""
        self._expires_at: float = 0.0  # Unix timestamp

    @property
    def is_configured(self) -> bool:
        return self._config.is_configured

    @property
    def has_valid_token(self) -> bool:
        """현재 캐시된 토큰이 유효한지 확인"""
        if not self._access_token:
            return False
        return time.time() < (self._expires_at - EXPIRY_BUFFER_SECONDS)

    async def get_access_token(self) -> str:
        """유효한 access_token 반환 (만료 시 자동 갱신)

        Returns:
            access_token 문자열

        Raises:
            GoogleAuthError: 토큰 갱신 실패
        """
        if self.has_valid_token:
            return self._access_token

        return await self._refresh_token()

    async def _refresh_token(self) -> str:
        """refresh_token으로 새 access_token 획득

        POST https://oauth2.googleapis.com/token
        """
        if not self._config.refresh_token:
            raise GoogleAuthError(
                "refresh_token이 설정되지 않았습니다. "
                "scripts/google_auth_setup.py를 실행하여 인증을 완료하세요"
            )

        payload = {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "refresh_token": self._config.refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(TOKEN_ENDPOINT, data=payload)

            if resp.status_code != 200:
                error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                error_desc = error_data.get("error_description", resp.text[:200])
                error_code = error_data.get("error", "unknown")

                if error_code == "invalid_grant":
                    raise GoogleAuthError(
                        "refresh_token이 만료되었습니다. "
                        "scripts/google_auth_setup.py를 다시 실행하세요"
                    )

                raise GoogleAuthError(
                    f"토큰 갱신 실패 (HTTP {resp.status_code}): {error_desc}"
                )

            data = resp.json()
            self._access_token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            self._expires_at = time.time() + expires_in

            logger.info(
                "google_token_refreshed",
                expires_in=expires_in,
            )

            return self._access_token

        except httpx.ConnectError:
            raise GoogleAuthError("Google OAuth2 서버 연결 실패")
        except httpx.TimeoutException:
            raise GoogleAuthError("Google OAuth2 토큰 갱신 타임아웃")
        except GoogleAuthError:
            raise
        except Exception as e:
            raise GoogleAuthError(f"토큰 갱신 중 예외: {str(e)[:200]}")

    def invalidate(self) -> None:
        """현재 캐시된 토큰 무효화 (401 응답 시 호출)"""
        self._access_token = ""
        self._expires_at = 0.0


class GoogleAuthError(Exception):
    """Google OAuth2 인증 에러"""
    pass


async def exchange_code_for_tokens(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob",
) -> dict[str, Any]:
    """Authorization code를 토큰으로 교환 (최초 인증용)

    scripts/google_auth_setup.py에서 사용

    Args:
        client_id: OAuth2 클라이언트 ID
        client_secret: OAuth2 클라이언트 시크릿
        code: 사용자 인증 후 받은 authorization code
        redirect_uri: 리다이렉트 URI

    Returns:
        {access_token, refresh_token, expires_in, token_type, scope}
    """
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(TOKEN_ENDPOINT, data=payload)

    if resp.status_code != 200:
        error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        raise GoogleAuthError(
            f"코드 교환 실패: {error_data.get('error_description', resp.text[:200])}"
        )

    return resp.json()
