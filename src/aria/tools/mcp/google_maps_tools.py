"""ARIA Engine - MCP Tool: Google Maps Platform

글로벌 장소 검색 / 지오코딩 / 경로 탐색 도구 3종
- GooglePlacesSearchTool: Places API (New) Text Search — 자연어 장소 검색
- GoogleGeocodeTool: Geocoding API — 주소↔좌표 변환
- GoogleDirectionsTool: Directions API — 경로 탐색 (driving/walking/transit/bicycling)

인증: API Key (X-Goog-Api-Key 헤더 또는 key 파라미터)
발급: https://console.cloud.google.com/apis/credentials
활성화 필요 API: Places API (New) / Geocoding API / Directions API

카카오맵(한국 전용)의 글로벌 대안 — 해외 장소 검색/경로에 사용
한국 내 장소는 카카오맵이 더 정확 (네이버 지역 검색과 병행)
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from aria.core.config import GoogleMapsConfig
from aria.tools.tool_types import (
    SafetyLevelHint,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolResult,
)

logger = structlog.get_logger()


# ============================================================
# Google Maps Client
# ============================================================


class GoogleMapsClient:
    """Google Maps Platform API 클라이언트

    httpx.AsyncClient 커넥션 풀링 + lazy init
    Places API (New) / Geocoding / Directions 공용

    Args:
        config: GoogleMapsConfig
    """

    PLACES_BASE = "https://places.googleapis.com/v1"
    GEOCODING_BASE = "https://maps.googleapis.com/maps/api/geocode/json"
    DIRECTIONS_BASE = "https://maps.googleapis.com/maps/api/directions/json"

    def __init__(self, config: GoogleMapsConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.request_timeout),
                follow_redirects=True,
            )
        return self._client

    async def places_text_search(
        self,
        query: str,
        language: str = "ko",
        max_results: int = 10,
        location_bias_lat: float | None = None,
        location_bias_lng: float | None = None,
        radius: float = 5000.0,
    ) -> dict[str, Any]:
        """Places API (New) — Text Search

        POST https://places.googleapis.com/v1/places:searchText

        Args:
            query: 검색 쿼리 (예: "pizza in New York" / "서울 맛집")
            language: 결과 언어 코드 (기본: ko)
            max_results: 최대 결과 수 (1~20)
            location_bias_lat: 위치 편향 위도
            location_bias_lng: 위치 편향 경도
            radius: 위치 편향 반경 (미터)

        Returns:
            {places: [...], result_count: N}
        """
        client = await self._get_client()

        body: dict[str, Any] = {
            "textQuery": query,
            "languageCode": language,
            "maxResultCount": min(max(max_results, 1), 20),
        }

        if location_bias_lat is not None and location_bias_lng is not None:
            body["locationBias"] = {
                "circle": {
                    "center": {
                        "latitude": location_bias_lat,
                        "longitude": location_bias_lng,
                    },
                    "radius": radius,
                },
            }

        field_mask = ",".join([
            "places.id",
            "places.displayName",
            "places.formattedAddress",
            "places.location",
            "places.rating",
            "places.userRatingCount",
            "places.types",
            "places.nationalPhoneNumber",
            "places.websiteUri",
            "places.currentOpeningHours",
            "places.businessStatus",
            "places.googleMapsUri",
        ])

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._config.api_key,
            "X-Goog-FieldMask": field_mask,
        }

        resp = await client.post(
            f"{self.PLACES_BASE}/places:searchText",
            json=body,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def geocode(
        self,
        address: str | None = None,
        latlng: str | None = None,
        language: str = "ko",
    ) -> dict[str, Any]:
        """Geocoding API — 주소↔좌표 변환

        GET https://maps.googleapis.com/maps/api/geocode/json

        Args:
            address: 주소 (정방향 지오코딩)
            latlng: "위도,경도" (역방향 지오코딩)
            language: 결과 언어

        Returns:
            Geocoding API 원본 응답
        """
        client = await self._get_client()

        params: dict[str, str] = {
            "key": self._config.api_key,
            "language": language,
        }

        if address:
            params["address"] = address
        elif latlng:
            params["latlng"] = latlng
        else:
            raise ValueError("address 또는 latlng 중 하나는 필수입니다")

        resp = await client.get(self.GEOCODING_BASE, params=params)
        resp.raise_for_status()
        return resp.json()

    async def directions(
        self,
        origin: str,
        destination: str,
        mode: str = "transit",
        language: str = "ko",
        alternatives: bool = False,
        departure_time: str | None = None,
    ) -> dict[str, Any]:
        """Directions API — 경로 탐색

        GET https://maps.googleapis.com/maps/api/directions/json

        Args:
            origin: 출발지 (주소 / "위도,경도" / place_id)
            destination: 도착지
            mode: 이동 수단 (driving / walking / bicycling / transit)
            language: 결과 언어
            alternatives: 대안 경로 포함 여부
            departure_time: 출발 시간 (Unix timestamp 또는 "now")

        Returns:
            Directions API 원본 응답
        """
        client = await self._get_client()

        params: dict[str, str] = {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "language": language,
            "key": self._config.api_key,
        }

        if alternatives:
            params["alternatives"] = "true"
        if departure_time:
            params["departure_time"] = departure_time

        resp = await client.get(self.DIRECTIONS_BASE, params=params)
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ============================================================
# 응답 정제 유틸리티
# ============================================================


def _simplify_place(place: dict[str, Any]) -> dict[str, Any]:
    """Places API (New) 응답을 LLM 친화 형태로 정제"""
    display_name = place.get("displayName", {})
    location = place.get("location", {})

    opening_hours = place.get("currentOpeningHours", {})
    is_open = None
    if opening_hours:
        is_open = opening_hours.get("openNow")

    return {
        "name": display_name.get("text", ""),
        "address": place.get("formattedAddress", ""),
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "rating": place.get("rating"),
        "rating_count": place.get("userRatingCount"),
        "phone": place.get("nationalPhoneNumber", ""),
        "website": place.get("websiteUri", ""),
        "google_maps_url": place.get("googleMapsUri", ""),
        "is_open": is_open,
        "business_status": place.get("businessStatus", ""),
        "types": place.get("types", [])[:5],
        "place_id": place.get("id", ""),
    }


def _simplify_geocode_result(result: dict[str, Any]) -> dict[str, Any]:
    """Geocoding 결과 정제"""
    geometry = result.get("geometry", {})
    location = geometry.get("location", {})
    return {
        "formatted_address": result.get("formatted_address", ""),
        "latitude": location.get("lat"),
        "longitude": location.get("lng"),
        "place_id": result.get("place_id", ""),
        "types": result.get("types", []),
    }


def _simplify_route(route: dict[str, Any]) -> dict[str, Any]:
    """Directions 경로 정제"""
    legs = route.get("legs", [])
    if not legs:
        return {"error": "경로 없음"}

    leg = legs[0]
    steps_simplified = []
    for step in leg.get("steps", []):
        step_info: dict[str, Any] = {
            "instruction": step.get("html_instructions", ""),
            "distance": step.get("distance", {}).get("text", ""),
            "duration": step.get("duration", {}).get("text", ""),
            "travel_mode": step.get("travel_mode", ""),
        }
        # 대중교통 세부정보
        transit = step.get("transit_details")
        if transit:
            line = transit.get("line", {})
            step_info["transit"] = {
                "line_name": line.get("name", ""),
                "short_name": line.get("short_name", ""),
                "vehicle": line.get("vehicle", {}).get("name", ""),
                "departure_stop": transit.get("departure_stop", {}).get("name", ""),
                "arrival_stop": transit.get("arrival_stop", {}).get("name", ""),
                "num_stops": transit.get("num_stops", 0),
            }
        steps_simplified.append(step_info)

    return {
        "summary": route.get("summary", ""),
        "total_distance": leg.get("distance", {}).get("text", ""),
        "total_duration": leg.get("duration", {}).get("text", ""),
        "start_address": leg.get("start_address", ""),
        "end_address": leg.get("end_address", ""),
        "steps": steps_simplified,
    }


# ============================================================
# Tool Executors
# ============================================================


class GooglePlacesSearchTool(ToolExecutor):
    """Google Places 장소 검색 도구

    Places API (New) Text Search 기반
    글로벌 장소 검색 — 해외 음식점/관광지/시설 검색에 강점
    한국 내 장소는 카카오맵/네이버 검색이 더 정확

    Args:
        client: GoogleMapsClient 인스턴스
    """

    def __init__(self, client: GoogleMapsClient) -> None:
        self._client = client

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="google_places_search",
            description=(
                "Google Places API로 전 세계 장소를 검색합니다. "
                "음식점, 호텔, 관광지, 병원 등 모든 종류의 장소를 자연어로 검색합니다. "
                "해외 장소 검색에 가장 정확합니다. "
                "한국 내 장소는 카카오맵이나 네이버 검색이 더 정확합니다."
            ),
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="검색 쿼리 (예: 'pizza in New York' / '도쿄 라멘 맛집')",
                    required=True,
                ),
                ToolParameter(
                    name="language",
                    type="string",
                    description="결과 언어 코드 (ko/en/ja 등 / 기본값 ko)",
                    required=False,
                    default="ko",
                ),
                ToolParameter(
                    name="max_results",
                    type="integer",
                    description="최대 결과 수 (1~20 / 기본값 10)",
                    required=False,
                ),
                ToolParameter(
                    name="location_bias",
                    type="string",
                    description="위치 편향 '위도,경도' (예: '37.5665,126.9780' — 서울 중심)",
                    required=False,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        query = parameters.get("query", "").strip()
        if not query:
            return ToolResult(tool_name="google_places_search", success=False, error="query가 비어있습니다")

        language = parameters.get("language", "ko")
        max_results = parameters.get("max_results", 10)

        # location_bias 파싱
        lat: float | None = None
        lng: float | None = None
        loc_bias = parameters.get("location_bias", "")
        if loc_bias:
            try:
                parts = loc_bias.split(",")
                lat = float(parts[0].strip())
                lng = float(parts[1].strip())
            except (ValueError, IndexError):
                pass

        try:
            raw = await self._client.places_text_search(
                query=query,
                language=language,
                max_results=max_results,
                location_bias_lat=lat,
                location_bias_lng=lng,
            )

            places = raw.get("places", [])
            results = [_simplify_place(p) for p in places]

            logger.info("google_places_search_success", query=query, result_count=len(results))

            return ToolResult(
                tool_name="google_places_search",
                success=True,
                output={
                    "query": query,
                    "results": results,
                    "result_count": len(results),
                },
            )

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            body = e.response.text[:300]
            error_msg = f"Google Places API 에러 (HTTP {status}): {body}"
            if status == 403:
                error_msg = "Google Places API 키가 유효하지 않거나 Places API (New)가 활성화되지 않았습니다"
            logger.error("google_places_search_failed", query=query, status=status)
            return ToolResult(tool_name="google_places_search", success=False, error=error_msg)

        except Exception as e:
            error_str = str(e)[:300]
            logger.error("google_places_search_failed", query=query, error=error_str)
            return ToolResult(tool_name="google_places_search", success=False, error=f"장소 검색 실패: {error_str}")


class GoogleGeocodeTool(ToolExecutor):
    """Google Geocoding 도구

    주소 → 좌표 (정방향) 또는 좌표 → 주소 (역방향) 변환
    글로벌 주소 체계 지원

    Args:
        client: GoogleMapsClient 인스턴스
    """

    def __init__(self, client: GoogleMapsClient) -> None:
        self._client = client

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="google_geocode",
            description=(
                "Google Geocoding API로 주소와 좌표를 변환합니다. "
                "주소 → 위도/경도 (정방향) 또는 위도/경도 → 주소 (역방향) 변환을 지원합니다. "
                "전 세계 주소 체계를 지원하며 글로벌 서비스에 적합합니다."
            ),
            parameters=[
                ToolParameter(
                    name="address",
                    type="string",
                    description="변환할 주소 (정방향 / 예: '1600 Amphitheatre Parkway, Mountain View')",
                    required=False,
                ),
                ToolParameter(
                    name="latlng",
                    type="string",
                    description="변환할 좌표 '위도,경도' (역방향 / 예: '37.4224764,-122.0842499')",
                    required=False,
                ),
                ToolParameter(
                    name="language",
                    type="string",
                    description="결과 언어 코드 (기본값 ko)",
                    required=False,
                    default="ko",
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        address = parameters.get("address", "").strip()
        latlng = parameters.get("latlng", "").strip()
        language = parameters.get("language", "ko")

        if not address and not latlng:
            return ToolResult(
                tool_name="google_geocode",
                success=False,
                error="address 또는 latlng 중 하나는 필수입니다",
            )

        try:
            raw = await self._client.geocode(
                address=address or None,
                latlng=latlng or None,
                language=language,
            )

            status = raw.get("status", "")
            if status != "OK":
                error_msg = raw.get("error_message", f"Geocoding 실패: {status}")
                if status == "ZERO_RESULTS":
                    error_msg = "검색 결과가 없습니다"
                return ToolResult(tool_name="google_geocode", success=False, error=error_msg)

            raw_results = raw.get("results", [])
            results = [_simplify_geocode_result(r) for r in raw_results[:5]]

            mode = "정방향" if address else "역방향"
            logger.info("google_geocode_success", mode=mode, result_count=len(results))

            return ToolResult(
                tool_name="google_geocode",
                success=True,
                output={
                    "mode": mode,
                    "query": address or latlng,
                    "results": results,
                    "result_count": len(results),
                },
            )

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code == 403:
                return ToolResult(tool_name="google_geocode", success=False, error="Geocoding API 키가 유효하지 않거나 API가 활성화되지 않았습니다")
            return ToolResult(tool_name="google_geocode", success=False, error=f"Geocoding API 에러 (HTTP {status_code})")

        except Exception as e:
            return ToolResult(tool_name="google_geocode", success=False, error=f"지오코딩 실패: {str(e)[:300]}")


class GoogleDirectionsTool(ToolExecutor):
    """Google Directions 경로 탐색 도구

    자동차/도보/자전거/대중교통 경로 탐색
    글로벌 경로 탐색 — 해외 여행 경로 안내에 최적

    Args:
        client: GoogleMapsClient 인스턴스
    """

    def __init__(self, client: GoogleMapsClient) -> None:
        self._client = client

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="google_directions",
            description=(
                "Google Directions API로 경로를 탐색합니다. "
                "자동차, 도보, 자전거, 대중교통 경로를 지원하며 "
                "단계별 안내, 소요시간, 거리를 제공합니다. "
                "해외 경로 탐색에 적합합니다. 한국 대중교통은 TMAP이 더 정확합니다."
            ),
            parameters=[
                ToolParameter(
                    name="origin",
                    type="string",
                    description="출발지 (주소 또는 '위도,경도')",
                    required=True,
                ),
                ToolParameter(
                    name="destination",
                    type="string",
                    description="도착지 (주소 또는 '위도,경도')",
                    required=True,
                ),
                ToolParameter(
                    name="mode",
                    type="string",
                    description="이동 수단 (driving/walking/bicycling/transit / 기본값 transit)",
                    required=False,
                    default="transit",
                    enum=["driving", "walking", "bicycling", "transit"],
                ),
                ToolParameter(
                    name="language",
                    type="string",
                    description="결과 언어 코드 (기본값 ko)",
                    required=False,
                    default="ko",
                ),
                ToolParameter(
                    name="alternatives",
                    type="boolean",
                    description="대안 경로 포함 여부 (기본값 false)",
                    required=False,
                ),
            ],
            category=ToolCategory.MCP,
            safety_hint=SafetyLevelHint.READ_ONLY,
            version="1.0.0",
        )

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        origin = parameters.get("origin", "").strip()
        destination = parameters.get("destination", "").strip()

        if not origin:
            return ToolResult(tool_name="google_directions", success=False, error="origin이 비어있습니다")
        if not destination:
            return ToolResult(tool_name="google_directions", success=False, error="destination이 비어있습니다")

        mode = parameters.get("mode", "transit")
        language = parameters.get("language", "ko")
        alternatives = bool(parameters.get("alternatives", False))

        try:
            raw = await self._client.directions(
                origin=origin,
                destination=destination,
                mode=mode,
                language=language,
                alternatives=alternatives,
            )

            status = raw.get("status", "")
            if status != "OK":
                error_msg = f"경로 탐색 실패: {status}"
                if status == "NOT_FOUND":
                    error_msg = "출발지 또는 도착지를 찾을 수 없습니다"
                elif status == "ZERO_RESULTS":
                    error_msg = f"{mode} 경로를 찾을 수 없습니다"
                return ToolResult(tool_name="google_directions", success=False, error=error_msg)

            routes = raw.get("routes", [])
            results = [_simplify_route(r) for r in routes[:3]]

            logger.info(
                "google_directions_success",
                origin=origin[:50],
                destination=destination[:50],
                mode=mode,
                routes=len(results),
            )

            return ToolResult(
                tool_name="google_directions",
                success=True,
                output={
                    "origin": origin,
                    "destination": destination,
                    "mode": mode,
                    "routes": results,
                    "route_count": len(results),
                },
            )

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code == 403:
                return ToolResult(tool_name="google_directions", success=False, error="Directions API 키가 유효하지 않거나 API가 활성화되지 않았습니다")
            return ToolResult(tool_name="google_directions", success=False, error=f"Directions API 에러 (HTTP {status_code})")

        except Exception as e:
            return ToolResult(tool_name="google_directions", success=False, error=f"경로 탐색 실패: {str(e)[:300]}")
