"""ARIA Engine - Google Maps Tools Tests

Google Maps Platform 도구 3종 테스트
- GoogleMapsConfig: 환경변수 + is_configured
- GoogleMapsClient: httpx 호출 + 응답 파싱
- GooglePlacesSearchTool: 정의 + 실행 + 에러
- GoogleGeocodeTool: 정방향/역방향 + 에러
- GoogleDirectionsTool: 경로 탐색 + 모드 + 에러
- 응답 정제 유틸리티
- LLM function calling 포맷
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# GoogleMapsConfig Tests
# ============================================================


class TestGoogleMapsConfig:
    """GoogleMapsConfig 환경변수 + 프로퍼티"""

    def test_default_values(self):
        from aria.core.config import GoogleMapsConfig
        config = GoogleMapsConfig(api_key="")
        assert config.api_key == ""
        assert config.request_timeout == 15
        assert config.is_configured is False

    def test_configured_with_key(self):
        from aria.core.config import GoogleMapsConfig
        config = GoogleMapsConfig(api_key="test-key-123")
        assert config.is_configured is True

    def test_in_aria_config(self):
        from aria.core.config import AriaConfig, GoogleMapsConfig
        config = AriaConfig()
        assert hasattr(config, "google_maps")
        assert isinstance(config.google_maps, GoogleMapsConfig)


# ============================================================
# Client Tests
# ============================================================


class TestGoogleMapsClient:
    """GoogleMapsClient 초기화 + lazy init"""

    def test_init(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient
        config = GoogleMapsConfig(api_key="test-key")
        client = GoogleMapsClient(config)
        assert client._client is None

    @pytest.mark.asyncio
    async def test_lazy_client_creation(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient
        config = GoogleMapsConfig(api_key="test-key")
        client = GoogleMapsClient(config)
        http_client = await client._get_client()
        assert http_client is not None
        assert not http_client.is_closed
        await client.close()


# ============================================================
# Response Simplification Tests
# ============================================================


class TestPlaceSimplification:
    """Places API 응답 정제"""

    def test_simplify_place_full(self):
        from aria.tools.mcp.google_maps_tools import _simplify_place
        raw = {
            "id": "ChIJ123",
            "displayName": {"text": "Test Restaurant", "languageCode": "en"},
            "formattedAddress": "123 Main St, City",
            "location": {"latitude": 37.5, "longitude": 126.9},
            "rating": 4.5,
            "userRatingCount": 200,
            "nationalPhoneNumber": "02-1234-5678",
            "websiteUri": "https://test.com",
            "googleMapsUri": "https://maps.google.com/?cid=123",
            "currentOpeningHours": {"openNow": True},
            "businessStatus": "OPERATIONAL",
            "types": ["restaurant", "food", "point_of_interest"],
        }
        result = _simplify_place(raw)
        assert result["name"] == "Test Restaurant"
        assert result["address"] == "123 Main St, City"
        assert result["latitude"] == 37.5
        assert result["longitude"] == 126.9
        assert result["rating"] == 4.5
        assert result["is_open"] is True
        assert result["place_id"] == "ChIJ123"

    def test_simplify_place_minimal(self):
        from aria.tools.mcp.google_maps_tools import _simplify_place
        result = _simplify_place({})
        assert result["name"] == ""
        assert result["latitude"] is None
        assert result["is_open"] is None


class TestGeocodeSimplification:
    """Geocoding 결과 정제"""

    def test_simplify_geocode(self):
        from aria.tools.mcp.google_maps_tools import _simplify_geocode_result
        raw = {
            "formatted_address": "서울특별시 중구",
            "geometry": {"location": {"lat": 37.5, "lng": 126.9}},
            "place_id": "ChIJ456",
            "types": ["locality", "political"],
        }
        result = _simplify_geocode_result(raw)
        assert result["formatted_address"] == "서울특별시 중구"
        assert result["latitude"] == 37.5
        assert result["place_id"] == "ChIJ456"


class TestRouteSimplification:
    """Directions 경로 정제"""

    def test_simplify_route(self):
        from aria.tools.mcp.google_maps_tools import _simplify_route
        raw = {
            "summary": "Route via Highway",
            "legs": [{
                "distance": {"text": "10 km"},
                "duration": {"text": "15분"},
                "start_address": "A",
                "end_address": "B",
                "steps": [
                    {
                        "html_instructions": "Turn left",
                        "distance": {"text": "1 km"},
                        "duration": {"text": "2분"},
                        "travel_mode": "DRIVING",
                    },
                ],
            }],
        }
        result = _simplify_route(raw)
        assert result["total_distance"] == "10 km"
        assert result["total_duration"] == "15분"
        assert len(result["steps"]) == 1

    def test_simplify_route_transit(self):
        from aria.tools.mcp.google_maps_tools import _simplify_route
        raw = {
            "legs": [{
                "distance": {"text": "5 km"},
                "duration": {"text": "20분"},
                "start_address": "A",
                "end_address": "B",
                "steps": [{
                    "html_instructions": "Take subway",
                    "distance": {"text": "3 km"},
                    "duration": {"text": "10분"},
                    "travel_mode": "TRANSIT",
                    "transit_details": {
                        "line": {"name": "2호선", "short_name": "2", "vehicle": {"name": "Subway"}},
                        "departure_stop": {"name": "강남역"},
                        "arrival_stop": {"name": "잠실역"},
                        "num_stops": 5,
                    },
                }],
            }],
        }
        result = _simplify_route(raw)
        transit = result["steps"][0]["transit"]
        assert transit["line_name"] == "2호선"
        assert transit["departure_stop"] == "강남역"
        assert transit["num_stops"] == 5

    def test_simplify_route_empty(self):
        from aria.tools.mcp.google_maps_tools import _simplify_route
        result = _simplify_route({"legs": []})
        assert "error" in result


# ============================================================
# GooglePlacesSearchTool Tests
# ============================================================


class TestGooglePlacesSearchTool:
    """Places 검색 도구"""

    def test_definition(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GooglePlacesSearchTool
        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GooglePlacesSearchTool(client)
        defn = tool.get_definition()
        assert defn.name == "google_places_search"
        assert defn.category.value == "mcp"
        assert defn.safety_hint.value == "read_only"
        assert any(p.name == "query" and p.required for p in defn.parameters)

    @pytest.mark.asyncio
    async def test_empty_query(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GooglePlacesSearchTool
        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GooglePlacesSearchTool(client)
        result = await tool.execute({"query": ""})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_success(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GooglePlacesSearchTool

        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GooglePlacesSearchTool(client)

        mock_response = {
            "places": [
                {
                    "id": "ChIJ123",
                    "displayName": {"text": "Pizza Place"},
                    "formattedAddress": "123 Broadway, NY",
                    "location": {"latitude": 40.7, "longitude": -74.0},
                    "rating": 4.2,
                },
            ],
        }

        with patch.object(client, "places_text_search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = mock_response
            result = await tool.execute({"query": "pizza in New York"})

        assert result.success is True
        assert result.output["result_count"] == 1
        assert result.output["results"][0]["name"] == "Pizza Place"

    @pytest.mark.asyncio
    async def test_location_bias_parsing(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GooglePlacesSearchTool

        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GooglePlacesSearchTool(client)

        with patch.object(client, "places_text_search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {"places": []}
            await tool.execute({"query": "cafe", "location_bias": "37.5,126.9"})

        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["location_bias_lat"] == 37.5
        assert call_kwargs["location_bias_lng"] == 126.9


# ============================================================
# GoogleGeocodeTool Tests
# ============================================================


class TestGoogleGeocodeTool:
    """Geocoding 도구"""

    def test_definition(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleGeocodeTool
        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GoogleGeocodeTool(client)
        defn = tool.get_definition()
        assert defn.name == "google_geocode"

    @pytest.mark.asyncio
    async def test_no_params(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleGeocodeTool
        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GoogleGeocodeTool(client)
        result = await tool.execute({})
        assert result.success is False
        assert "필수" in result.error

    @pytest.mark.asyncio
    async def test_forward_geocode(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleGeocodeTool

        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GoogleGeocodeTool(client)

        with patch.object(client, "geocode", new_callable=AsyncMock) as mock_geo:
            mock_geo.return_value = {
                "status": "OK",
                "results": [{
                    "formatted_address": "서울특별시",
                    "geometry": {"location": {"lat": 37.5, "lng": 126.9}},
                    "place_id": "abc",
                    "types": ["locality"],
                }],
            }
            result = await tool.execute({"address": "서울"})

        assert result.success is True
        assert result.output["mode"] == "정방향"
        assert result.output["results"][0]["latitude"] == 37.5

    @pytest.mark.asyncio
    async def test_reverse_geocode(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleGeocodeTool

        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GoogleGeocodeTool(client)

        with patch.object(client, "geocode", new_callable=AsyncMock) as mock_geo:
            mock_geo.return_value = {
                "status": "OK",
                "results": [{"formatted_address": "Main St", "geometry": {"location": {"lat": 37, "lng": 127}}, "place_id": "x", "types": []}],
            }
            result = await tool.execute({"latlng": "37,127"})

        assert result.success is True
        assert result.output["mode"] == "역방향"

    @pytest.mark.asyncio
    async def test_zero_results(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleGeocodeTool

        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GoogleGeocodeTool(client)

        with patch.object(client, "geocode", new_callable=AsyncMock) as mock_geo:
            mock_geo.return_value = {"status": "ZERO_RESULTS", "results": []}
            result = await tool.execute({"address": "asdfjkl"})

        assert result.success is False
        assert "결과가 없습니다" in result.error


# ============================================================
# GoogleDirectionsTool Tests
# ============================================================


class TestGoogleDirectionsTool:
    """Directions 경로 탐색 도구"""

    def test_definition(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleDirectionsTool
        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GoogleDirectionsTool(client)
        defn = tool.get_definition()
        assert defn.name == "google_directions"
        assert any(p.name == "origin" and p.required for p in defn.parameters)
        assert any(p.name == "destination" and p.required for p in defn.parameters)

    @pytest.mark.asyncio
    async def test_empty_origin(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleDirectionsTool
        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GoogleDirectionsTool(client)
        result = await tool.execute({"origin": "", "destination": "B"})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_empty_destination(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleDirectionsTool
        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GoogleDirectionsTool(client)
        result = await tool.execute({"origin": "A", "destination": ""})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_success(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleDirectionsTool

        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GoogleDirectionsTool(client)

        with patch.object(client, "directions", new_callable=AsyncMock) as mock_dir:
            mock_dir.return_value = {
                "status": "OK",
                "routes": [{
                    "summary": "via I-95",
                    "legs": [{
                        "distance": {"text": "100 km"},
                        "duration": {"text": "1시간"},
                        "start_address": "A",
                        "end_address": "B",
                        "steps": [],
                    }],
                }],
            }
            result = await tool.execute({"origin": "Seoul", "destination": "Busan", "mode": "driving"})

        assert result.success is True
        assert result.output["route_count"] == 1
        assert result.output["routes"][0]["total_distance"] == "100 km"

    @pytest.mark.asyncio
    async def test_not_found(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleDirectionsTool

        client = GoogleMapsClient(GoogleMapsConfig(api_key="test"))
        tool = GoogleDirectionsTool(client)

        with patch.object(client, "directions", new_callable=AsyncMock) as mock_dir:
            mock_dir.return_value = {"status": "NOT_FOUND", "routes": []}
            result = await tool.execute({"origin": "nowhere", "destination": "nowhere2"})

        assert result.success is False
        assert "찾을 수 없습니다" in result.error


# ============================================================
# LLM Format Tests
# ============================================================


class TestGoogleMapsLLMFormat:
    """LLM function calling 포맷 변환"""

    def test_places_search(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GooglePlacesSearchTool
        llm = GooglePlacesSearchTool(GoogleMapsClient(GoogleMapsConfig(api_key="t"))).get_definition().to_llm_tool()
        assert llm["type"] == "function"
        assert llm["function"]["name"] == "google_places_search"

    def test_geocode(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleGeocodeTool
        llm = GoogleGeocodeTool(GoogleMapsClient(GoogleMapsConfig(api_key="t"))).get_definition().to_llm_tool()
        assert llm["function"]["name"] == "google_geocode"

    def test_directions(self):
        from aria.core.config import GoogleMapsConfig
        from aria.tools.mcp.google_maps_tools import GoogleMapsClient, GoogleDirectionsTool
        llm = GoogleDirectionsTool(GoogleMapsClient(GoogleMapsConfig(api_key="t"))).get_definition().to_llm_tool()
        assert llm["function"]["name"] == "google_directions"
