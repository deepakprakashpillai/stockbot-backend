"""Deterministic, presentation-neutral helpers for bounded comparison reads."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class NumericSeriesPoint:
    timestamp: datetime
    sequence: int
    value: Decimal


def envelope_downsample(
    points: Sequence[NumericSeriesPoint], maximum: int
) -> list[NumericSeriesPoint]:
    """Keep chronology, endpoints, and local extrema within a strict point cap."""

    ordered = list(points)
    if len(ordered) <= maximum:
        return ordered
    if maximum < 4:
        raise ValueError("maximum must leave room for endpoints and extrema")

    selected = {0, len(ordered) - 1}
    interior = list(range(1, len(ordered) - 1))
    bucket_count = max(1, (maximum - 2) // 2)
    bucket_size = max(1, (len(interior) + bucket_count - 1) // bucket_count)
    for offset in range(0, len(interior), bucket_size):
        bucket = interior[offset : offset + bucket_size]
        selected.add(min(bucket, key=lambda index: ordered[index].value))
        selected.add(max(bucket, key=lambda index: ordered[index].value))

    # The ceiling division above produces at most ``bucket_count`` buckets,
    # each contributing at most two points, so this remains within ``maximum``.
    return [ordered[index] for index in sorted(selected)]
