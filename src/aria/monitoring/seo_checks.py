"""ARIA Engine - SEO Monitoring Checks

SEO 품질 점검 로직 (ToolExecutor + cron 스크립트 공용)
- check_meta_tags: title/description/OG 태그/canonical 검증
- check_broken_links: 내부 링크 크롤링 + HTTP 상태 확인
- check_robots_sitemap: robots.txt 존재 + sitemap.xml 파싱
- run_seo_audit: 위 3가지 통합 실행

모든 함수는 dict 반환 → ToolResult.output / EventInput.data 양쪽에 사용
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import structlog

logger = structlog.get_logger()

# 내부 링크 추출용 정규식
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_META_RE = re.compile(
    r'<meta\s+(?:[^>]*?\s)?(?:name|property)=["\']([^"\']+)["\']'
    r'\s+content=["\']([^"\']*)["\']',
    re.IGNORECASE | re.DOTALL,
)
_META_REV_RE = re.compile(
    r'<meta\s+content=["\']([^"\']*)["\']'
    r'\s+(?:name|property)=["\']([^"\']+)["\']',
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.IGNORECASE)
_CANONICAL_RE = re.compile(
    r'<link\s+[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


# ============================================================
# 1. Meta Tags Check
# ============================================================


async def check_meta_tags(
    url: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """페이지 메타태그 검증

    검사 항목:
    - <title> 존재 + 길이 (30-60자 권장)
    - meta description 존재 + 길이 (120-160자 권장)
    - og:title / og:description / og:image
    - canonical URL
    - viewport 메타태그

    Returns:
        {url, score, max_score, issues, meta}
    """
    result: dict[str, Any] = {
        "url": url,
        "score": 0,
        "max_score": 7,
        "issues": [],
        "meta": {},
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "ARIA-SEO-Monitor/1.0"},
        ) as client:
            resp = await client.get(url)
            html = resp.text
            result["status_code"] = resp.status_code

            if resp.status_code != 200:
                result["issues"].append({
                    "type": "http_error",
                    "severity": "high",
                    "message": f"HTTP {resp.status_code} 반환",
                })
                return result

    except httpx.TimeoutException:
        result["issues"].append({
            "type": "timeout",
            "severity": "high",
            "message": f"요청 타임아웃 ({timeout}초)",
        })
        return result
    except Exception as e:
        result["issues"].append({
            "type": "connection_error",
            "severity": "high",
            "message": str(e)[:200],
        })
        return result

    # 메타 데이터 추출
    meta_dict: dict[str, str] = {}

    # name/property → content
    for match in _META_RE.finditer(html):
        meta_dict[match.group(1).lower()] = match.group(2)
    # content → name/property (역순 태그)
    for match in _META_REV_RE.finditer(html):
        meta_dict[match.group(2).lower()] = match.group(1)

    result["meta"] = meta_dict

    # --- <title> ---
    title_match = _TITLE_RE.search(html)
    if title_match:
        title = title_match.group(1).strip()
        result["meta"]["title"] = title
        if len(title) < 10:
            result["issues"].append({
                "type": "title_too_short",
                "severity": "medium",
                "message": f"title 너무 짧음 ({len(title)}자 / 권장 30-60자)",
            })
        elif len(title) > 70:
            result["issues"].append({
                "type": "title_too_long",
                "severity": "low",
                "message": f"title 너무 김 ({len(title)}자 / 권장 30-60자)",
            })
        else:
            result["score"] += 1
    else:
        result["issues"].append({
            "type": "title_missing",
            "severity": "high",
            "message": "<title> 태그 없음",
        })

    # --- meta description ---
    description = meta_dict.get("description", "")
    if description:
        if len(description) < 50:
            result["issues"].append({
                "type": "description_too_short",
                "severity": "medium",
                "message": f"description 너무 짧음 ({len(description)}자 / 권장 120-160자)",
            })
        elif len(description) > 170:
            result["issues"].append({
                "type": "description_too_long",
                "severity": "low",
                "message": f"description 너무 김 ({len(description)}자 / 권장 120-160자)",
            })
        else:
            result["score"] += 1
    else:
        result["issues"].append({
            "type": "description_missing",
            "severity": "high",
            "message": "meta description 없음",
        })

    # --- OG tags ---
    og_title = meta_dict.get("og:title", "")
    og_desc = meta_dict.get("og:description", "")
    og_image = meta_dict.get("og:image", "")

    if og_title:
        result["score"] += 1
    else:
        result["issues"].append({
            "type": "og_title_missing",
            "severity": "medium",
            "message": "og:title 없음 (SNS 공유 시 제목 미표시)",
        })

    if og_desc:
        result["score"] += 1
    else:
        result["issues"].append({
            "type": "og_description_missing",
            "severity": "low",
            "message": "og:description 없음",
        })

    if og_image:
        result["score"] += 1
    else:
        result["issues"].append({
            "type": "og_image_missing",
            "severity": "medium",
            "message": "og:image 없음 (SNS 공유 시 이미지 미표시)",
        })

    # --- canonical ---
    canonical_match = _CANONICAL_RE.search(html)
    if canonical_match:
        result["meta"]["canonical"] = canonical_match.group(1)
        result["score"] += 1
    else:
        result["issues"].append({
            "type": "canonical_missing",
            "severity": "low",
            "message": "canonical URL 없음 (중복 콘텐츠 리스크)",
        })

    # --- viewport ---
    viewport = meta_dict.get("viewport", "")
    if viewport:
        result["score"] += 1
    else:
        result["issues"].append({
            "type": "viewport_missing",
            "severity": "medium",
            "message": "viewport 메타태그 없음 (모바일 최적화 미흡)",
        })

    return result


# ============================================================
# 2. Broken Links Check
# ============================================================


async def check_broken_links(
    url: str,
    max_links: int = 50,
    timeout: float = 10.0,
    check_external: bool = False,
) -> dict[str, Any]:
    """페이지 내 깨진 링크 검사

    1. 페이지 HTML에서 href 추출
    2. 내부 링크 HEAD 요청으로 상태 확인
    3. 외부 링크는 옵션 (check_external=True)

    Args:
        url: 검사 대상 페이지 URL
        max_links: 최대 검사 링크 수 (비용 제어)
        timeout: 개별 링크 타임아웃
        check_external: 외부 링크도 검사할지 여부

    Returns:
        {url, total_links, checked, broken, broken_links, checked_at}
    """
    result: dict[str, Any] = {
        "url": url,
        "total_links": 0,
        "checked": 0,
        "broken": 0,
        "broken_links": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    parsed_base = urlparse(url)
    base_domain = parsed_base.netloc

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "ARIA-SEO-Monitor/1.0"},
        ) as client:
            # 1. 페이지 HTML 가져오기
            resp = await client.get(url)
            if resp.status_code != 200:
                result["error"] = f"페이지 접근 불가: HTTP {resp.status_code}"
                return result

            html = resp.text

            # 2. href 추출
            hrefs = _HREF_RE.findall(html)
            # 중복 제거 + 정규화
            unique_links: list[str] = []
            seen: set[str] = set()
            for href in hrefs:
                href = href.strip()
                # 앵커/자바스크립트/메일 제외
                if href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                    continue
                # 상대 URL → 절대 URL
                full_url = urljoin(url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    unique_links.append(full_url)

            result["total_links"] = len(unique_links)

            # 3. 링크 체크 (내부만 or 전체)
            links_to_check: list[str] = []
            for link in unique_links:
                parsed = urlparse(link)
                is_internal = parsed.netloc == base_domain or not parsed.netloc
                if is_internal or check_external:
                    links_to_check.append(link)

            # max_links 제한
            links_to_check = links_to_check[:max_links]

            # 4. HEAD 요청으로 상태 확인
            for link in links_to_check:
                try:
                    link_resp = await client.head(link, timeout=timeout)
                    result["checked"] += 1
                    if link_resp.status_code >= 400:
                        result["broken"] += 1
                        result["broken_links"].append({
                            "url": link,
                            "status_code": link_resp.status_code,
                            "type": "internal" if urlparse(link).netloc == base_domain else "external",
                        })
                except (httpx.TimeoutException, httpx.ConnectError):
                    result["checked"] += 1
                    result["broken"] += 1
                    result["broken_links"].append({
                        "url": link,
                        "status_code": 0,
                        "type": "timeout",
                    })
                except Exception:
                    # 기타 에러 무시 (broken 카운트 안 함)
                    result["checked"] += 1

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


# ============================================================
# 3. Robots.txt + Sitemap.xml Check
# ============================================================


async def check_robots_sitemap(
    url: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """robots.txt + sitemap.xml 존재 및 기본 검증

    검사 항목:
    - robots.txt 존재 + Sitemap 필드 포함 여부
    - sitemap.xml 존재 + URL 개수 + 마지막 수정일

    Returns:
        {url, robots_exists, robots_has_sitemap, sitemap_exists, sitemap_url_count, issues}
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    result: dict[str, Any] = {
        "url": url,
        "robots_exists": False,
        "robots_has_sitemap": False,
        "sitemap_exists": False,
        "sitemap_url_count": 0,
        "issues": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "ARIA-SEO-Monitor/1.0"},
        ) as client:
            # --- robots.txt ---
            robots_url = f"{base}/robots.txt"
            try:
                robots_resp = await client.get(robots_url)
                if robots_resp.status_code == 200:
                    result["robots_exists"] = True
                    robots_text = robots_resp.text.lower()
                    if "sitemap:" in robots_text:
                        result["robots_has_sitemap"] = True
                    else:
                        result["issues"].append({
                            "type": "robots_no_sitemap",
                            "severity": "low",
                            "message": "robots.txt에 Sitemap 필드 없음",
                        })
                else:
                    result["issues"].append({
                        "type": "robots_missing",
                        "severity": "medium",
                        "message": f"robots.txt 없음 (HTTP {robots_resp.status_code})",
                    })
            except Exception:
                result["issues"].append({
                    "type": "robots_error",
                    "severity": "medium",
                    "message": "robots.txt 접근 실패",
                })

            # --- sitemap.xml ---
            sitemap_url = f"{base}/sitemap.xml"
            try:
                sitemap_resp = await client.get(sitemap_url)
                if sitemap_resp.status_code == 200:
                    result["sitemap_exists"] = True
                    sitemap_text = sitemap_resp.text
                    # <loc> 태그 개수 = URL 수
                    loc_count = sitemap_text.lower().count("<loc>")
                    result["sitemap_url_count"] = loc_count
                    if loc_count == 0:
                        result["issues"].append({
                            "type": "sitemap_empty",
                            "severity": "medium",
                            "message": "sitemap.xml에 URL 없음",
                        })
                else:
                    result["issues"].append({
                        "type": "sitemap_missing",
                        "severity": "medium",
                        "message": f"sitemap.xml 없음 (HTTP {sitemap_resp.status_code})",
                    })
            except Exception:
                result["issues"].append({
                    "type": "sitemap_error",
                    "severity": "medium",
                    "message": "sitemap.xml 접근 실패",
                })

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


# ============================================================
# 4. SEO Audit (통합)
# ============================================================


async def run_seo_audit(
    url: str,
    check_links: bool = True,
    max_links: int = 30,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """SEO 종합 감사 — meta + broken links + robots/sitemap

    Returns:
        {url, meta, links, robots_sitemap, total_issues, high_issues, checked_at}
    """
    start = time.monotonic()

    meta_result = await check_meta_tags(url, timeout=timeout)
    links_result = (
        await check_broken_links(url, max_links=max_links, timeout=timeout)
        if check_links
        else {"checked": 0, "broken": 0, "broken_links": []}
    )
    robots_result = await check_robots_sitemap(url, timeout=timeout)

    # 이슈 통합
    all_issues = (
        meta_result.get("issues", [])
        + robots_result.get("issues", [])
    )

    # 깨진 링크도 이슈로 추가
    for bl in links_result.get("broken_links", []):
        all_issues.append({
            "type": "broken_link",
            "severity": "medium",
            "message": f"깨진 링크: {bl['url']} (HTTP {bl['status_code']})",
        })

    high_issues = [i for i in all_issues if i.get("severity") == "high"]

    elapsed_ms = round((time.monotonic() - start) * 1000, 1)

    return {
        "url": url,
        "meta": meta_result,
        "links": links_result,
        "robots_sitemap": robots_result,
        "total_issues": len(all_issues),
        "high_issues": len(high_issues),
        "all_issues": all_issues,
        "elapsed_ms": elapsed_ms,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
