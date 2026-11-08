from __future__ import annotations

import math
from collections import defaultdict
from threading import Lock


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class MetricsRegistry:
    """Small, dependency-free Prometheus text registry for bounded platform metrics."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[int, float]] = {}

    @staticmethod
    def _key(name: str, labels: dict[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((labels or {}).items()))

    def increment(self, name: str, labels: dict[str, str] | None = None, value: float = 1) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = value

    def add_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] += value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        if not math.isfinite(value):
            return
        with self._lock:
            key = self._key(name, labels)
            count, total = self._histograms.get(key, (0, 0.0))
            self._histograms[key] = (count + 1, total + max(0.0, value))

    @staticmethod
    def _labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        return "{" + ",".join(f'{key}="{_escape(value)}"' for key, value in labels) + "}"

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = dict(self._histograms)
        declared: set[str] = set()
        for (name, labels), value in sorted(counters.items()):
            if name not in declared:
                lines.append(f"# TYPE {name} counter")
                declared.add(name)
            lines.append(f"{name}{self._labels(labels)} {value:g}")
        for (name, labels), value in sorted(gauges.items()):
            if name not in declared:
                lines.append(f"# TYPE {name} gauge")
                declared.add(name)
            lines.append(f"{name}{self._labels(labels)} {value:g}")
        for (name, labels), (count, total) in sorted(histograms.items()):
            label_values = dict(labels)
            if name not in declared:
                lines.append(f"# TYPE {name} summary")
                declared.add(name)
            count_labels = tuple(sorted(label_values.items()))
            lines.append(f"{name}_count{self._labels(count_labels)} {count}")
            lines.append(f"{name}_sum{self._labels(count_labels)} {total:g}")
        return "\n".join(lines) + "\n"
