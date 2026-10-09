"""Edge-case tests for multi-file ingestion: missing, late, partial and
re-delivered chunks.

Each test asserts what SHOULD happen. A failing test here marks a real gap
in the current pipeline, not a broken test. Run with:

    python3 -m pytest tests/test_multifile_edge_cases.py -v
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
for p in (ROOT, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import batch_processor  # noqa: E402
from test_multifile_pipeline import _write_chunk  # noqa: E402

TEST_ID = "RUN123"
TABLES = ("events", "nas_events", "rf_kpis")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Path]:
    in_dir = tmp_path / "incoming_logs"
    in_dir.mkdir()
    return {"in_dir": in_dir, "db": tmp_path / "qxdm_indexed.db"}


def _write_run(in_dir: Path, seqs: list[int], test_id: str = TEST_ID) -> None:
    for seq in seqs:
        _write_chunk(in_dir, f"{test_id}_chunk_{seq:03d}.txt", seq, f"c{seq}")


def _process(env: dict[str, Path], **kw):
    kw.setdefault("stability_window", 0.0)
    summary = batch_processor.process_directory(
        input_dir=env["in_dir"], db_path=env["db"], **kw
    )
    return {g.test_id: g for g in summary.groups}


def _db_chunks(db: Path, test_id: str = TEST_ID) -> dict[str, int]:
    """source_file -> chunk_seq as stored in the DB."""
    if not db.exists():
        return {}
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT DISTINCT source_file, chunk_seq FROM events WHERE test_id = ?",
            (test_id,),
        ).fetchall()
    return dict(rows)


def _db_row_count(db: Path, test_id: str = TEST_ID) -> int:
    if not db.exists():
        return 0
    with sqlite3.connect(db) as conn:
        return sum(
            conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE test_id = ?", (test_id,)  # noqa: S608
            ).fetchone()[0]
            for t in TABLES
        )


# ---------------------------------------------------------------------------
# 1. Baseline: things that should already work
# ---------------------------------------------------------------------------


def test_complete_run_is_processed_with_correct_chunk_numbers(env) -> None:
    _write_run(env["in_dir"], [1, 2, 3, 4])
    result = _process(env)
    assert result[TEST_ID].status == "processed"
    assert _db_chunks(env["db"]) == {
        f"{TEST_ID}_chunk_{s:03d}.txt": s for s in (1, 2, 3, 4)
    }


def test_out_of_order_arrival_is_ingested_in_chunk_order(env) -> None:
    _write_run(env["in_dir"], [3, 1, 4, 2])
    _process(env)
    with sqlite3.connect(env["db"]) as conn:
        order = [
            r[0]
            for r in conn.execute(
                "SELECT source_file FROM events WHERE test_id = ? ORDER BY sequence",
                (TEST_ID,),
            )
        ]
    first_seen = list(dict.fromkeys(order))
    assert first_seen == [f"{TEST_ID}_chunk_{s:03d}.txt" for s in (1, 2, 3, 4)]


# ---------------------------------------------------------------------------
# 2. Chunk numbering
# ---------------------------------------------------------------------------


def test_db_keeps_real_chunk_numbers_from_filenames(env) -> None:
    """Files 005 and 006 must be stored as chunk 5 and 6, not renumbered 1, 2."""
    _write_run(env["in_dir"], [5, 6])
    _process(env)
    assert _db_chunks(env["db"]) == {
        f"{TEST_ID}_chunk_005.txt": 5,
        f"{TEST_ID}_chunk_006.txt": 6,
    }


# ---------------------------------------------------------------------------
# 3. Missing chunks
# ---------------------------------------------------------------------------


def test_missing_middle_chunk_is_reported_as_missing(env) -> None:
    """Got 001, 002, 004. The error should name chunk 3 as missing."""
    _write_run(env["in_dir"], [1, 2, 4])
    result = _process(env)
    assert result[TEST_ID].status != "processed"
    error = (result[TEST_ID].error or "").lower()
    assert "missing" in error and "3" in error, f"misleading error: {error!r}"


def test_missing_middle_chunk_leaves_no_rows_in_db(env) -> None:
    """A run that is not processed must not be queryable by the agent."""
    _write_run(env["in_dir"], [1, 2, 4])
    result = _process(env)
    assert result[TEST_ID].status != "processed"
    assert _db_row_count(env["db"]) == 0


@pytest.mark.xfail(
    reason="Needs an expected chunk count (e.g. capture-side manifest); "
    "nothing in the current design can know chunk 004 should exist.",
    strict=True,
)
def test_missing_last_chunk_is_detected(env) -> None:
    """Real run had 4 chunks, only 001-003 arrived. Undetectable today."""
    _write_run(env["in_dir"], [1, 2, 3])
    result = _process(env)
    assert result[TEST_ID].status != "processed"


# ---------------------------------------------------------------------------
# 4. Failed runs must not leak partial data
# ---------------------------------------------------------------------------


def test_failed_run_leaves_no_rows_in_db(env) -> None:
    """Chunk 2 is corrupt. Whole run goes to failed/, so chunk 1's rows
    must not stay behind in the DB."""
    _write_run(env["in_dir"], [1, 3])
    (env["in_dir"] / f"{TEST_ID}_chunk_002.txt").write_text("not QXDM\n")
    result = _process(env)
    assert result[TEST_ID].status == "failed"
    assert _db_row_count(env["db"]) == 0


# ---------------------------------------------------------------------------
# 5. A chunk still being copied
# ---------------------------------------------------------------------------


def test_still_copying_chunk_holds_whole_group(env) -> None:
    """Chunk 3 keeps growing during processing. Nothing from the run
    should be indexed or moved; it should wait for the next cycle."""
    _write_run(env["in_dir"], [1, 2, 4])
    growing = env["in_dir"] / f"{TEST_ID}_chunk_003.txt"
    growing.write_text("")
    stop = threading.Event()

    def keep_writing() -> None:
        while not stop.is_set():
            with growing.open("a") as f:
                f.write("x" * 100 + "\n")
            time.sleep(0.05)

    t = threading.Thread(target=keep_writing, daemon=True)
    t.start()
    try:
        result = _process(env, stability_window=0.3, max_wait=0.8)
    finally:
        stop.set()
        t.join()

    assert result[TEST_ID].status not in ("processed", "failed")
    assert _db_row_count(env["db"]) == 0
    remaining = {p.name for p in env["in_dir"].glob("*.txt")}
    assert remaining == {f"{TEST_ID}_chunk_{s:03d}.txt" for s in (1, 2, 3, 4)}


# ---------------------------------------------------------------------------
# 6. Re-delivery and late arrival
# ---------------------------------------------------------------------------


def test_same_run_delivered_twice_does_not_duplicate_rows(env, tmp_path) -> None:
    _write_run(env["in_dir"], [1, 2])
    backup = tmp_path / "backup"
    shutil.copytree(env["in_dir"], backup)
    _process(env)
    first = _db_row_count(env["db"])
    for f in backup.glob("*.txt"):
        shutil.copy(f, env["in_dir"] / f.name)
    _process(env)
    assert _db_row_count(env["db"]) == first


def test_late_chunk_joins_existing_run_with_correct_number(env) -> None:
    """001-003 processed first, 004 arrives later on its own."""
    _write_run(env["in_dir"], [1, 2, 3])
    _process(env)
    _write_run(env["in_dir"], [4])
    _process(env)
    assert _db_chunks(env["db"]).get(f"{TEST_ID}_chunk_004.txt") == 4
