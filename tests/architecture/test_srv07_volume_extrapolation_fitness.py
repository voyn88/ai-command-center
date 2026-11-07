"""VOYN-W0-AICC-SRV-07-VOLUME-EXTRAPOLATED: migration volume is extrapolation.

The ~1.22M-row / ~6.5-minute figures that circulated for the SQLite→PostgreSQL
data migration were extrapolated from one table in a synthetic fixture, not
measured on production. The live database's sixteen domain tables are empty
(137 rows across the file). This gate keeps that correction on the decision
surface for SRV-07 / SRV-09 planning — dropping the caveat would let capacity
estimates be read as measured production volume again.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DECISIONS = REPO_ROOT / "DECISIONS.md"
FOUNDATION = REPO_ROOT / "docs/postgres-foundation.md"
SCHEMA_MAP = REPO_ROOT / "docs/srv01b-schema-map.md"

_DR_ID = "DR-SRV07-VOLUME-EXTRAPOLATION-001"


def test_decision_log_records_volume_as_extrapolation():
    text = DECISIONS.read_text(encoding="utf-8")
    assert _DR_ID in text, f"{_DR_ID} must stay in DECISIONS.md"
    assert "extrapolation" in text.lower()
    assert "VOYN-W0-AICC-SRV-07" in text
    assert "VOYN-W0-AICC-SRV-09" in text
    assert "1.22" in text
    assert "synthetic fixture" in text.lower() or "synthetic" in text.lower()
    # Must not present the figures as a production measurement.
    decision_row = next(
        line for line in text.splitlines() if _DR_ID in line
    )
    assert "not a measurement" in decision_row.lower()


def test_postgres_foundation_flags_volume_unmeasured():
    text = FOUNDATION.read_text(encoding="utf-8")
    assert "Volume is unmeasured" in text
    assert _DR_ID in text
    assert "extrapolation" in text.lower()
    assert "VOYN-W0-AICC-SRV-07" in text
    assert "VOYN-W0-AICC-SRV-09" in text


def test_srv01b_schema_map_does_not_cite_volume_as_measured():
    """The SRV-07 schema map must not reintroduce the figures as fact."""
    text = SCHEMA_MAP.read_text(encoding="utf-8")
    # If the map mentions the circulated figures, they must be labeled.
    if "1.22" in text or "6.5" in text:
        assert "extrapolat" in text.lower(), (
            "docs/srv01b-schema-map.md cites the circulated volume figures "
            "without labeling them as extrapolation"
        )
    assert _DR_ID in text or "Volume is unmeasured" in text or (
        "extrapolat" in text.lower()
    ), (
        "docs/srv01b-schema-map.md must point planners at the volume-"
        f"extrapolation caveat ({_DR_ID})"
    )
