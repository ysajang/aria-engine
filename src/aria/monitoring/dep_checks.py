"""ARIA Engine - Dependency Audit Checks

의존성 취약점 스캔 로직 (ToolExecutor + cron 스크립트 공용)
- check_dependabot_alerts: GitHub Dependabot API로 취약점 목록 조회
- run_dependency_audit: 통합 실행 (여러 레포 지원)

인증: GITHUB_TOKEN (Personal Access Token / security_events 또는 vulnerability_alerts 스코프)

모든 함수는 dict 반환 → ToolResult.output / EventInput.data 양쪽에 사용
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

logger = structlog.get_logger()

GITHUB_API_BASE = "https://api.github.com"


def _parse_repo_url(repo_url: str) -> tuple[str, str] | None:
    """GitHub URL에서 owner/repo 추출

    지원 형식:
    - https://github.com/owner/repo
    - https://github.com/owner/repo.git
    - github.com/owner/repo
    - owner/repo

    Returns:
        (owner, repo) 또는 None
    """
    url = repo_url.strip().rstrip("/")

    # .git 제거
    if url.endswith(".git"):
        url = url[:-4]

    # URL 파싱
    if "github.com" in url:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2:
            return parts[0], parts[1]
    elif "/" in url and "." not in url.split("/")[0]:
        # owner/repo 형식
        parts = url.split("/")
        if len(parts) == 2:
            return parts[0], parts[1]

    return None


# ============================================================
# 1. Dependabot Alerts
# ============================================================


async def check_dependabot_alerts(
    repo_url: str,
    github_token: str,
    state: str = "open",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """GitHub Dependabot 취약점 알림 조회

    Args:
        repo_url: GitHub 레포 URL (예: https://github.com/Testorum/testorum.git)
        github_token: GitHub Personal Access Token
        state: 알림 상태 필터 (open / dismissed / fixed / auto_dismissed)

    Returns:
        {repo, owner, total_alerts, critical, high, medium, low, alerts, issues, checked_at}
    """
    result: dict[str, Any] = {
        "repo_url": repo_url,
        "owner": "",
        "repo": "",
        "total_alerts": 0,
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "alerts": [],
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    parsed = _parse_repo_url(repo_url)
    if parsed is None:
        result["issues"].append({
            "type": "invalid_repo_url",
            "severity": "high",
            "message": f"GitHub URL 파싱 실패: {repo_url}",
        })
        return result

    owner, repo = parsed
    result["owner"] = owner
    result["repo"] = repo

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/dependabot/alerts",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {github_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={
                    "state": state,
                    "per_page": 100,
                    "sort": "created",
                    "direction": "desc",
                },
            )

            if resp.status_code == 200:
                alerts = resp.json()
                if isinstance(alerts, list):
                    result["total_alerts"] = len(alerts)

                    # 심각도별 분류
                    severity_map: dict[str, int] = {
                        "critical": 0,
                        "high": 0,
                        "medium": 0,
                        "low": 0,
                    }

                    processed_alerts: list[dict[str, Any]] = []
                    for alert in alerts:
                        vuln = alert.get("security_vulnerability", {})
                        advisory = alert.get("security_advisory", {})
                        dep = alert.get("dependency", {})
                        pkg = dep.get("package", {})
                        severity = vuln.get("severity", "unknown")

                        if severity in severity_map:
                            severity_map[severity] += 1

                        alert_info = {
                            "number": alert.get("number"),
                            "state": alert.get("state"),
                            "package": pkg.get("name", "unknown"),
                            "ecosystem": pkg.get("ecosystem", "unknown"),
                            "severity": severity,
                            "cve_id": advisory.get("cve_id"),
                            "ghsa_id": advisory.get("ghsa_id"),
                            "summary": advisory.get("summary", "")[:200],
                            "vulnerable_range": vuln.get("vulnerable_version_range", ""),
                            "patched_version": (
                                vuln.get("first_patched_version", {}).get("identifier", "")
                                if vuln.get("first_patched_version")
                                else ""
                            ),
                            "manifest_path": dep.get("manifest_path", ""),
                            "created_at": alert.get("created_at", ""),
                        }
                        processed_alerts.append(alert_info)

                        # high/critical → 이슈
                        if severity in ("critical", "high"):
                            result["issues"].append({
                                "type": f"vuln_{severity}",
                                "severity": "high",
                                "message": (
                                    f"[{severity.upper()}] {pkg.get('name', '?')} "
                                    f"({vuln.get('vulnerable_version_range', '?')}): "
                                    f"{advisory.get('summary', '?')[:100]}"
                                ),
                            })

                    result.update(severity_map)
                    result["alerts"] = processed_alerts[:20]  # 상위 20개

            elif resp.status_code == 403:
                result["issues"].append({
                    "type": "permission_denied",
                    "severity": "medium",
                    "message": (
                        "Dependabot 접근 권한 없음. "
                        "GITHUB_TOKEN에 security_events 스코프 필요 "
                        "또는 레포 설정에서 Dependabot alerts 활성화 필요"
                    ),
                })
            elif resp.status_code == 404:
                result["issues"].append({
                    "type": "repo_not_found",
                    "severity": "medium",
                    "message": f"레포 미발견 또는 Dependabot 미활성화: {owner}/{repo}",
                })
            elif resp.status_code == 401:
                result["issues"].append({
                    "type": "auth_failed",
                    "severity": "high",
                    "message": "GitHub API 인증 실패 (GITHUB_TOKEN 확인)",
                })
            else:
                result["issues"].append({
                    "type": "api_error",
                    "severity": "medium",
                    "message": f"GitHub API HTTP {resp.status_code}",
                })

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "medium",
            "message": f"GitHub API 타임아웃 ({timeout}초)",
        })
    except Exception as e:
        result["issues"].append({
            "type": "error",
            "severity": "high",
            "message": str(e)[:200],
        })

    return result


# ============================================================
# 2. Dependency Audit (통합 / 여러 레포)
# ============================================================


async def run_dependency_audit(
    repo_urls: list[str],
    github_token: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """의존성 종합 감사 — 여러 레포의 Dependabot 알림 통합

    Returns:
        {repos, total_alerts, critical, high, medium, low, total_issues, high_issues, elapsed_ms}
    """
    start = time.monotonic()

    results: list[dict[str, Any]] = []
    total_critical = 0
    total_high = 0
    total_medium = 0
    total_low = 0
    all_issues: list[dict[str, Any]] = []

    for repo_url in repo_urls:
        result = await check_dependabot_alerts(repo_url, github_token, timeout=timeout)
        results.append(result)
        total_critical += result.get("critical", 0)
        total_high += result.get("high", 0)
        total_medium += result.get("medium", 0)
        total_low += result.get("low", 0)
        all_issues.extend(result.get("issues", []))

    high_issues = [i for i in all_issues if i.get("severity") == "high"]
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)

    return {
        "repos": results,
        "repo_count": len(repo_urls),
        "total_alerts": sum(r.get("total_alerts", 0) for r in results),
        "critical": total_critical,
        "high": total_high,
        "medium": total_medium,
        "low": total_low,
        "total_issues": len(all_issues),
        "high_issues": len(high_issues),
        "all_issues": all_issues,
        "elapsed_ms": elapsed_ms,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
