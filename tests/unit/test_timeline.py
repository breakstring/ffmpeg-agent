from fractions import Fraction

import pytest

from media_service.errors import ServiceError
from media_service.media import fraction_milliseconds, parse_timeline


def timeline(pts, time_base="1/30000"):
    return {"streams": [{"time_base": time_base}], "frames": [{"pts": value} for value in pts]}


def test_exact_selection_before_response_rounding():
    result = parse_timeline(timeline([60000, 61001, 62002]))
    assert result.times == (Fraction(0), Fraction(1001, 30000), Fraction(1001, 15000))
    assert result.first_at(33) == 1
    assert result.first_at(34) == 2
    assert result.first_at(67) is None
    assert fraction_milliseconds(Fraction(1, 2000)) == 1
    assert fraction_milliseconds(Fraction(1, 3000)) == 0


@pytest.mark.parametrize(
    "raw",
    [
        timeline([]),
        timeline([0, 0]),
        timeline([10, 9]),
        timeline([0, None]),
        timeline(["0"]),
        timeline([False]),
        timeline([0], "0/1"),
        timeline([0], "1/0"),
        {"streams": [{"time_base": "1/1000"}], "frames": [{"best_effort_timestamp": 0}]},
        {"streams": [{"time_base": "1/1000"}], "frames": [{"pts": 0, "best_effort_timestamp": 1}]},
    ],
)
def test_missing_non_monotonic_or_synthesized_timestamps_are_rejected(raw):
    with pytest.raises(ServiceError, match="INVALID_TIMELINE"):
        parse_timeline(raw)
