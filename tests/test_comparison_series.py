from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.comparison_series import NumericSeriesPoint, envelope_downsample


def test_envelope_downsampling_keeps_order_endpoints_and_each_bucket_extrema() -> None:
    start = datetime(2026, 8, 28, tzinfo=UTC)
    points = [
        NumericSeriesPoint(start + timedelta(minutes=index), index + 1, Decimal(value))
        for index, value in enumerate((0, 4, -6, 2, 8, -3, 1, 7, -9, 5, 0, 3))
    ]

    sampled = envelope_downsample(points, maximum=10)

    assert len(sampled) <= 10
    assert sampled[0] == points[0]
    assert sampled[-1] == points[-1]
    assert {Decimal("8"), Decimal("-9")} <= {point.value for point in sampled}
    assert [point.sequence for point in sampled] == sorted(point.sequence for point in sampled)
    assert envelope_downsample(points[:3], maximum=10) == points[:3]
    with pytest.raises(ValueError, match="endpoints and extrema"):
        envelope_downsample(points, maximum=3)
