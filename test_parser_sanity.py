"""Sanity tests for the parsed KPI tables.

These tests don't read the 50 MB QXDM log — they query the SQLite index the
indexer already produced and check that the parsed values fall within their
3GPP-defined bounds. Use these after any config change to confirm the
extraction didn't regress:

    pytest -q test_parser_sanity.py
    # or, against a custom DB:
    python3 test_parser_sanity.py path/to/qxdm_indexed.db
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Tests default to ``qxdm_indexed.db`` next to this file; override with the
# ``QXDM_DB`` env var or by editing DEFAULT_DB.
DEFAULT_DB = os.environ.get("QXDM_DB", "qxdm_indexed.db")


def _connect() -> sqlite3.Connection:
    path = Path(DEFAULT_DB)
    if not path.exists():
        pytest.skip(f"SQLite index not found: {path}. Run: python3 indexer.py <log>")
    return sqlite3.connect(str(path))


def test_rsrp_within_3gpp_bounds() -> None:
    """3GPP TS 38.133 Table 10.1.6.1-1: SS-RSRP valid range -156..-31 dBm."""
    conn = _connect()
    rows = conn.execute(
        "SELECT COUNT(*) FROM rf_kpis "
        "WHERE rsrp IS NOT NULL AND (rsrp < -156.0 OR rsrp > -31.0)"
    ).fetchone()
    conn.close()
    assert rows[0] == 0, (
        f"{rows[0]} RSRP samples fell outside the 3GPP valid range "
        "(-156..-31 dBm) — indexer is probably parsing the wrong field."
    )


def test_rsrq_within_reasonable_bounds() -> None:
    """SS-RSRQ valid range -30..10 dB per 3GPP TS 38.215."""
    conn = _connect()
    rows = conn.execute(
        "SELECT COUNT(*) FROM rf_kpis "
        "WHERE rsrq IS NOT NULL AND (rsrq < -30.0 OR rsrq > 10.0)"
    ).fetchone()
    conn.close()
    assert rows[0] == 0, f"{rows[0]} RSRQ samples fell outside [-30, 10] dB"


def test_rssi_within_bounds() -> None:
    """LTE RSSI in -140..0 dBm. Flag anything wildly out of range."""
    conn = _connect()
    rows = conn.execute(
        "SELECT COUNT(*) FROM rf_kpis "
        "WHERE rssi IS NOT NULL AND (rssi < -140.0 OR rssi > 0.0)"
    ).fetchone()
    conn.close()
    assert rows[0] == 0, f"{rows[0]} RSSI samples fell outside [-140, 0] dBm"


def test_snr_within_bounds() -> None:
    """SNR for LTE typically -20..60 dB; flag anything outside."""
    conn = _connect()
    rows = conn.execute(
        "SELECT COUNT(*) FROM rf_kpis "
        "WHERE snr IS NOT NULL AND (snr < -20.0 OR snr > 60.0)"
    ).fetchone()
    conn.close()
    assert rows[0] == 0, f"{rows[0]} SNR samples fell outside [-20, 60] dB"


def test_nas_cause_code_in_byte_range() -> None:
    """NAS cause codes are 5-bit / 7-bit values — anything over 255 is junk."""
    conn = _connect()
    rows = conn.execute(
        "SELECT COUNT(*) FROM nas_events "
        "WHERE cause_code IS NOT NULL AND (cause_code < 0 OR cause_code > 255)"
    ).fetchone()
    conn.close()
    assert rows[0] == 0, f"{rows[0]} NAS cause codes are outside [0, 255]"


def test_nas_timers_within_backoff_range() -> None:
    """T3346 / T3502 are operator-controlled; flag anything >2h (7200 s)."""
    conn = _connect()
    rows = conn.execute(
        "SELECT COUNT(*) FROM nas_events "
        "WHERE (t3346_timer IS NOT NULL AND (t3346_timer < 0 OR t3346_timer > 7200)) "
        "   OR (t3502_timer IS NOT NULL AND (t3502_timer < 0 OR t3502_timer > 7200))"
    ).fetchone()
    conn.close()
    assert rows[0] == 0, f"{rows[0]} T3346/T3502 timers fell outside [0, 7200] s"


def test_rf_table_populated() -> None:
    """Smoke check: the indexer found at least one RF measurement."""
    conn = _connect()
    n = conn.execute("SELECT COUNT(*) FROM rf_kpis").fetchone()[0]
    conn.close()
    assert n > 0, "rf_kpis table is empty — indexer failed to extract any RF KPIs"


def test_nas_table_populated() -> None:
    """Smoke check: the indexer found at least one NAS event."""
    conn = _connect()
    n = conn.execute("SELECT COUNT(*) FROM nas_events").fetchone()[0]
    conn.close()
    assert n > 0, "nas_events table is empty — indexer failed to extract NAS events"


def test_parser_health_consistent() -> None:
    """Every parser's parsed+failed counts must add up to its matched count."""
    conn = _connect()
    rows = conn.execute(
        "SELECT parser_name, matched, parsed, failed, invalid_value "
        "FROM parser_health"
    ).fetchall()
    conn.close()
    for name, matched, parsed, failed, invalid_value in rows:
        assert matched == parsed + failed, (
            f"{name}: matched={matched} but parsed+fails={parsed + failed}"
        )
        assert invalid_value <= parsed, (
            f"{name}: more invalid_value samples ({invalid_value}) than parsed "
            f"({parsed}) — invalid_value should be a subset of parsed."
        )


def test_unknown_samples_match_unknown_codes() -> None:
    """Every unknown_samples row must reference an unknown_msg_codes row."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM unknown_samples s
        WHERE NOT EXISTS (
            SELECT 1 FROM unknown_msg_codes c WHERE c.msg_code = s.msg_code
        )
    """)
    orphan_samples = cur.fetchone()[0]
    conn.close()
    assert orphan_samples == 0, (
        f"{orphan_samples} unknown_samples rows reference no unknown_msg_codes "
        "row — sample/code tables are out of sync"
    )


# ---------------------------------------------------------------------------
# CLI entrypoint: run tests against an arbitrary DB without pytest
# ---------------------------------------------------------------------------


def _run_cli(db_path: str) -> int:
    os.environ["QXDM_DB"] = db_path
    args = [__file__, "-q"]
    return pytest.main(args)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    raise SystemExit(_run_cli(target))