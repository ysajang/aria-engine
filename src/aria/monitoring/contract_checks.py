"""ARIA Engine - API Contract Testing Checks

제품 API 엔드포인트 응답 스키마 검증 로직 (ToolExecutor + cron 공용)
- check_endpoint: 단일 엔드포인트 호출 + 스키마 검증
- run_contract_tests: 제품별 엔드포인트 목록 통합 테스트
- validate_schema: JSON Schema Draft 7 경량 검증

설계 원칙:
- AI API 호출 0 — 순수 HTTP 호출 + 스키마 비교
- jsonschema 라이브러리 미사용 — 핵심 타입 검증만 경량 구현
- 모든 함수는 dict 반환 → ToolResult.output / EventInput.data 양쪽 사용
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

DEFAULT_TIMEOUT = 15.0


# ============================================================
# 경량 JSON Schema 검증기 (외부 의존성 없음)
# ============================================================

def validate_schema(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """경량 JSON Schema 검증 (Draft 7 서브셋)

    지원하는 키워드:
    - type: string/number/integer/boolean/array/object/null
    - required: 필수 필드 목록
    - properties: 하위 필드 스키마
    - items: 배열 요소 스키마
    - enum: 허용 값 목록
    - minimum/maximum: 숫자 범위
    - minLength/maxLength: 문자열 길이

    Returns:
        에러 메시지 리스트 (빈 리스트면 통과)
    """
    errors: list[str] = []

    # type 검증
    expected_type = schema.get("type")
    if expected_type:
        type_map: dict[str, type | tuple[type, ...]] = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
            "null": type(None),
        }

        if isinstance(expected_type, list):
            # union type: ["string", "null"]
            valid_types = tuple(
                t for et in expected_type
                for t in (type_map.get(et, ()) if isinstance(type_map.get(et), tuple) else (type_map.get(et, type(None)),))
            )
            if not isinstance(data, valid_types):
                errors.append(f"{path}: 타입 불일치 — 예상 {expected_type} / 실제 {type(data).__name__}")
                return errors
        else:
            valid = type_map.get(expected_type)
            if not isinstance(data, valid):
                errors.append(f"{path}: 타입 불일치 — 예상 {expected_type} / 실제 {type(data).__name__}")
                return errors
            # Python에서 bool은 int의 서브클래스 → integer 요청 시 bool 거부
            if expected_type == "integer" and isinstance(data, bool):
                errors.append(f"{path}: 타입 불일치 — 예상 integer / 실제 boolean")
                return errors

    # enum 검증
    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: 허용 값 {schema['enum']}에 {data!r} 없음")

    # 문자열 길이
    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            errors.append(f"{path}: 최소 길이 {schema['minLength']} 미달 (실제 {len(data)})")
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            errors.append(f"{path}: 최대 길이 {schema['maxLength']} 초과 (실제 {len(data)})")

    # 숫자 범위
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errors.append(f"{path}: 최솟값 {schema['minimum']} 미달 (실제 {data})")
        if "maximum" in schema and data > schema["maximum"]:
            errors.append(f"{path}: 최댓값 {schema['maximum']} 초과 (실제 {data})")

    # object 검증
    if isinstance(data, dict):
        # required 필드
        for req_field in schema.get("required", []):
            if req_field not in data:
                errors.append(f"{path}: 필수 필드 '{req_field}' 누락")

        # properties 재귀 검증
        props = schema.get("properties", {})
        for field_name, field_schema in props.items():
            if field_name in data:
                sub_errors = validate_schema(data[field_name], field_schema, f"{path}.{field_name}")
                errors.extend(sub_errors)

    # array 검증
    if isinstance(data, list) and "items" in schema:
        for i, item in enumerate(data[:20]):  # 최대 20개만 검증
            sub_errors = validate_schema(item, schema["items"], f"{path}[{i}]")
            errors.extend(sub_errors)

    return errors


# ============================================================
# API 엔드포인트 검증
# ============================================================

async def check_endpoint(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    expected_status: int = 200,
    expected_schema: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """단일 API 엔드포인트 검증

    Args:
        url: 테스트할 엔드포인트 URL
        method: HTTP 메서드 (GET/POST/PUT/DELETE)
        headers: 추가 헤더
        body: POST/PUT 요청 본문
        expected_status: 예상 상태코드
        expected_schema: 예상 응답 JSON Schema (None이면 스키마 검증 스킵)
        timeout: 타임아웃 (초)

    Returns:
        {url, method, passed, status_code, response_time_ms, schema_errors, issues}
    """
    result: dict[str, Any] = {
        "url": url,
        "method": method.upper(),
        "passed": False,
        "status_code": 0,
        "response_time_ms": 0,
        "schema_errors": [],
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    req_headers = {"User-Agent": "ARIA-ContractTest/1.0"}
    if headers:
        req_headers.update(headers)

    try:
        start = time.monotonic()
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            if method.upper() == "GET":
                resp = await client.get(url, headers=req_headers)
            elif method.upper() == "POST":
                resp = await client.post(url, headers=req_headers, json=body)
            elif method.upper() == "PUT":
                resp = await client.put(url, headers=req_headers, json=body)
            elif method.upper() == "DELETE":
                resp = await client.delete(url, headers=req_headers)
            else:
                result["issues"].append({
                    "severity": "medium",
                    "message": f"지원하지 않는 HTTP 메서드: {method}",
                })
                return result

        elapsed_ms = int((time.monotonic() - start) * 1000)
        result["status_code"] = resp.status_code
        result["response_time_ms"] = elapsed_ms

        # 1. 상태코드 검증
        if resp.status_code != expected_status:
            result["issues"].append({
                "severity": "high",
                "message": f"상태코드 불일치 — 예상 {expected_status} / 실제 {resp.status_code}",
            })
            return result

        # 2. 스키마 검증 (JSON 응답일 때만)
        if expected_schema:
            content_type = resp.headers.get("content-type", "")
            if "json" not in content_type.lower():
                result["issues"].append({
                    "severity": "medium",
                    "message": f"Content-Type이 JSON이 아님: {content_type}",
                })
                return result

            try:
                resp_data = resp.json()
            except Exception:
                result["issues"].append({
                    "severity": "high",
                    "message": "응답을 JSON으로 파싱할 수 없음",
                })
                return result

            schema_errors = validate_schema(resp_data, expected_schema)
            result["schema_errors"] = schema_errors

            if schema_errors:
                result["issues"].append({
                    "severity": "high",
                    "message": f"스키마 검증 실패 ({len(schema_errors)}건): {schema_errors[0]}",
                })
                return result

        # 3. 응답 시간 경고
        if elapsed_ms > 5000:
            result["issues"].append({
                "severity": "low",
                "message": f"응답 시간 느림: {elapsed_ms}ms",
            })

        result["passed"] = True

    except httpx.TimeoutException:
        result["issues"].append({
            "severity": "high",
            "message": f"타임아웃 ({timeout}초 초과)",
        })
    except httpx.ConnectError as e:
        result["issues"].append({
            "severity": "high",
            "message": f"연결 실패: {e}",
        })
    except Exception as e:
        result["issues"].append({
            "severity": "medium",
            "message": f"검증 실패: {type(e).__name__}: {e}",
        })

    return result


async def run_contract_tests(
    endpoints: list[dict[str, Any]],
    base_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """제품별 API 엔드포인트 목록 통합 테스트

    Args:
        endpoints: 테스트할 엔드포인트 목록
            각 항목: {url, method?, expected_status?, expected_schema?, headers?, body?}
        base_headers: 모든 요청에 적용할 기본 헤더

    Returns:
        {total, passed, failed, results, severity, issues}
    """
    result: dict[str, Any] = {
        "total": len(endpoints),
        "passed": 0,
        "failed": 0,
        "results": [],
        "severity": "none",
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    for ep_config in endpoints:
        url = ep_config.get("url", "")
        if not url:
            continue

        # 헤더 병합 (base + endpoint-specific)
        merged_headers = dict(base_headers or {})
        ep_headers = ep_config.get("headers")
        if ep_headers:
            merged_headers.update(ep_headers)

        ep_result = await check_endpoint(
            url=url,
            method=ep_config.get("method", "GET"),
            headers=merged_headers or None,
            body=ep_config.get("body"),
            expected_status=ep_config.get("expected_status", 200),
            expected_schema=ep_config.get("expected_schema"),
        )

        result["results"].append(ep_result)

        if ep_result["passed"]:
            result["passed"] += 1
        else:
            result["failed"] += 1

    # 심각도 판단
    if result["failed"] > 0:
        fail_rate = result["failed"] / max(result["total"], 1) * 100
        if fail_rate >= 50:
            result["severity"] = "high"
        elif fail_rate >= 20:
            result["severity"] = "medium"
        else:
            result["severity"] = "low"

        result["issues"].append({
            "severity": result["severity"],
            "message": (
                f"API Contract 테스트 {result['failed']}/{result['total']}건 실패 "
                f"({fail_rate:.0f}%)"
            ),
        })

    return result
