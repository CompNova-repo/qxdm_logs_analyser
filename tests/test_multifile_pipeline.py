"""End-to-end tests for the multi-file QXDM pipeline.

These tests synthesise ``.txt`` QXDM chunks in a temporary directory, run
the batch processor, and verify:

1.  Filenames are grouped into the right ``test_id`` by the regex
    classifier; unclassified files are surfaced separately.
2.  Chunks ingest in numeric ``chunk_seq`` order with a global ``sequence``
    column that continues across chunk boundaries.
3.  ``test_id`` / ``source_file`` / ``chunk_seq`` provenance lands on every
    row in ``events``, ``nas_events``, and ``rf_kpis``.
4.  Files are moved into ``processed/<test_id>/`` with a manifest, while
    corrupt/quarantined runs land in ``failed/<test_id>/``.
5.  ``qxdm_tool.py`` returns the right slices for ``list-tests``,
    ``anomalies --test-id``, ``rf-summary --test-id``, and ``window``.
6.  Re-running the batch on an empty input folder is a no-op (idempotent).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

# Make `tests/` runnable both as a package (`pytest tests/`) and as a top-
# level script (`python tests/test_multifile_pipeline.py`).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import batch_processor  # noqa: E402  - sys.path munging above
import indexer  # noqa: E402
import qxdm_tool  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _chunk_text(seq_index: int, marker: str) -> str:
    """One valid QXDM block carrying a payload string and a consecutive sequence."""
    return (
        f"2026 Sep 1 12:00:0{seq_index}.001 "
        f"[{seq_index + 100}] 0xB821 NR5G RRC DL Message\n"
        f"Payload String = Chunk {marker} marker\n\n"
    )


def _lte_rf_chunk(seq_index: int, marker: str, rsrp: int = -85) -> str:
    """An LTE NAS Signal Info block that triggers the ``lte_rf`` parser."""
    return (
        f"2026 Sep 1 12:00:0{seq_index}.002 "
        f"[{seq_index + 200}] 0x1544 LTE NAS Signal Info\n"
        f"nas_sig_info rsrp = {rsrp}\n"
        f"nas_sig_info rsrq = -8\n"
        f"nas_sig_info rssi = -60\n"
        f"nas_sig_info snr = 15\n\n"
    )


def _write_chunk(in_dir: Path, name: str, seq_index: int, marker: str) -> Path:
    """Write one ``.txt`` chunk combining an RRC event + an LTE RF event."""
    text = _chunk_text(seq_index, marker) + _lte_rf_chunk(seq_index, marker)
    p = in_dir / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. Filename classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected_id", "expected_seq", "reason"),
    [
        ("RUN42_session_001.txt", "RUN42", 1, "explicit_chunk_suffix"),
        ("RUN42_session_010.txt", "RUN42", 10, "explicit_chunk_suffix"),
        ("RUN9_part2.txt", "RUN9", 2, "explicit_chunk_suffix"),
        ("RUN9_chunk_5.txt", "RUN9", 5, "explicit_chunk_suffix"),
        ("RUN77_segment_3.txt", "RUN77", 3, "explicit_chunk_suffix"),
        ("RUN123_seq_7.txt", "RUN123", 7, "explicit_chunk_suffix"),
        ("RUN123_202609011200_001.txt", "RUN123", 1, "timestamp_then_seq"),
        ("RUN123_202609011200_2.txt", "RUN123", 2, "timestamp_then_seq"),
        ("RUN_TEST_3.txt", "RUN_TEST", 3, "underscore_seq"),
        ("plain_test_run.txt", "plain_test_run", 0, "fallback_stem"),
    ],
)
def test_classify_matches_filename_patterns(
    tmp_path: Path, filename: str, expected_id: str, expected_seq: int, reason: str,
) -> None:
    p = tmp_path / filename
    p.write_text("placeholder")
    ref = batch_processor._classify(p)
    assert ref is not None, f"classifier rejected {filename!r}"
    assert ref.test_id == expected_id
    assert ref.chunk_seq == expected_seq
    assert ref.reason == reason


def test_classify_rejects_empty_stem(tmp_path: Path) -> None:
    """A file literally named ``.txt`` (no stem, only extension) cannot be
    classified as a chunk."""
    p = tmp_path / ".txt"
    p.write_text("placeholder")
    assert batch_processor._classify(p) is None


def test_group_files_out_of_order_is_sorted_by_chunk_seq(tmp_path: Path) -> None:
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    _write_chunk(in_dir, "RUN42_session_003.txt", 3, "C")
    _write_chunk(in_dir, "RUN42_session_001.txt", 1, "A")
    _write_chunk(in_dir, "RUN42_session_002.txt", 2, "B")

    grouped, unclassified = batch_processor.group_files(sorted(in_dir.glob("*.txt")))
    assert "RUN42" in grouped
    refs = grouped["RUN42"]
    assert [r.path.name for r in refs] == [
        "RUN42_session_001.txt",
        "RUN42_session_002.txt",
        "RUN42_session_003.txt",
    ]
    assert unclassified == []


# ---------------------------------------------------------------------------
# 2. End-to-end multi-file ingest into SQLite
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_run(tmp_path: Path) -> dict[str, Path]:
    """Lay out two test runs (RUN42 with 2 chunks, RUN9 with 3 chunks) plus
    one file with a fallback-stem classification that holds junk content
    (and thus ends up in ``failed/`` after the per-file sanity check)."""
    in_dir = tmp_path / "incoming"
    in_dir.mkdir()
    db_path = tmp_path / "qxdm_indexed.db"

    _write_chunk(in_dir, "RUN42_session_001.txt", 1, "A")
    _write_chunk(in_dir, "RUN42_session_002.txt", 2, "B")
    _write_chunk(in_dir, "RUN9_chunk_1.txt", 1, "A")
    _write_chunk(in_dir, "RUN9_chunk_2.txt", 2, "B")
    _write_chunk(in_dir, "RUN9_chunk_3.txt", 3, "C")
    # Fallback stem — classifies with chunk_seq=0. The content is not
    # parseable QXDM so the per-file quarantine in the batch processor
    # routes it to ``failed/`` while the rest of the group succeeds.
    (in_dir / "incomplete-rollover.txt").write_text(
        "this is junk from a botched flush, not a QXDM block\n"
    )

    return {"in_dir": in_dir, "db_path": db_path}


def test_batch_processing_groups_and_processes_two_runs(
    fixture_run: dict[str, Path],
) -> None:
    summary = batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    by_id = {g.test_id: g for g in summary.groups}
    assert set(by_id) == {"RUN42", "RUN9", "incomplete-rollover"}
    assert by_id["RUN42"].status == "processed"
    assert by_id["RUN9"].status == "processed"
    # The fallback stem is non-QXDM at the parser level and gets quarantined.
    assert by_id["incomplete-rollover"].status == "failed"

    # All RUN42 / RUN9 files moved into processed/<test_id>/ + manifest.
    proc_run9 = fixture_run["in_dir"] / "processed" / "RUN9"
    proc_run42 = fixture_run["in_dir"] / "processed" / "RUN42"
    assert (proc_run42 / "manifest.json").is_file()
    assert (proc_run9 / "manifest.json").is_file()
    assert {p.name for p in proc_run42.iterdir()} == {
        "RUN42_session_001.txt",
        "RUN42_session_002.txt",
        "manifest.json",
    }
    assert {p.name for p in proc_run9.iterdir()} == {
        "RUN9_chunk_1.txt",
        "RUN9_chunk_2.txt",
        "RUN9_chunk_3.txt",
        "manifest.json",
    }
    # Quarantine folder holds the fallback stem.
    failed = fixture_run["in_dir"] / "failed" / "incomplete-rollover"
    assert failed.is_dir()
    assert (failed / "error.txt").is_file()
    assert not (fixture_run["in_dir"] / "incomplete-rollover.txt").exists()


def test_manifest_records_hashes_and_totals(fixture_run: dict[str, Path]) -> None:
    batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    manifest_path = fixture_run["in_dir"] / "processed" / "RUN9" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["test_id"] == "RUN9"
    assert len(manifest["chunks"]) == 3
    for entry in manifest["chunks"]:
        assert entry["sha256"]
        assert entry["size_bytes"] > 0
        assert entry["chunk_seq"] in (1, 2, 3)
    assert manifest["totals"]["files"] == 3
    # 1 RRC event + 1 LTE RF event per chunk = 3 events, 0 nas (synthetic
    # chunk doesn't include any NAS cause), 3 rf_kpis (one per chunk).
    assert manifest["totals"]["events"] == 3
    assert manifest["totals"]["rf_kpis"] == 3


def test_db_schema_has_provenance_columns(fixture_run: dict[str, Path]) -> None:
    batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    conn = sqlite3.connect(str(fixture_run["db_path"]))
    for table in ("events", "nas_events", "rf_kpis"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        assert {"test_id", "source_file", "chunk_seq"}.issubset(cols), table
    conn.close()


def test_global_sequence_continues_across_chunks(
    fixture_run: dict[str, Path],
) -> None:
    batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    conn = sqlite3.connect(str(fixture_run["db_path"]))
    seqs = [
        r[0]
        for r in conn.execute(
            "SELECT sequence FROM events ORDER BY sequence ASC"
        ).fetchall()
    ]
    # 2 (RUN42) + 3 (RUN9) = 5 contiguous sequence numbers starting at 0.
    assert seqs == [0, 1, 2, 3, 4]

    # chunk_seq restarts at 1 for each test_id.
    chunk_seqs = list(conn.execute(
        "SELECT test_id, chunk_seq FROM events ORDER BY sequence ASC"
    ).fetchall())
    assert chunk_seqs == [
        ("RUN42", 1), ("RUN42", 2),
        ("RUN9", 1), ("RUN9", 2), ("RUN9", 3),
    ]
    conn.close()


# ---------------------------------------------------------------------------
# 3. CLI / qxdm_tool.py filtering
# ---------------------------------------------------------------------------


def test_list_tests_summarises_chunk_and_record_counts(
    fixture_run: dict[str, Path],
) -> None:
    batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    rows = qxdm_tool.list_tests(str(fixture_run["db_path"]))
    by_id = {r["test_id"]: r for r in rows}
    assert by_id["RUN42"]["chunks"] == 2
    assert by_id["RUN42"]["event_count"] == 2
    assert by_id["RUN42"]["first_chunk_seq"] == 1
    assert by_id["RUN42"]["last_chunk_seq"] == 2
    assert by_id["RUN9"]["chunks"] == 3
    assert by_id["RUN9"]["event_count"] == 3


def test_rf_summary_filtered_by_test_id(
    fixture_run: dict[str, Path],
) -> None:
    batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    all_summary = qxdm_tool.get_rf_summary(str(fixture_run["db_path"]))
    per_test = qxdm_tool.get_rf_summary(
        str(fixture_run["db_path"]), test_id="RUN42",
    )
    # Per-test count must be ≤ all-tests count.
    assert per_test["test_id"] == "RUN42"
    assert per_test["records"] <= all_summary["records"]
    assert per_test["records"] == 2  # one LTE RF block per chunk


def test_anomalies_filtered_by_test_id(
    fixture_run: dict[str, Path],
) -> None:
    batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    rows = qxdm_tool.query_anomalies(
        str(fixture_run["db_path"]), test_id="RUN9",
    )
    assert rows == []  # synthetic log has no RRC release trigger
    # No filter = same empty result for this fixture; nothing to assert
    # beyond not crashing.


def test_events_filter_includes_provenance(
    fixture_run: dict[str, Path],
) -> None:
    batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    run42 = qxdm_tool.list_events(
        str(fixture_run["db_path"]), 50, test_id="RUN42",
    )
    assert len(run42) == 2
    assert all(r["test_id"] == "RUN42" for r in run42)
    assert {r["source_file"] for r in run42} == {
        "RUN42_session_001.txt", "RUN42_session_002.txt",
    }
    assert {r["chunk_seq"] for r in run42} == {1, 2}


def test_window_slices_across_chunk_boundary(
    fixture_run: dict[str, Path],
) -> None:
    """Pick a timestamp inside RUN9's second chunk; window must include
    events before/after regardless of which file they live in."""
    batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    snippet = qxdm_tool.get_context_window(
        str(fixture_run["db_path"]),
        "2026 Sep 1 12:00:02.001",
        window_count=5,
        test_id="RUN9",
    )
    # RUN9_chunk_2's payload appears in the slice even though the search
    # anchor was the prior chunk's tail.
    assert "Chunk B marker" in snippet
    # Cross-chunk continuity shows the third chunk's payload too.
    assert "Chunk C marker" in snippet
    # Each chunk's source_file is annotated in the slice so the agent can
    # trace a particular block back to its origin file.
    assert "RUN9_chunk_1.txt" in snippet
    assert "RUN9_chunk_3.txt" in snippet


# ---------------------------------------------------------------------------
# 4. Error isolation
# ---------------------------------------------------------------------------


def test_invalid_file_quarantined_without_corrupting_db(
    fixture_run: dict[str, Path],
) -> None:
    """Force a parser-level failure by writing junk to a chunk; the
    orchestrator must move it to ``failed/`` and leave the other test_id
    intact in the DB."""
    bad = fixture_run["in_dir"] / "RUN9_chunk_002.txt"
    bad.write_text("this is not QXDM at all\n")

    summary = batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    by_id = {g.test_id: g for g in summary.groups}
    assert by_id["RUN9"].status == "failed"
    # RUN42 should still be ingested cleanly.
    assert by_id["RUN42"].status == "processed"
    # RUN42 rows are present in the DB.
    conn = sqlite3.connect(str(fixture_run["db_path"]))
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE test_id = 'RUN42'"
    ).fetchone()[0]
    assert n >= 2
    conn.close()
    # The corrupted chunk now sits under failed/RUN9/.
    failed = fixture_run["in_dir"] / "failed" / "RUN9"
    assert any(p.name.endswith(".txt") for p in failed.iterdir())
    assert (failed / "error.txt").is_file()


# ---------------------------------------------------------------------------
# 5. Direct multi-file parse_files API
# ---------------------------------------------------------------------------


def test_parse_files_surfaces_provenance_and_continuity(tmp_path: Path) -> None:
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    _write_chunk(in_dir, "RUN55_part_1.txt", 1, "A")
    _write_chunk(in_dir, "RUN55_part_2.txt", 2, "B")
    _write_chunk(in_dir, "RUN55_part_3.txt", 3, "C")

    db_path = tmp_path / "out.db"
    result = indexer.parse_files(
        files=[
            str(in_dir / "RUN55_part_1.txt"),
            str(in_dir / "RUN55_part_2.txt"),
            str(in_dir / "RUN55_part_3.txt"),
        ],
        db_path=str(db_path),
        test_id="RUN55",
    )
    assert result["test_id"] == "RUN55"
    assert result["file_count"] == 3
    assert result["totals"]["events"] == 3

    conn = sqlite3.connect(str(db_path))
    rows = list(conn.execute(
        "SELECT chunk_seq, source_file FROM events ORDER BY sequence ASC"
    ).fetchall())
    # chunk_seq is per-file provenance, sequence is global.
    assert rows == [
        (1, "RUN55_part_1.txt"),
        (2, "RUN55_part_2.txt"),
        (3, "RUN55_part_3.txt"),
    ]
    conn.close()


def test_parse_files_rejects_empty_input(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one input file"):
        indexer.parse_files(files=[], db_path=str(tmp_path / "x.db"))


def test_parse_files_chunk_sequences_length_must_match(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text(_chunk_text(1, "A"))
    with pytest.raises(ValueError, match="chunk_sequences length"):
        indexer.parse_files(
            files=[str(p)], db_path=str(tmp_path / "x.db"),
            chunk_sequences=[1, 2],
        )


# ---------------------------------------------------------------------------
# 6. Idempotency / rerun
# ---------------------------------------------------------------------------


def test_rerun_on_empty_input_is_a_noop(tmp_path: Path) -> None:
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    db_path = tmp_path / "out.db"
    summary = batch_processor.process_directory(
        input_dir=in_dir, db_path=db_path, stability_window=0.0,
    )
    assert summary.groups == []
    assert summary.unclassified == []
    # No DB is created when there is nothing to process.
    assert not db_path.exists()


def test_rerun_after_processing_does_not_double_ingest(
    fixture_run: dict[str, Path],
) -> None:
    """A second run over an already-cleared input directory is a no-op;
    counts in the DB must not change."""
    batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    conn = sqlite3.connect(str(fixture_run["db_path"]))
    n_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()

    summary = batch_processor.process_directory(
        input_dir=fixture_run["in_dir"],
        db_path=fixture_run["db_path"],
        stability_window=0.0,
    )
    assert summary.groups == []  # input was emptied on the first run

    conn = sqlite3.connect(str(fixture_run["db_path"]))
    n_after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    assert n_after == n_before


if __name__ == "__main__":
    # Allow running as ``python tests/test_multifile_pipeline.py`` for
    # ad-hoc debugging without pytest installed.
    import pytest as _pt
    raise SystemExit(_pt.main([__file__, "-q"]))
