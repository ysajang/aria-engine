"""ARIA Engine - Product Insight Store (Phase B: AI 개인화)

제품별 모니터링 스냅샷 저장 + 추세 분석
- InsightStore: 제품별 스냅샷 이력 관리 (in-memory + JSON 파일 백업)
- Snapshot: 단일 시점 모니터링 결과
- Trend: 지표별 추세 계산 (이동평균 / 방향 / 변동성)

설계 원칙:
- scope별 격리: 제품 ID로 데이터 완전 분리
- 경량: 스냅샷 최대 90일 보관 (자동 정리)
- Exit-Safe: JSON 직렬화 → 파일 백업 가능
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

MAX_SNAPSHOTS_PER_PRODUCT = 90  # 최대 90일치
TREND_WINDOW = 7  # 추세 분석 기본 윈도우 (7 스냅샷)


class Snapshot:
    """단일 시점 모니터링 스냅샷"""

    __slots__ = ("timestamp", "metrics", "issues_count", "high_issues")

    def __init__(
        self,
        metrics: dict[str, float],
        issues_count: int = 0,
        high_issues: int = 0,
        timestamp: str | None = None,
    ) -> None:
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        self.metrics = metrics
        self.issues_count = issues_count
        self.high_issues = high_issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "metrics": self.metrics,
            "issues_count": self.issues_count,
            "high_issues": self.high_issues,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Snapshot:
        return cls(
            metrics=data.get("metrics", {}),
            issues_count=data.get("issues_count", 0),
            high_issues=data.get("high_issues", 0),
            timestamp=data.get("timestamp"),
        )


class Trend:
    """지표 추세 분석 결과"""

    __slots__ = ("metric", "current", "avg", "direction", "change_pct", "volatility")

    def __init__(
        self,
        metric: str,
        current: float,
        avg: float,
        direction: str,
        change_pct: float,
        volatility: float,
    ) -> None:
        self.metric = metric
        self.current = current
        self.avg = avg
        self.direction = direction      # "up" / "down" / "stable"
        self.change_pct = change_pct    # 현재 vs 평균 변화율 (%)
        self.volatility = volatility    # 표준편차 / 평균 (%)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "current": self.current,
            "avg": round(self.avg, 2),
            "direction": self.direction,
            "change_pct": round(self.change_pct, 1),
            "volatility": round(self.volatility, 1),
        }


def _calc_trend(metric: str, values: list[float]) -> Trend | None:
    """값 목록에서 추세 계산"""
    if len(values) < 2:
        return None

    current = values[-1]
    avg = sum(values) / len(values)

    # 방향: 마지막 값 vs 평균
    change_pct = ((current - avg) / avg * 100) if avg != 0 else 0.0

    if change_pct > 5:
        direction = "up"
    elif change_pct < -5:
        direction = "down"
    else:
        direction = "stable"

    # 변동성: 변동계수 (CV)
    if avg != 0:
        variance = sum((v - avg) ** 2 for v in values) / len(values)
        std = variance ** 0.5
        volatility = (std / abs(avg)) * 100
    else:
        volatility = 0.0

    return Trend(
        metric=metric,
        current=current,
        avg=avg,
        direction=direction,
        change_pct=change_pct,
        volatility=volatility,
    )


class InsightStore:
    """제품별 모니터링 스냅샷 저장소

    Thread-safe / 제품 ID 기반 격리 / 자동 정리
    """

    def __init__(
        self,
        max_snapshots: int = MAX_SNAPSHOTS_PER_PRODUCT,
        backup_dir: str | None = None,
    ) -> None:
        self._store: dict[str, deque[Snapshot]] = {}
        self._lock = threading.Lock()
        self._max = max_snapshots
        self._backup_dir = Path(backup_dir) if backup_dir else None

    def add_snapshot(
        self,
        product_id: str,
        metrics: dict[str, float],
        issues_count: int = 0,
        high_issues: int = 0,
    ) -> Snapshot:
        """스냅샷 추가

        Args:
            product_id: 제품 ID
            metrics: 지표 dict (예: {"bounce_rate": 45.0, "refund_rate": 2.0})
            issues_count: 이슈 수
            high_issues: 심각 이슈 수

        Returns:
            생성된 Snapshot
        """
        snap = Snapshot(metrics=metrics, issues_count=issues_count, high_issues=high_issues)

        with self._lock:
            if product_id not in self._store:
                self._store[product_id] = deque(maxlen=self._max)
            self._store[product_id].append(snap)

        return snap

    def get_snapshots(
        self,
        product_id: str,
        last_n: int | None = None,
    ) -> list[Snapshot]:
        """스냅샷 조회

        Args:
            product_id: 제품 ID
            last_n: 최근 N개 (None이면 전체)
        """
        with self._lock:
            snaps = list(self._store.get(product_id, []))

        if last_n and last_n > 0:
            return snaps[-last_n:]
        return snaps

    def get_trends(
        self,
        product_id: str,
        window: int = TREND_WINDOW,
    ) -> list[Trend]:
        """제품별 지표 추세 분석

        Args:
            product_id: 제품 ID
            window: 분석 윈도우 (스냅샷 수)

        Returns:
            Trend 목록 (지표별)
        """
        snaps = self.get_snapshots(product_id, last_n=window)
        if len(snaps) < 2:
            return []

        # 지표별 값 수집
        metric_values: dict[str, list[float]] = {}
        for snap in snaps:
            for key, val in snap.metrics.items():
                if key not in metric_values:
                    metric_values[key] = []
                metric_values[key].append(float(val))

        trends = []
        for metric, values in metric_values.items():
            trend = _calc_trend(metric, values)
            if trend:
                trends.append(trend)

        return trends

    def get_anomalies(
        self,
        product_id: str,
        window: int = TREND_WINDOW,
        threshold_sigma: float = 2.0,
    ) -> list[dict[str, Any]]:
        """이상 탐지: 현재 값이 평균에서 threshold_sigma 표준편차 이상 벗어난 지표

        Returns:
            [{metric, current, avg, std, deviation_sigma, direction}]
        """
        snaps = self.get_snapshots(product_id, last_n=window)
        if len(snaps) < 3:
            return []

        metric_values: dict[str, list[float]] = {}
        for snap in snaps:
            for key, val in snap.metrics.items():
                if key not in metric_values:
                    metric_values[key] = []
                metric_values[key].append(float(val))

        anomalies = []
        for metric, values in metric_values.items():
            if len(values) < 3:
                continue

            avg = sum(values) / len(values)
            variance = sum((v - avg) ** 2 for v in values) / len(values)
            std = variance ** 0.5

            if std == 0:
                continue

            current = values[-1]
            deviation = abs(current - avg) / std

            if deviation >= threshold_sigma:
                anomalies.append({
                    "metric": metric,
                    "current": current,
                    "avg": round(avg, 2),
                    "std": round(std, 2),
                    "deviation_sigma": round(deviation, 2),
                    "direction": "above" if current > avg else "below",
                })

        return anomalies

    def get_product_ids(self) -> list[str]:
        """등록된 제품 ID 목록"""
        with self._lock:
            return list(self._store.keys())

    def snapshot_count(self, product_id: str) -> int:
        """특정 제품 스냅샷 수"""
        with self._lock:
            return len(self._store.get(product_id, []))

    def clear(self, product_id: str | None = None) -> int:
        """스냅샷 삭제

        Args:
            product_id: 특정 제품만 (None이면 전체)

        Returns:
            삭제된 스냅샷 수
        """
        with self._lock:
            if product_id:
                count = len(self._store.pop(product_id, []))
            else:
                count = sum(len(v) for v in self._store.values())
                self._store.clear()
        return count

    def save_backup(self) -> str | None:
        """JSON 파일 백업

        Returns:
            백업 파일 경로 (backup_dir 미설정이면 None)
        """
        if not self._backup_dir:
            return None

        self._backup_dir.mkdir(parents=True, exist_ok=True)
        path = self._backup_dir / "insight_store.json"

        with self._lock:
            data = {
                pid: [s.to_dict() for s in snaps]
                for pid, snaps in self._store.items()
            }

        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return str(path)

    def load_backup(self) -> int:
        """JSON 파일 복원

        Returns:
            복원된 스냅샷 수
        """
        if not self._backup_dir:
            return 0

        path = self._backup_dir / "insight_store.json"
        if not path.exists():
            return 0

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("insight_backup_load_failed", error=str(e)[:100])
            return 0

        count = 0
        with self._lock:
            for pid, snaps in data.items():
                if pid not in self._store:
                    self._store[pid] = deque(maxlen=self._max)
                for s in snaps:
                    self._store[pid].append(Snapshot.from_dict(s))
                    count += 1

        return count
