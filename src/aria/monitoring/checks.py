"""ARIA Engine - Monitoring Checks

서버 모니터링 핵심 로직 (ToolExecutor + cron 스크립트 공용)
- run_healthcheck: HTTP 응답시간 + 상태코드 + SSL 만료 체크
- analyze_error_logs: 로그 파일에서 에러 패턴 추출 + 집계
- check_traffic_anomaly: 요청 빈도 이상 감지 (이동평균 대비)
- run_security_scan: 보안 헤더 + 열린 포트 + SSL 설정 체크

모든 함수는 dict 반환 → ToolResult.output / EventInput.data 양쪽에 사용
"""

from __future__ import annotations

import re
import socket
import ssl
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


# ============================================================
# 1. Healthcheck
# ============================================================


async def run_healthcheck(
    url: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """HTTP 헬스체크 — 응답시간 / 상태코드 / SSL 만료일 체크

    Args:
        url: 체크 대상 URL (http:// 또는 https://)
        timeout: 요청 타임아웃 (초)

    Returns:
        {url, status, status_code, response_time_ms, ssl_expiry_days, error}
    """
    result: dict[str, Any] = {
        "url": url,
        "status": "unknown",
        "status_code": 0,
        "response_time_ms": 0.0,
        "ssl_expiry_days": None,
        "error": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        start = time.monotonic()
        async with httpx.AsyncClient(
            verify=True,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
        elapsed_ms = (time.monotonic() - start) * 1000

        result["status_code"] = resp.status_code
        result["response_time_ms"] = round(elapsed_ms, 2)

        if 200 <= resp.status_code < 400:
            result["status"] = "healthy"
        elif 400 <= resp.status_code < 500:
            result["status"] = "client_error"
        else:
            result["status"] = "server_error"

    except httpx.ConnectError:
        result["status"] = "unreachable"
        result["error"] = "연결 실패 — 서버가 응답하지 않습니다"
    except httpx.TimeoutException:
        result["status"] = "timeout"
        result["error"] = f"타임아웃 ({timeout}초 초과)"
    except httpx.ConnectTimeout:
        result["status"] = "timeout"
        result["error"] = f"연결 타임아웃 ({timeout}초 초과)"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:300]

    # SSL 만료일 체크 (https만)
    if url.startswith("https://"):
        try:
            ssl_days = _check_ssl_expiry(url)
            result["ssl_expiry_days"] = ssl_days
        except Exception as e:
            logger.debug("ssl_check_failed", url=url, error=str(e)[:100])

    return result


def _check_ssl_expiry(url: str) -> int | None:
    """SSL 인증서 만료까지 남은 일수 반환

    Args:
        url: https:// URL

    Returns:
        남은 일수 (음수면 이미 만료) 또는 None (확인 불가)
    """
    try:
        # URL에서 hostname 추출
        hostname = url.split("://")[1].split("/")[0].split(":")[0]
        port = 443

        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()

        if cert and "notAfter" in cert:
            # SSL 날짜 형식: 'Sep 15 00:00:00 2025 GMT'
            expiry_str = cert["notAfter"]
            expiry_dt = datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z")
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return (expiry_dt - now).days

    except Exception:
        return None

    return None


async def run_healthcheck_batch(
    urls: list[str],
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """여러 URL 동시 헬스체크

    Args:
        urls: 체크할 URL 목록
        timeout: 개별 요청 타임아웃

    Returns:
        각 URL의 체크 결과 목록
    """
    import asyncio
    tasks = [run_healthcheck(url, timeout) for url in urls]
    return await asyncio.gather(*tasks, return_exceptions=False)


# ============================================================
# 2. Error Log Analysis
# ============================================================


def analyze_error_logs(
    log_path: str,
    minutes: int = 30,
    max_lines: int = 10000,
) -> dict[str, Any]:
    """로그 파일에서 에러 패턴 추출 + 집계

    nginx/uvicorn/structlog JSON 포맷 + 일반 텍스트 로그 지원

    Args:
        log_path: 로그 파일 경로
        minutes: 분석할 최근 N분
        max_lines: 읽을 최대 라인 수 (성능 보호)

    Returns:
        {total_lines, error_count, status_codes, top_error_paths, severity, ...}
    """
    result: dict[str, Any] = {
        "log_path": log_path,
        "analysis_window_minutes": minutes,
        "total_lines_scanned": 0,
        "error_count": 0,
        "warning_count": 0,
        "status_code_distribution": {},
        "top_error_paths": [],
        "top_error_messages": [],
        "severity": "info",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    path = Path(log_path)
    if not path.exists():
        result["error"] = f"로그 파일 없음: {log_path}"
        return result

    if not path.is_file():
        result["error"] = f"파일이 아님: {log_path}"
        return result

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    status_codes: Counter[int] = Counter()
    error_paths: Counter[str] = Counter()
    error_messages: Counter[str] = Counter()
    error_count = 0
    warning_count = 0
    lines_scanned = 0

    try:
        # 파일 끝부터 읽기 (최근 로그 우선)
        lines = _tail_file(path, max_lines)

        for line in lines:
            lines_scanned += 1
            parsed = _parse_log_line(line)
            if parsed is None:
                continue

            # 시간 필터 (파싱 가능한 경우만)
            if parsed.get("timestamp"):
                try:
                    log_time = datetime.fromisoformat(
                        parsed["timestamp"].replace("Z", "+00:00")
                    )
                    if log_time < cutoff:
                        continue
                except (ValueError, AttributeError):
                    pass  # 타임스탬프 파싱 실패 시 포함

            status = parsed.get("status_code", 0)
            if status:
                status_codes[status] += 1

            level = parsed.get("level", "").lower()
            if level in ("error", "critical", "fatal"):
                error_count += 1
                if parsed.get("path"):
                    error_paths[parsed["path"]] += 1
                if parsed.get("message"):
                    # 메시지 정규화 (숫자/ID 제거)
                    normalized = re.sub(r"\b[0-9a-f-]{8,}\b", "<ID>", parsed["message"])
                    normalized = re.sub(r"\b\d+\b", "<N>", normalized)
                    error_messages[normalized[:100]] += 1
            elif level == "warning":
                warning_count += 1

    except PermissionError:
        result["error"] = f"읽기 권한 없음: {log_path}"
        return result
    except Exception as e:
        result["error"] = f"로그 분석 실패: {str(e)[:200]}"
        return result

    result["total_lines_scanned"] = lines_scanned
    result["error_count"] = error_count
    result["warning_count"] = warning_count
    result["status_code_distribution"] = dict(status_codes.most_common(10))
    result["top_error_paths"] = [
        {"path": p, "count": c} for p, c in error_paths.most_common(5)
    ]
    result["top_error_messages"] = [
        {"message": m, "count": c} for m, c in error_messages.most_common(5)
    ]

    # 심각도 판단
    if error_count >= 50:
        result["severity"] = "critical"
    elif error_count >= 10:
        result["severity"] = "warning"
    elif error_count > 0:
        result["severity"] = "info"

    return result


def _tail_file(path: Path, max_lines: int) -> list[str]:
    """파일 끝에서 max_lines만큼 읽기"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-max_lines:]
    except Exception:
        return []


def _parse_log_line(line: str) -> dict[str, Any] | None:
    """로그 라인 파싱 (structlog JSON / nginx / 일반 텍스트)

    Returns:
        {timestamp, level, status_code, path, message} 또는 None
    """
    line = line.strip()
    if not line:
        return None

    # 1. structlog JSON 형식
    if line.startswith("{"):
        try:
            import json
            data = json.loads(line)
            return {
                "timestamp": data.get("timestamp", data.get("time", "")),
                "level": data.get("level", data.get("log_level", "")),
                "status_code": data.get("status_code", data.get("status", 0)),
                "path": data.get("path", data.get("url", "")),
                "message": data.get("event", data.get("message", data.get("msg", ""))),
            }
        except (ValueError, KeyError):
            pass

    # 2. nginx/apache access log 형식
    # 패턴: IP - - [timestamp] "METHOD /path HTTP/x.x" STATUS SIZE
    nginx_pattern = re.compile(
        r'(\S+)\s+\S+\s+\S+\s+\[([^\]]+)\]\s+"(\S+)\s+(\S+)\s+\S+"\s+(\d{3})\s+(\d+)'
    )
    m = nginx_pattern.match(line)
    if m:
        status_code = int(m.group(5))
        return {
            "timestamp": "",
            "level": "error" if status_code >= 500 else ("warning" if status_code >= 400 else "info"),
            "status_code": status_code,
            "path": m.group(4),
            "message": f"{m.group(3)} {m.group(4)} → {status_code}",
        }

    # 3. 일반 텍스트 (level 키워드 감지)
    level = "info"
    for lvl in ("CRITICAL", "FATAL", "ERROR", "WARNING", "WARN"):
        if lvl in line.upper():
            level = "error" if lvl in ("CRITICAL", "FATAL", "ERROR") else "warning"
            break

    if level != "info":
        return {
            "timestamp": "",
            "level": level,
            "status_code": 0,
            "path": "",
            "message": line[:200],
        }

    return None


# ============================================================
# 3. Traffic Anomaly Detection
# ============================================================


def check_traffic_anomaly(
    log_path: str,
    window_minutes: int = 15,
    baseline_hours: int = 24,
    anomaly_threshold: float = 3.0,
    max_lines: int = 50000,
) -> dict[str, Any]:
    """트래픽 이상 감지 — 최근 윈도우 vs 과거 평균 비교

    Args:
        log_path: 로그 파일 경로 (nginx access log 또는 ARIA request log)
        window_minutes: 현재 윈도우 크기 (분)
        baseline_hours: 베이스라인 산출 기간 (시간)
        anomaly_threshold: 이상 판정 배수 (기본 3x)
        max_lines: 최대 분석 라인 수

    Returns:
        {current_rpm, baseline_rpm, anomaly_detected, ratio, top_ips, top_paths, ...}
    """
    result: dict[str, Any] = {
        "log_path": log_path,
        "window_minutes": window_minutes,
        "current_rpm": 0.0,
        "baseline_rpm": 0.0,
        "ratio": 0.0,
        "anomaly_detected": False,
        "anomaly_threshold": anomaly_threshold,
        "total_requests_window": 0,
        "top_ips": [],
        "top_paths": [],
        "severity": "info",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    path = Path(log_path)
    if not path.exists():
        result["error"] = f"로그 파일 없음: {log_path}"
        return result

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=window_minutes)
    baseline_start = now - timedelta(hours=baseline_hours)

    window_count = 0
    baseline_count = 0
    window_ips: Counter[str] = Counter()
    window_paths: Counter[str] = Counter()

    try:
        lines = _tail_file(path, max_lines)

        for line in lines:
            parsed = _parse_log_line(line)
            if parsed is None:
                continue

            # 시간 추출 시도
            ts = parsed.get("timestamp", "")
            if ts:
                try:
                    log_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    baseline_count += 1
                    continue
            else:
                # 타임스탬프 없으면 baseline에 포함
                baseline_count += 1
                continue

            if log_time >= window_start:
                window_count += 1
                # IP/경로 추출 (nginx 로그에서)
                ip_match = re.match(r"(\S+)", line)
                if ip_match:
                    ip = ip_match.group(1)
                    if re.match(r"\d+\.\d+\.\d+\.\d+", ip):
                        window_ips[ip] += 1
                if parsed.get("path"):
                    window_paths[parsed["path"]] += 1

            if log_time >= baseline_start:
                baseline_count += 1

    except Exception as e:
        result["error"] = f"트래픽 분석 실패: {str(e)[:200]}"
        return result

    # RPM 계산
    result["total_requests_window"] = window_count
    result["current_rpm"] = round(window_count / max(window_minutes, 1), 2)

    baseline_minutes = baseline_hours * 60
    result["baseline_rpm"] = round(baseline_count / max(baseline_minutes, 1), 2)

    # 이상 감지
    if result["baseline_rpm"] > 0:
        result["ratio"] = round(result["current_rpm"] / result["baseline_rpm"], 2)
        result["anomaly_detected"] = result["ratio"] >= anomaly_threshold
    elif result["current_rpm"] > 10:
        # 베이스라인 없는데 요청 많으면 이상
        result["anomaly_detected"] = True
        result["ratio"] = float("inf")

    result["top_ips"] = [
        {"ip": ip, "count": c} for ip, c in window_ips.most_common(5)
    ]
    result["top_paths"] = [
        {"path": p, "count": c} for p, c in window_paths.most_common(5)
    ]

    # 심각도
    if result["anomaly_detected"]:
        result["severity"] = "warning" if result["ratio"] < 5.0 else "critical"

    return result


# ============================================================
# 4. Security Scan
# ============================================================


async def run_security_scan(
    url: str,
    ports: list[int] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """기본 보안 취약점 스캔

    체크 항목:
    - HTTP 보안 헤더 (HSTS / CSP / X-Frame-Options / X-Content-Type-Options 등)
    - SSL/TLS 설정 (프로토콜 버전)
    - 열린 포트 확인
    - 서버 정보 노출 (Server / X-Powered-By 헤더)

    Args:
        url: 스캔 대상 URL
        ports: 체크할 포트 목록 (기본: 주요 포트)
        timeout: 요청 타임아웃

    Returns:
        {url, headers_score, issues, open_ports, ssl_info, ...}
    """
    if ports is None:
        ports = [21, 22, 80, 443, 3306, 5432, 6379, 8080, 8100, 8443, 9200, 27017]

    result: dict[str, Any] = {
        "url": url,
        "headers_score": 0,
        "headers_max_score": 0,
        "issues": [],
        "open_ports": [],
        "ssl_info": {},
        "server_info_exposed": False,
        "severity": "info",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    # 1. 보안 헤더 체크
    try:
        async with httpx.AsyncClient(
            verify=True,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)

        headers_result = _check_security_headers(dict(resp.headers))
        result["headers_score"] = headers_result["score"]
        result["headers_max_score"] = headers_result["max_score"]
        result["issues"].extend(headers_result["issues"])

        # 서버 정보 노출 체크
        server_header = resp.headers.get("server", "")
        powered_by = resp.headers.get("x-powered-by", "")
        if server_header and any(
            kw in server_header.lower()
            for kw in ("apache", "nginx", "uvicorn", "gunicorn", "express")
        ):
            result["server_info_exposed"] = True
            result["issues"].append({
                "type": "info_exposure",
                "severity": "low",
                "detail": f"Server 헤더에서 서버 정보 노출: {server_header}",
            })
        if powered_by:
            result["server_info_exposed"] = True
            result["issues"].append({
                "type": "info_exposure",
                "severity": "low",
                "detail": f"X-Powered-By 헤더 노출: {powered_by}",
            })

    except Exception as e:
        result["issues"].append({
            "type": "connection_error",
            "severity": "high",
            "detail": f"보안 헤더 체크 실패: {str(e)[:200]}",
        })

    # 2. 열린 포트 스캔
    hostname = _extract_hostname(url)
    if hostname:
        open_ports = await _scan_ports(hostname, ports, timeout=3.0)
        result["open_ports"] = open_ports

        # 위험 포트 체크
        dangerous_ports = {
            3306: "MySQL",
            5432: "PostgreSQL",
            6379: "Redis",
            9200: "Elasticsearch",
            27017: "MongoDB",
        }
        for port_info in open_ports:
            port_num = port_info["port"]
            if port_num in dangerous_ports:
                result["issues"].append({
                    "type": "open_dangerous_port",
                    "severity": "high",
                    "detail": f"위험 포트 외부 노출: {port_num} ({dangerous_ports[port_num]})",
                })

    # 3. SSL 정보 (https만)
    if url.startswith("https://"):
        ssl_info = _get_ssl_info(url)
        result["ssl_info"] = ssl_info
        if ssl_info.get("issues"):
            result["issues"].extend(ssl_info["issues"])

    # 심각도 결정
    high_issues = sum(1 for i in result["issues"] if i.get("severity") == "high")
    medium_issues = sum(1 for i in result["issues"] if i.get("severity") == "medium")

    if high_issues > 0:
        result["severity"] = "critical"
    elif medium_issues > 0:
        result["severity"] = "warning"

    return result


def _check_security_headers(headers: dict[str, str]) -> dict[str, Any]:
    """HTTP 보안 헤더 점수 매기기

    Returns:
        {score, max_score, issues: [{type, severity, detail}]}
    """
    checks: list[tuple[str, str, int, str]] = [
        # (header_name, expected_pattern, points, description)
        ("strict-transport-security", "max-age=", 2, "HSTS 미설정 — HTTPS 강제 불가"),
        ("content-security-policy", "", 2, "CSP 미설정 — XSS 공격에 취약"),
        ("x-frame-options", "", 1, "X-Frame-Options 미설정 — 클릭재킹 취약"),
        ("x-content-type-options", "nosniff", 1, "X-Content-Type-Options 미설정 — MIME 스니핑 취약"),
        ("referrer-policy", "", 1, "Referrer-Policy 미설정"),
        ("permissions-policy", "", 1, "Permissions-Policy 미설정"),
        ("x-xss-protection", "", 1, "X-XSS-Protection 미설정"),
    ]

    score = 0
    max_score = sum(c[2] for c in checks)
    issues: list[dict[str, Any]] = []

    # 헤더명 소문자 정규화
    lower_headers = {k.lower(): v for k, v in headers.items()}

    for header_name, expected_pattern, points, desc in checks:
        value = lower_headers.get(header_name, "")
        if value:
            if expected_pattern and expected_pattern not in value.lower():
                issues.append({
                    "type": "weak_header",
                    "severity": "medium",
                    "detail": f"{header_name}: 값이 부적절 — {value}",
                })
            else:
                score += points
        else:
            severity = "medium" if points >= 2 else "low"
            issues.append({
                "type": "missing_header",
                "severity": severity,
                "detail": desc,
            })

    return {"score": score, "max_score": max_score, "issues": issues}


def _extract_hostname(url: str) -> str:
    """URL에서 hostname 추출"""
    try:
        after_protocol = url.split("://")[1] if "://" in url else url
        hostname = after_protocol.split("/")[0].split(":")[0]
        return hostname
    except (IndexError, AttributeError):
        return ""


async def _scan_ports(
    hostname: str,
    ports: list[int],
    timeout: float = 3.0,
) -> list[dict[str, Any]]:
    """TCP 포트 스캔 (비동기)

    Returns:
        열린 포트 목록: [{port, service}]
    """
    import asyncio

    well_known: dict[int, str] = {
        21: "FTP", 22: "SSH", 25: "SMTP", 53: "DNS",
        80: "HTTP", 443: "HTTPS", 3306: "MySQL",
        5432: "PostgreSQL", 6379: "Redis", 8080: "HTTP-Alt",
        8100: "ARIA", 8443: "HTTPS-Alt", 9200: "Elasticsearch",
        27017: "MongoDB",
    }

    async def check_port(port: int) -> dict[str, Any] | None:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(hostname, port),
                timeout=timeout,
            )
            writer.close()
            await writer.wait_closed()
            return {
                "port": port,
                "service": well_known.get(port, "unknown"),
            }
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
            return None

    tasks = [check_port(p) for p in ports]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    return [r for r in results if isinstance(r, dict)]


def _get_ssl_info(url: str) -> dict[str, Any]:
    """SSL/TLS 인증서 정보 수집

    Returns:
        {protocol, cipher, expiry_days, issuer, issues: [...]}
    """
    info: dict[str, Any] = {
        "protocol": "",
        "cipher": "",
        "expiry_days": None,
        "issuer": "",
        "issues": [],
    }

    hostname = _extract_hostname(url)
    if not hostname:
        return info

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                info["protocol"] = ssock.version() or ""
                cipher_info = ssock.cipher()
                if cipher_info:
                    info["cipher"] = cipher_info[0]

                cert = ssock.getpeercert()
                if cert:
                    # 만료일
                    if "notAfter" in cert:
                        expiry_dt = datetime.strptime(
                            cert["notAfter"], "%b %d %H:%M:%S %Y %Z"
                        ).replace(tzinfo=timezone.utc)
                        days_left = (expiry_dt - datetime.now(timezone.utc)).days
                        info["expiry_days"] = days_left

                        if days_left < 0:
                            info["issues"].append({
                                "type": "ssl_expired",
                                "severity": "high",
                                "detail": f"SSL 인증서 만료됨 ({abs(days_left)}일 전)",
                            })
                        elif days_left < 14:
                            info["issues"].append({
                                "type": "ssl_expiring_soon",
                                "severity": "high",
                                "detail": f"SSL 인증서 {days_left}일 후 만료",
                            })
                        elif days_left < 30:
                            info["issues"].append({
                                "type": "ssl_expiring_soon",
                                "severity": "medium",
                                "detail": f"SSL 인증서 {days_left}일 후 만료",
                            })

                    # 발급자
                    issuer = cert.get("issuer", ())
                    for field in issuer:
                        for key, value in field:
                            if key == "organizationName":
                                info["issuer"] = value

        # TLS 버전 체크
        if info["protocol"] and info["protocol"] < "TLSv1.2":
            info["issues"].append({
                "type": "weak_tls",
                "severity": "high",
                "detail": f"취약한 TLS 버전: {info['protocol']} (최소 TLSv1.2 권장)",
            })

    except Exception as e:
        info["issues"].append({
            "type": "ssl_check_error",
            "severity": "medium",
            "detail": f"SSL 정보 수집 실패: {str(e)[:200]}",
        })

    return info


# ============================================================
# 5. 종합 결과 심각도 판정
# ============================================================


def determine_overall_severity(
    results: list[dict[str, Any]],
) -> str:
    """여러 체크 결과에서 전체 심각도 판정

    Returns:
        "info" / "warning" / "critical"
    """
    severities = [r.get("severity", "info") for r in results]

    if "critical" in severities:
        return "critical"
    if "warning" in severities:
        return "warning"
    return "info"
