#!/usr/bin/env python3
"""ARIA Engine - Google OAuth2 Setup

최초 1회 실행하여 refresh_token을 획득하는 스크립트

사전 준비:
1. Google Cloud Console → API 및 서비스 → 사용자 인증 정보
2. "OAuth 2.0 클라이언트 ID" 생성 (유형: 데스크톱 앱)
3. Gmail API + Google Calendar API 활성화

사용법:
    python scripts/google_auth_setup.py \\
        --client-id YOUR_CLIENT_ID \\
        --client-secret YOUR_CLIENT_SECRET

플로우:
1. 브라우저에서 Google 로그인 URL 열기
2. 승재가 Google 계정 승인
3. 리다이렉트된 URL에서 authorization code 복사
4. code → refresh_token 교환
5. .env에 추가할 값 출력
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import urllib.parse
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"

# Gmail + Calendar 스코프
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]


def build_auth_url(client_id: str) -> str:
    """브라우저에서 열 인증 URL 생성"""
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


async def main(client_id: str, client_secret: str) -> int:
    print("=" * 60)
    print("ARIA Engine — Google OAuth2 Setup")
    print("=" * 60)
    print()

    # Step 1: 인증 URL 생성
    auth_url = build_auth_url(client_id)
    print("[Step 1] 아래 URL을 브라우저에서 열어주세요:")
    print()
    print(f"  {auth_url}")
    print()
    print("Google 계정 로그인 → ARIA 앱 승인")
    print()

    # Step 2: Authorization code 입력
    code = input("[Step 2] 승인 후 받은 인증 코드를 붙여넣으세요: ").strip()
    if not code:
        print("❌ 인증 코드가 비어있습니다")
        return 1

    # Step 3: 토큰 교환
    print()
    print("[Step 3] 토큰 교환 중...")
    try:
        from aria.auth.google_oauth import exchange_code_for_tokens

        tokens = await exchange_code_for_tokens(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=REDIRECT_URI,
        )
    except Exception as e:
        print(f"❌ 토큰 교환 실패: {e}")
        return 1

    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        print("❌ refresh_token을 받지 못했습니다")
        print("   → 이미 인증한 적이 있다면 Google 계정 설정에서")
        print("   → '서드파티 앱 및 서비스'에서 ARIA 제거 후 다시 시도하세요")
        return 1

    # Step 4: .env 추가 안내
    print()
    print("✅ 인증 완료! .env에 아래 값을 추가하세요:")
    print()
    print(f"ARIA_GOOGLE_OAUTH_CLIENT_ID={client_id}")
    print(f"ARIA_GOOGLE_OAUTH_CLIENT_SECRET={client_secret}")
    print(f"ARIA_GOOGLE_OAUTH_REFRESH_TOKEN={refresh_token}")
    print()
    print("추가 후 ARIA 서버를 재시작하면 Gmail + Calendar 도구가 활성화됩니다")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ARIA Google OAuth2 인증 설정",
    )
    parser.add_argument("--client-id", required=True, help="OAuth2 클라이언트 ID")
    parser.add_argument("--client-secret", required=True, help="OAuth2 클라이언트 시크릿")
    args = parser.parse_args()

    exit_code = asyncio.run(main(args.client_id, args.client_secret))
    sys.exit(exit_code)
