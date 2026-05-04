"""ARIA Engine - MCP Tool: Gmail

Gmail API v1 기반 도구 4종
- GmailSearchTool: 이메일 검색 (Gmail 검색 문법 지원)
- GmailReadTool: 이메일/스레드 읽기
- GmailSendTool: 이메일 발송 (Critic NEEDS_CONFIRMATION)
- GmailDraftTool: 초안 생성 (Critic NEEDS_CONFIRMATION)

인증: OAuth2 (GoogleTokenManager)
스코프: https://www.googleapis.com/auth/gmail.modify
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Any

import httpx
import structlog

from aria.auth.google_oauth import GoogleAuthError, GoogleTokenManager
from aria.tools.tool_types import (
    SafetyLevelHint,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolResult,
)

logger = structlog.get_logger()

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailClient:
    """Gmail API 클라이언트

    GoogleTokenManager를 통한 자동 토큰 갱신
    401 응답 시 토큰 무효화 → 재시도 1회

    Args:
        token_manager: GoogleTokenManager 인스턴스
    """

    def __init__(self, token_manager: GoogleTokenManager) -> None:
        self._token_mgr = token_manager
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self._client

    async def _headers(self) -> dict[str, str]:
        token = await self._token_mgr.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """API 요청 + 401 자동 재인증"""
        client = await self._get_client()
        headers = await self._headers()

        resp = await client.request(method, url, headers=headers, **kwargs)

        # 401 → 토큰 무효화 후 1회 재시도
        if resp.status_code == 401:
            self._token_mgr.invalidate()
            headers = await self._headers()
            resp = await client.request(method, url, headers=headers, **kwargs)

        resp.raise_for_status()
        return resp.json() if resp.text else {}

    async def search(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """이메일 검색 → 메시지 ID + 스니펫 목록"""
        data = await self._request(
            "GET",
            f"{GMAIL_BASE}/messages",
            params={"q": query, "maxResults": min(max(max_results, 1), 50)},
        )

        messages = data.get("messages", [])
        if not messages:
            return []

        # 각 메시지의 스니펫 + 헤더 가져오기
        results = []
        for msg_ref in messages[:max_results]:
            msg = await self._request(
                "GET",
                f"{GMAIL_BASE}/messages/{msg_ref['id']}",
                params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
            )
            headers_map = _extract_headers(msg.get("payload", {}).get("headers", []))
            results.append({
                "id": msg.get("id", ""),
                "thread_id": msg.get("threadId", ""),
                "snippet": msg.get("snippet", ""),
                "from": headers_map.get("From", ""),
                "to": headers_map.get("To", ""),
                "subject": headers_map.get("Subject", ""),
                "date": headers_map.get("Date", ""),
                "label_ids": msg.get("labelIds", []),
            })

        return results

    async def read_message(self, message_id: str) -> dict[str, Any]:
        """메시지 전체 내용 읽기"""
        msg = await self._request(
            "GET",
            f"{GMAIL_BASE}/messages/{message_id}",
            params={"format": "full"},
        )

        headers_map = _extract_headers(msg.get("payload", {}).get("headers", []))
        body = _extract_body(msg.get("payload", {}))

        return {
            "id": msg.get("id", ""),
            "thread_id": msg.get("threadId", ""),
            "from": headers_map.get("From", ""),
            "to": headers_map.get("To", ""),
            "subject": headers_map.get("Subject", ""),
            "date": headers_map.get("Date", ""),
            "body": body,
            "label_ids": msg.get("labelIds", []),
        }

    async def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
    ) -> dict[str, Any]:
        """이메일 발송"""
        raw = _create_raw_message(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        data = await self._request(
            "POST",
            f"{GMAIL_BASE}/messages/send",
            json={"raw": raw},
        )
        return {"id": data.get("id", ""), "thread_id": data.get("threadId", "")}

    async def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
    ) -> dict[str, Any]:
        """초안 생성"""
        raw = _create_raw_message(to=to, subject=subject, body=body, cc=cc)
        data = await self._request(
            "POST",
            f"{GMAIL_BASE}/drafts",
            json={"message": {"raw": raw}},
        )
        return {
            "draft_id": data.get("id", ""),
            "message_id": data.get("message", {}).get("id", ""),
        }

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# === 유틸리티 ===


def _extract_headers(headers: list[dict[str, str]]) -> dict[str, str]:
    """Gmail 헤더 목록 → dict 변환"""
    return {h["name"]: h["value"] for h in headers}


def _extract_body(payload: dict[str, Any]) -> str:
    """Gmail payload에서 본문 텍스트 추출 (plain > html)"""
    # 단일 파트
    if payload.get("mimeType", "").startswith("text/plain"):
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # 멀티파트
    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # HTML fallback
    for part in parts:
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                return f"[HTML]\n{html[:2000]}"

    # 중첩 멀티파트
    for part in parts:
        if part.get("parts"):
            nested = _extract_body(part)
            if nested:
                return nested

    return "(본문 없음)"


def _create_raw_message(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> str:
    """RFC 2822 이메일 → base64url 인코딩"""
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return raw


# === Tool Executors ===


class GmailSearchTool(ToolExecutor):
    """Gmail 이메일 검색"""

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="gmail_search",
            description=(
                "Gmail에서 이메일을 검색합니다. "
                "Gmail 검색 문법을 지원합니다 (from: / to: / subject: / has:attachment / after: / before: 등). "
                "검색 결과로 제목, 발신자, 날짜, 스니펫을 반환합니다."
            ),
            parameters=[
                ToolParameter(name="query", type="string", description="검색 쿼리 (예: 'from:user@example.com subject:invoice after:2026/01/01')", required=True),
                ToolParameter(name="max_results", type="integer", description="최대 결과 수 (1~50 / 기본값 10)", required=False, default=10),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        query = parameters.get("query", "").strip()
        if not query:
            return ToolResult(tool_name="gmail_search", success=False, error="query가 비어있습니다")

        max_results = int(parameters.get("max_results", 10))

        try:
            results = await self._client.search(query=query, max_results=max_results)
            logger.info("gmail_search_success", query=query[:50], count=len(results))
            return ToolResult(
                tool_name="gmail_search",
                success=True,
                output={"query": query, "results": results, "result_count": len(results)},
            )
        except GoogleAuthError as e:
            return ToolResult(tool_name="gmail_search", success=False, error=f"인증 실패: {str(e)}")
        except httpx.HTTPStatusError as e:
            return ToolResult(tool_name="gmail_search", success=False, error=f"Gmail API 에러 (HTTP {e.response.status_code})")
        except Exception as e:
            return ToolResult(tool_name="gmail_search", success=False, error=f"검색 실패: {str(e)[:300]}")


class GmailReadTool(ToolExecutor):
    """Gmail 이메일 읽기"""

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="gmail_read",
            description=(
                "Gmail 이메일의 전체 내용을 읽습니다. "
                "gmail_search로 얻은 message_id를 사용합니다. "
                "발신자, 수신자, 제목, 날짜, 본문을 반환합니다."
            ),
            parameters=[
                ToolParameter(name="message_id", type="string", description="읽을 메시지 ID (gmail_search 결과에서 획득)", required=True),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        message_id = parameters.get("message_id", "").strip()
        if not message_id:
            return ToolResult(tool_name="gmail_read", success=False, error="message_id가 비어있습니다")

        try:
            result = await self._client.read_message(message_id)
            logger.info("gmail_read_success", message_id=message_id)
            return ToolResult(tool_name="gmail_read", success=True, output=result)
        except GoogleAuthError as e:
            return ToolResult(tool_name="gmail_read", success=False, error=f"인증 실패: {str(e)}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return ToolResult(tool_name="gmail_read", success=False, error="메시지를 찾을 수 없습니다")
            return ToolResult(tool_name="gmail_read", success=False, error=f"Gmail API 에러 (HTTP {e.response.status_code})")
        except Exception as e:
            return ToolResult(tool_name="gmail_read", success=False, error=f"읽기 실패: {str(e)[:300]}")


class GmailSendTool(ToolExecutor):
    """Gmail 이메일 발송 (HITL 확인 필요)"""

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="gmail_send",
            description=(
                "Gmail로 이메일을 발송합니다. "
                "수신자, 제목, 본문을 지정하며 CC/BCC도 지원합니다. "
                "외부 전송 행위이므로 Critic이 사용자 확인을 요청합니다."
            ),
            parameters=[
                ToolParameter(name="to", type="string", description="수신자 이메일 주소", required=True),
                ToolParameter(name="subject", type="string", description="이메일 제목", required=True),
                ToolParameter(name="body", type="string", description="이메일 본문 (평문)", required=True),
                ToolParameter(name="cc", type="string", description="CC 수신자 (쉼표 구분)", required=False),
                ToolParameter(name="bcc", type="string", description="BCC 수신자 (쉼표 구분)", required=False),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.EXTERNAL,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        to = parameters.get("to", "").strip()
        subject = parameters.get("subject", "").strip()
        body = parameters.get("body", "").strip()

        if not to:
            return ToolResult(tool_name="gmail_send", success=False, error="수신자(to)가 비어있습니다")
        if not subject:
            return ToolResult(tool_name="gmail_send", success=False, error="제목(subject)이 비어있습니다")
        if not body:
            return ToolResult(tool_name="gmail_send", success=False, error="본문(body)이 비어있습니다")

        cc = parameters.get("cc", "")
        bcc = parameters.get("bcc", "")

        try:
            result = await self._client.send_message(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
            logger.info("gmail_send_success", to=to, subject=subject[:30])
            return ToolResult(
                tool_name="gmail_send",
                success=True,
                output={"status": "sent", "to": to, "subject": subject, **result},
            )
        except GoogleAuthError as e:
            return ToolResult(tool_name="gmail_send", success=False, error=f"인증 실패: {str(e)}")
        except Exception as e:
            return ToolResult(tool_name="gmail_send", success=False, error=f"발송 실패: {str(e)[:300]}")


class GmailDraftTool(ToolExecutor):
    """Gmail 초안 생성"""

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="gmail_draft",
            description=(
                "Gmail에 이메일 초안을 생성합니다. "
                "바로 발송하지 않고 초안함에 저장됩니다. "
                "승재가 확인 후 직접 발송할 수 있습니다."
            ),
            parameters=[
                ToolParameter(name="to", type="string", description="수신자 이메일 주소", required=True),
                ToolParameter(name="subject", type="string", description="이메일 제목", required=True),
                ToolParameter(name="body", type="string", description="이메일 본문 (평문)", required=True),
                ToolParameter(name="cc", type="string", description="CC 수신자 (쉼표 구분)", required=False),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.WRITE,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        to = parameters.get("to", "").strip()
        subject = parameters.get("subject", "").strip()
        body = parameters.get("body", "").strip()

        if not to:
            return ToolResult(tool_name="gmail_draft", success=False, error="수신자(to)가 비어있습니다")
        if not subject:
            return ToolResult(tool_name="gmail_draft", success=False, error="제목(subject)이 비어있습니다")

        cc = parameters.get("cc", "")

        try:
            result = await self._client.create_draft(to=to, subject=subject, body=body, cc=cc)
            logger.info("gmail_draft_created", to=to, subject=subject[:30])
            return ToolResult(
                tool_name="gmail_draft",
                success=True,
                output={"status": "draft_created", "to": to, "subject": subject, **result},
            )
        except GoogleAuthError as e:
            return ToolResult(tool_name="gmail_draft", success=False, error=f"인증 실패: {str(e)}")
        except Exception as e:
            return ToolResult(tool_name="gmail_draft", success=False, error=f"초안 생성 실패: {str(e)[:300]}")
