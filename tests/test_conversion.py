"""The percentage/dB mapping must match the gateway's own, exactly.

Reference: ``internal/matrix/service.go`` -- ``zonePos``/``PctOfDb`` and
``zoneDb``, over a fader range of -60 dB .. 0 dB.
"""

from __future__ import annotations

from conftest import api
import pytest


@pytest.mark.parametrize(
    ("pct", "db"),
    [
        (0, -60.0),
        (25, -45.0),
        (50, -30.0),
        (75, -15.0),
        (100, 0.0),
    ],
)
def test_pct_to_db_reference_points(pct: float, db: float) -> None:
    """Known anchors on the console fader."""
    assert api.pct_to_db(pct) == pytest.approx(db)
    assert api.db_to_pct(db) == pytest.approx(pct)


def test_pct_to_db_matches_the_linear_formula() -> None:
    """Check db == pct * 0.6 - 60 across the whole range."""
    for step in range(0, 101):
        assert api.pct_to_db(step) == pytest.approx(step * 0.6 - 60.0)


def test_round_trip_is_stable() -> None:
    """Check that pct -> dB -> pct returns the original position."""
    for step in range(0, 1001):
        pct = step / 10
        assert api.db_to_pct(api.pct_to_db(pct)) == pytest.approx(pct)


def test_round_trip_from_db() -> None:
    """Check that dB -> pct -> dB returns the original level in range."""
    for tenth in range(-600, 1):
        db = tenth / 10
        assert api.pct_to_db(api.db_to_pct(db)) == pytest.approx(db)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-120.0, 0.0), (-60.0, 0.0), (0.0, 100.0), (6.0, 100.0)],
)
def test_db_to_pct_clamps(value: float, expected: float) -> None:
    """Levels outside the fader range clamp rather than overflow."""
    assert api.db_to_pct(value) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "expected"), [(-10.0, -60.0), (0.0, -60.0), (100.0, 0.0), (140.0, 0.0)]
)
def test_pct_to_db_clamps(value: float, expected: float) -> None:
    """Positions outside 0..100 clamp rather than overflow."""
    assert api.pct_to_db(value) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw",
    [
        "http://console.local",
        "http://console.local/",
        "https://audio.example.org///",
        "  https://audio.example.org  ",
        "http://10.0.0.5:8080",
    ],
)
def test_normalize_base_url_accepts(raw: str) -> None:
    """Whitespace and trailing slashes are stripped; the rest is preserved."""
    assert not api.normalize_base_url(raw).endswith("/")
    assert api.normalize_base_url(raw).startswith(("http://", "https://"))


@pytest.mark.parametrize(
    "raw", ["", "   ", "console.local", "ftp://console.local", "https://"]
)
def test_normalize_base_url_rejects(raw: str) -> None:
    """Anything without an http/https scheme and a host is refused."""
    with pytest.raises(ValueError):
        api.normalize_base_url(raw)
