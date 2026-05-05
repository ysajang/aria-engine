"""ARIA Engine - Phase B: AI 개인화 Tests

InsightStore + InsightEngine + InsightTools 테스트

총 테스트: ~45개
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest


# ============================================================
# 1. Snapshot
# ============================================================


class TestSnapshot:
    """Snapshot 직렬화/역직렬화"""

    def test_create(self):
        from aria.monitoring.insight_store import Snapshot
        s = Snapshot(metrics={"bounce_rate": 45.0}, issues_count=2, high_issues=1)
        assert s.metrics["bounce_rate"] == 45.0
        assert s.issues_count == 2
        assert s.timestamp is not None

    def test_to_dict(self):
        from aria.monitoring.insight_store import Snapshot
        s = Snapshot(metrics={"x": 1.0}, timestamp="2026-01-01T00:00:00")
        d = s.to_dict()
        assert d["timestamp"] == "2026-01-01T00:00:00"
        assert d["metrics"]["x"] == 1.0

    def test_from_dict(self):
        from aria.monitoring.insight_store import Snapshot
        s = Snapshot.from_dict({"metrics": {"y": 2.0}, "issues_count": 3, "timestamp": "t1"})
        assert s.metrics["y"] == 2.0
        assert s.issues_count == 3


# ============================================================
# 2. Trend Calculation
# ============================================================


class TestCalcTrend:
    """_calc_trend 유틸리티"""

    def test_insufficient_data(self):
        from aria.monitoring.insight_store import _calc_trend
        assert _calc_trend("x", [1.0]) is None

    def test_stable(self):
        from aria.monitoring.insight_store import _calc_trend
        t = _calc_trend("x", [10.0, 10.1, 9.9, 10.0, 10.0])
        assert t is not None
        assert t.direction == "stable"

    def test_upward(self):
        from aria.monitoring.insight_store import _calc_trend
        t = _calc_trend("x", [10, 10, 10, 10, 20])
        assert t is not None
        assert t.direction == "up"
        assert t.change_pct > 5

    def test_downward(self):
        from aria.monitoring.insight_store import _calc_trend
        t = _calc_trend("x", [20, 20, 20, 20, 10])
        assert t is not None
        assert t.direction == "down"

    def test_to_dict(self):
        from aria.monitoring.insight_store import _calc_trend
        t = _calc_trend("bounce_rate", [50, 55, 60])
        assert t is not None
        d = t.to_dict()
        assert d["metric"] == "bounce_rate"
        assert "direction" in d


# ============================================================
# 3. InsightStore
# ============================================================


class TestInsightStore:
    """InsightStore 핵심 기능"""

    def test_add_and_get(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        store.add_snapshot("test", {"bounce_rate": 45.0})
        snaps = store.get_snapshots("test")
        assert len(snaps) == 1
        assert snaps[0].metrics["bounce_rate"] == 45.0

    def test_max_snapshots(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore(max_snapshots=5)
        for i in range(10):
            store.add_snapshot("test", {"val": float(i)})
        assert store.snapshot_count("test") == 5

    def test_last_n(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        for i in range(10):
            store.add_snapshot("test", {"val": float(i)})
        last3 = store.get_snapshots("test", last_n=3)
        assert len(last3) == 3
        assert last3[0].metrics["val"] == 7.0

    def test_product_isolation(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        store.add_snapshot("a", {"x": 1.0})
        store.add_snapshot("b", {"x": 2.0})
        assert store.snapshot_count("a") == 1
        assert store.snapshot_count("b") == 1
        assert store.get_snapshots("c") == []

    def test_get_product_ids(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        store.add_snapshot("testorum", {"x": 1})
        store.add_snapshot("mystel", {"x": 2})
        ids = store.get_product_ids()
        assert "testorum" in ids
        assert "mystel" in ids

    def test_clear_product(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        store.add_snapshot("a", {"x": 1})
        store.add_snapshot("b", {"x": 2})
        cleared = store.clear("a")
        assert cleared == 1
        assert store.snapshot_count("a") == 0
        assert store.snapshot_count("b") == 1

    def test_clear_all(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        store.add_snapshot("a", {"x": 1})
        store.add_snapshot("b", {"x": 2})
        cleared = store.clear()
        assert cleared == 2

    def test_trends(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        for i in range(7):
            store.add_snapshot("test", {"bounce_rate": 40.0 + i * 2})
        trends = store.get_trends("test")
        assert len(trends) >= 1
        br_trend = [t for t in trends if t.metric == "bounce_rate"][0]
        assert br_trend.direction in ("up", "stable", "down")

    def test_trends_insufficient_data(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        store.add_snapshot("test", {"x": 1})
        assert store.get_trends("test") == []

    def test_anomalies(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        # 안정적인 값들 + 마지막에 급등
        for _ in range(6):
            store.add_snapshot("test", {"x": 10.0})
        store.add_snapshot("test", {"x": 50.0})  # 이상치
        anomalies = store.get_anomalies("test")
        assert len(anomalies) >= 1
        assert anomalies[0]["metric"] == "x"
        assert anomalies[0]["direction"] == "above"

    def test_no_anomalies(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        for _ in range(7):
            store.add_snapshot("test", {"x": 10.0})
        assert store.get_anomalies("test") == []

    def test_backup_and_restore(self):
        from aria.monitoring.insight_store import InsightStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store1 = InsightStore(backup_dir=tmpdir)
            store1.add_snapshot("test", {"a": 1.0, "b": 2.0})
            store1.add_snapshot("test", {"a": 3.0, "b": 4.0})
            path = store1.save_backup()
            assert path is not None
            assert Path(path).exists()

            store2 = InsightStore(backup_dir=tmpdir)
            count = store2.load_backup()
            assert count == 2
            assert store2.snapshot_count("test") == 2

    def test_backup_no_dir(self):
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        assert store.save_backup() is None
        assert store.load_backup() == 0


# ============================================================
# 4. InsightEngine — Health Score
# ============================================================


class TestHealthScore:
    """generate_health_score"""

    def test_no_data(self):
        from aria.monitoring.insight_engine import generate_health_score
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        result = generate_health_score(store, "empty")
        assert result["grade"] == "N/A"
        assert result["score"] == 0.0

    def test_perfect_score(self):
        from aria.monitoring.insight_engine import generate_health_score
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        store.add_snapshot("test", {
            "bounce_rate": 0, "refund_rate": 0, "failure_rate": 0,
            "churn_rate": 0, "conversion_rate": 100, "engagement_rate": 100,
            "monthly_pct": 0,
        })
        result = generate_health_score(store, "test")
        assert result["score"] == 100.0
        assert result["grade"] == "A"

    def test_poor_score(self):
        from aria.monitoring.insight_engine import generate_health_score
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        store.add_snapshot("test", {
            "bounce_rate": 90, "refund_rate": 80, "failure_rate": 70,
            "churn_rate": 60, "conversion_rate": 5, "engagement_rate": 10,
            "monthly_pct": 95,
        })
        result = generate_health_score(store, "test")
        assert result["score"] < 40
        assert result["grade"] in ("D", "F")

    def test_partial_metrics(self):
        from aria.monitoring.insight_engine import generate_health_score
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        store.add_snapshot("test", {"bounce_rate": 30})
        result = generate_health_score(store, "test")
        assert result["score"] == 70.0  # 100 - 30


# ============================================================
# 5. InsightEngine — Insights
# ============================================================


class TestGenerateInsights:
    """generate_insights"""

    def test_no_data(self):
        from aria.monitoring.insight_engine import generate_insights
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        assert generate_insights(store, "empty") == []

    def test_declining_metric(self):
        from aria.monitoring.insight_engine import generate_insights
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        # bounce_rate 상승 (lower_is_better → declining)
        for i in range(7):
            store.add_snapshot("test", {"bounce_rate": 40 + i * 5})
        insights = generate_insights(store, "test")
        declining = [i for i in insights if i["category"] == "declining"]
        assert len(declining) >= 1

    def test_improving_metric(self):
        from aria.monitoring.insight_engine import generate_insights
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        # conversion_rate 상승 (higher_is_better → improving)
        for i in range(7):
            store.add_snapshot("test", {"conversion_rate": 2 + i * 1})
        insights = generate_insights(store, "test")
        improving = [i for i in insights if i["category"] == "improving"]
        assert len(improving) >= 1

    def test_anomaly_detected(self):
        from aria.monitoring.insight_engine import generate_insights
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        for _ in range(6):
            store.add_snapshot("test", {"refund_rate": 2.0})
        store.add_snapshot("test", {"refund_rate": 30.0})  # 이상치
        insights = generate_insights(store, "test")
        anomalies = [i for i in insights if i["category"] == "anomaly"]
        assert len(anomalies) >= 1


# ============================================================
# 6. InsightEngine — Recommendations
# ============================================================


class TestGenerateRecommendations:
    """generate_recommendations"""

    def test_no_data(self):
        from aria.monitoring.insight_engine import generate_recommendations
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        recs = generate_recommendations(store, "empty")
        assert len(recs) == 1
        assert recs[0]["category"] == "setup"

    def test_high_bounce_rate(self):
        from aria.monitoring.insight_engine import generate_recommendations
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        store.add_snapshot("test", {"bounce_rate": 85.0})
        recs = generate_recommendations(store, "test")
        ux_recs = [r for r in recs if r["category"] == "ux"]
        assert len(ux_recs) >= 1

    def test_high_refund_rate(self):
        from aria.monitoring.insight_engine import generate_recommendations
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        store.add_snapshot("test", {"refund_rate": 8.0})
        recs = generate_recommendations(store, "test")
        biz_recs = [r for r in recs if r["category"] == "business"]
        assert len(biz_recs) >= 1

    def test_data_accumulation_warning(self):
        from aria.monitoring.insight_engine import generate_recommendations
        from aria.monitoring.insight_store import InsightStore
        store = InsightStore()
        for i in range(3):
            store.add_snapshot("test", {"x": float(i)})
        recs = generate_recommendations(store, "test")
        setup_recs = [r for r in recs if r["category"] == "setup"]
        assert len(setup_recs) >= 1


# ============================================================
# 7. ProductInsightTool
# ============================================================


class TestProductInsightTool:
    """ProductInsightTool ToolExecutor"""

    def test_definition(self):
        from aria.monitoring.insight_store import InsightStore
        from aria.tools.mcp.insight_tools import ProductInsightTool
        tool = ProductInsightTool(InsightStore())
        defn = tool.get_definition()
        assert defn.name == "product_insight"
        assert defn.safety_hint.value == "read_only"

    @pytest.mark.asyncio
    async def test_missing_product_id(self):
        from aria.monitoring.insight_store import InsightStore
        from aria.tools.mcp.insight_tools import ProductInsightTool
        tool = ProductInsightTool(InsightStore())
        result = await tool.execute({"product_id": ""})
        assert not result.success

    @pytest.mark.asyncio
    async def test_with_data(self):
        from aria.monitoring.insight_store import InsightStore
        from aria.tools.mcp.insight_tools import ProductInsightTool
        store = InsightStore()
        for i in range(7):
            store.add_snapshot("testorum", {"bounce_rate": 40 + i, "refund_rate": 2.0})
        tool = ProductInsightTool(store)
        result = await tool.execute({"product_id": "testorum"})
        assert result.success
        assert result.output["snapshot_count"] == 7
        assert "health" in result.output
        assert "insights" in result.output

    @pytest.mark.asyncio
    async def test_llm_format(self):
        from aria.monitoring.insight_store import InsightStore
        from aria.tools.mcp.insight_tools import ProductInsightTool
        tool = ProductInsightTool(InsightStore())
        llm = tool.get_definition().to_llm_tool()
        assert llm["function"]["name"] == "product_insight"


# ============================================================
# 8. SnapshotRecordTool
# ============================================================


class TestSnapshotRecordTool:
    """SnapshotRecordTool ToolExecutor"""

    def test_definition(self):
        from aria.monitoring.insight_store import InsightStore
        from aria.tools.mcp.insight_tools import SnapshotRecordTool
        tool = SnapshotRecordTool(InsightStore())
        defn = tool.get_definition()
        assert defn.name == "snapshot_record"

    @pytest.mark.asyncio
    async def test_record_success(self):
        from aria.monitoring.insight_store import InsightStore
        from aria.tools.mcp.insight_tools import SnapshotRecordTool
        store = InsightStore()
        tool = SnapshotRecordTool(store)
        result = await tool.execute({
            "product_id": "testorum",
            "metrics": {"bounce_rate": 45.0, "refund_rate": 2.0},
        })
        assert result.success
        assert result.output["total_snapshots"] == 1
        assert result.output["metric_count"] == 2
        assert store.snapshot_count("testorum") == 1

    @pytest.mark.asyncio
    async def test_empty_metrics(self):
        from aria.monitoring.insight_store import InsightStore
        from aria.tools.mcp.insight_tools import SnapshotRecordTool
        tool = SnapshotRecordTool(InsightStore())
        result = await tool.execute({"product_id": "test", "metrics": {}})
        assert not result.success

    @pytest.mark.asyncio
    async def test_invalid_metric_values(self):
        from aria.monitoring.insight_store import InsightStore
        from aria.tools.mcp.insight_tools import SnapshotRecordTool
        tool = SnapshotRecordTool(InsightStore())
        result = await tool.execute({
            "product_id": "test",
            "metrics": {"x": "not_a_number"},
        })
        assert not result.success

    @pytest.mark.asyncio
    async def test_missing_product_id(self):
        from aria.monitoring.insight_store import InsightStore
        from aria.tools.mcp.insight_tools import SnapshotRecordTool
        tool = SnapshotRecordTool(InsightStore())
        result = await tool.execute({"product_id": "", "metrics": {"x": 1}})
        assert not result.success


# ============================================================
# 9. Metric Labels
# ============================================================


class TestMetricLabel:
    """_metric_label"""

    def test_known_metrics(self):
        from aria.monitoring.insight_engine import _metric_label
        assert _metric_label("bounce_rate") == "이탈률"
        assert _metric_label("refund_rate") == "환불율"
        assert _metric_label("conversion_rate") == "전환율"

    def test_unknown_metric(self):
        from aria.monitoring.insight_engine import _metric_label
        assert _metric_label("custom_xyz") == "custom_xyz"
