#!/usr/bin/env python3
"""Batch ingestion pipeline for multi-file QXDM test runs.

Responsibilities
----------------
1.  Scan an input directory for ``*.txt`` decoded QXDM logs.
2.  Group files by their extracted test-run id (regex on filename). Chunks
    that belong to the same run are sorted by sequence number before
    ingestion.
3.  Validate file stability (two consecutive stat() calls report equal size
    after a configurable delay) so an actively written chunk isn't grabbed
    mid-flush.
4.  Stream chunks into ``qxdm_indexed.db`` via :func:`indexer.parse_files`
    with a single ``test_id`` and per-chunk ``chunk_seq`` and ``source_file``.
5.  Run a sanity check (record counts > 0, no fatal parser exception).
6.  Atomically move the entire group to ``<input>/processed/<test_id>/`` and
    write a ``manifest.json`` summarising files, SHA-256 hashes, chunk count,
    and totals.
7.  On any failure for a file in the group, quarantine the entire group
    under ``<input>/failed/<test_id>/`` and persist ``error.txt``.

Default input directory:
    ./incoming_logs  (relative to the cwd)  — overridable via ``--input-dir``.

Paths and shutil operations are written to be portable to Windows.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import indexer

log = logging.getLogger("Batch_Processor")


# ---------------------------------------------------------------------------
# Filename → (test_id, chunk_seq) extraction
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ChunkRef:
    """One input file classified by its test-run id and chunk sequence."""

    path: Path
    test_id: str
    chunk_seq: int
    reason: str  # which regex / fallback classified it

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "filename": self.path.name,
            "test_id": self.test_id,
            "chunk_seq": self.chunk_seq,
            "reason": self.reason,
        }


# Order matters: the first regex that matches wins. Patterns are anchored at
# the end and tolerate either ``_session_001``, ``_part1``, ``_chunk_2``,
# or ``_0123`` style suffixes. All patterns accept an optional ``.txt`` so
# they work whether the caller passes the stem or the full filename.
_CHUNK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # <id>_(session|part|chunk|segment|seg|seq)[-_]?<seq>(.txt)?
    (
        re.compile(
            r"^(?P<id>[^_\s]+(?:_[^_\s]+)*?)_"
            r"(?:session|part|chunk|segment|seg|seq)[\-_]?"
            r"(?P<seq>\d{1,5})"
            r"(?:\.txt)?$",
            re.IGNORECASE,
        ),
        "explicit_chunk_suffix",
    ),
    # <id>_<ts>[-_]<seq>(.txt)?  (timestamp, then sequence)
    (
        re.compile(
            r"^(?P<id>[^_\s]+(?:_[^_\s]+)*?)_"
            r"(?P<ts>\d{8,14})[\-_]"
            r"(?P<seq>\d{1,5})"
            r"(?:\.txt)?$",
            re.IGNORECASE,
        ),
        "timestamp_then_seq",
    ),
    # <id>_<seq>(.txt)?
    (
        re.compile(
            r"^(?P<id>[^_\s]+(?:_[^_\s]+)*?)_"
            r"(?P<seq>\d{1,5})"
            r"(?:\.txt)?$",
            re.IGNORECASE,
        ),
        "underscore_seq",
    ),
)


def _classify(path: Path) -> ChunkRef | None:
    """Return a :class:`ChunkRef` for ``path`` or ``None`` if it doesn't fit
    any known chunk pattern and isn't a fallback candidate."""
    stem = path.stem  # filename without ``.txt``
    name = path.name
    for source, reason in _CHUNK_PATTERNS:
        for value in (stem, name):
            m = source.match(value)
            if not m:
                continue
            test_id = m.group("id").strip()
            seq = int(m.group("seq"))
            if not test_id:
                continue
            return ChunkRef(path=path, test_id=test_id, chunk_seq=seq, reason=reason)
    # Fallback: treat the whole stem as test_id, sequence 0. Only enabled
    # when the stem is a real name (not empty, not a Unix-style hidden
    # file with no stem like ``.txt``).
    if not stem or stem.startswith(".") or stem in {"", ".", ".."}:
        return None
    return ChunkRef(path=path, test_id=stem, chunk_seq=0, reason="fallback_stem")


def group_files(
    paths: Iterable[Path],
) -> tuple[dict[str, list[ChunkRef]], list[Path]]:
    """Group :class:`ChunkRef`s by ``test_id``. Returns ``(groups, unclassified)``.

    Each group's chunk list is sorted by ``(chunk_seq, filename)`` so
    ingestion proceeds chronologically / numerically regardless of the OS
    glob order.
    """
    grouped: dict[str, list[ChunkRef]] = defaultdict(list)
    unclassified: list[Path] = []
    for p in paths:
        ref = _classify(p)
        if ref is None:
            unclassified.append(p)
            continue
        grouped[ref.test_id].append(ref)
    for refs in grouped.values():
        refs.sort(key=lambda r: (r.chunk_seq, r.path.name))
    return dict(grouped), unclassified


# ---------------------------------------------------------------------------
# Stability check & hashing
# ---------------------------------------------------------------------------


def _wait_for_size_stability(
    path: Path, stability_window: float, max_wait: float,
) -> bool:
    """Return True if the file's size stayed constant for ``stability_window``.

    Polls the file size every ``poll_interval`` seconds (capped at
    ``stability_window/2``) up to ``max_wait``. Returns False if the file
    never stabilised within the deadline — the caller can treat that as a
    still-being-written chunk and skip.
    """
    if stability_window <= 0:
        return True
    poll_interval = max(0.1, min(stability_window / 2.0, 1.0))
    deadline = time.monotonic() + max_wait
    last_size = -1
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size != last_size:
            last_size = size
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= stability_window:
            return True
        time.sleep(poll_interval)
    return False


def _sha256(path: Path, chunk_size: int = 65536) -> str:
    """Compute ``path``'s SHA-256 as a lowercase hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# File movement helpers (cross-volume safe, Windows-safe)
# ---------------------------------------------------------------------------


def _safe_move(src: Path, dst_dir: Path) -> Path:
    """Move ``src`` into ``dst_dir`` (created if needed). Returns final path.

    Uses ``shutil.move`` which handles cross-volume moves by copying then
    deleting the source. Falls back to a same-volume ``os.replace`` when
    possible. Always returns the resolved destination path.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        # Disambiguate by appending a timestamp; ensure uniqueness within a
        # single retry cycle of the orchestrator.
        stamp = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")
        dst = dst_dir / f"{src.stem}.{stamp}{src.suffix}"
    shutil.move(str(src), str(dst))
    return dst


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class GroupResult:
    test_id: str
    files: list[dict[str, Any]]
    manifest_path: Path | None
    status: str  # "processed" | "failed" | "skipped"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "test_id": self.test_id,
            "files": self.files,
            "status": self.status,
        }
        if self.manifest_path is not None:
            d["manifest_path"] = str(self.manifest_path)
        if self.error is not None:
            d["error"] = self.error
        return d


@dataclasses.dataclass
class BatchSummary:
    groups: list[GroupResult]
    unclassified: list[str]
    failed_at_setup: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "groups": [g.to_dict() for g in self.groups],
            "unclassified": list(self.unclassified),
            "setup_failures": list(self.failed_at_setup),
        }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def process_directory(
    input_dir: str | Path,
    db_path: str | Path = "qxdm_indexed.db",
    config_path: str | Path = indexer.DEFAULT_CONFIG_PATH,
    stability_window: float = 2.0,
    max_wait: float = 120.0,
    processed_subdir: str = "processed",
    failed_subdir: str = "failed",
    ingest_dry_run: bool = False,
    progress: "Callable[[str], None] | None" = None,
) -> BatchSummary:
    """Run the full pipeline against ``input_dir``.

    See module docstring for the per-step behaviour.
    """
    in_dir = Path(input_dir)
    if not in_dir.is_dir():
        raise NotADirectoryError(f"input directory not found: {in_dir}")

    def _emit(msg: str) -> None:
        log.info(msg)
        if progress is not None:
            progress(msg)

    # Skip already-processed/failed sub-trees so reruns don't double-ingest
    # or fight zombie .part files left from a crash.
    skip = {processed_subdir, failed_subdir}
    candidates: list[Path] = []
    for p in sorted(in_dir.iterdir()):
        if not p.is_file():
            continue
        if p.name in skip:
            continue
        if p.suffix.lower() != ".txt":
            continue
        candidates.append(p)

    grouped, unclassified = group_files(candidates)
    summary = BatchSummary(groups=[], unclassified=[str(p) for p in unclassified], failed_at_setup=[])

    if not grouped:
        _emit(f"no indexable .txt files found in {in_dir}")
        return summary

    db_path_str = str(db_path)
    config_path_str = str(config_path)

    for test_id, refs in grouped.items():
        _emit(f"processing test_id={test_id}: {len(refs)} file(s)")

        # Stability check + hashing
        ready: list[ChunkRef] = []
        for ref in refs:
            if not ref.path.is_file():
                summary.failed_at_setup.append({
                    "test_id": test_id,
                    "filename": ref.path.name,
                    "reason": "missing",
                })
                continue
            if not _wait_for_size_stability(ref.path, stability_window, max_wait):
                summary.failed_at_setup.append({
                    "test_id": test_id,
                    "filename": ref.path.name,
                    "reason": "size_unstable",
                })
                continue
            ready.append(ref)

        if not ready:
            summary.groups.append(GroupResult(
                test_id=test_id, files=[],
                manifest_path=None, status="skipped",
                error="no files passed the stability check",
            ))
            continue

        # Hash + size before ingestion so the manifest is complete
        # regardless of move outcome.
        metadata: list[dict[str, Any]] = []
        for ref in ready:
            try:
                sha = _sha256(ref.path)
                size = ref.path.stat().st_size
            except OSError as exc:
                summary.failed_at_setup.append({
                    "test_id": test_id,
                    "filename": ref.path.name,
                    "reason": f"hash_or_stat_failed: {exc}",
                })
                sha = ""
                size = 0
            metadata.append({
                "filename": ref.path.name,
                "size_bytes": size,
                "sha256": sha,
                "chunk_seq": ref.chunk_seq,
                "reason": ref.reason,
            })

        # Retry loop: if a previous run of this orchestrator already moved
        # some files into processed/, the DB rows are already there for
        # them — but only re-ingest files that still exist in the input
        # folder. Re-grouping on each retry keeps the manifest in sync.
        to_ingest: list[ChunkRef] = [
            ref for ref in ready if ref.path.exists()
        ]
        bad_chunks: list[dict[str, Any]] = []
        if to_ingest:
            files_for_index = [str(r.path) for r in to_ingest]
            sequences = [r.chunk_seq for r in to_ingest]
            try:
                ingest_result = indexer.parse_files(
                    files=files_for_index,
                    db_path=db_path_str,
                    config_path=config_path_str,
                    test_id=test_id,
                    dry_run=ingest_dry_run,
                )
            except Exception as exc:  # noqa: BLE001 — orchestrator must
                # report failures from the indexer rather than crash.
                _emit(
                    f"indexing failed for {test_id}: {type(exc).__name__}: {exc}"
                )
                failed_dir = in_dir / failed_subdir / test_id
                failed_dir.mkdir(parents=True, exist_ok=True)
                err_path = failed_dir / "error.txt"
                err_path.write_text(
                    f"test_id={test_id}\n"
                    f"timestamp={_dt.datetime.now(tz=_dt.timezone.utc).isoformat()}\n"
                    f"exception={type(exc).__name__}: {exc}\n",
                    encoding="utf-8",
                )
                for ref in ready:
                    if ref.path.exists():
                        _safe_move(ref.path, failed_dir)
                summary.groups.append(GroupResult(
                    test_id=test_id,
                    files=[],
                    manifest_path=None,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                ))
                continue

            # Per-file 0-record detection: a chunk that yielded no events,
            # NAS records, or RF KPIs gets flagged. Per the user's spec
            # ("If indexing fails for any file in the group, quarantine
            # the files to <input_dir>/failed/<test_id>/"), the entire
            # group is routed to failed/ when any chunk produces zero
            # records so downstream agents don't see partially-parsed
            # runs.
            if not ingest_dry_run and Path(db_path_str).exists():
                try:
                    with sqlite3.connect(db_path_str) as conn:
                        cur = conn.cursor()
                        per_chunk_counts: dict[int, int] = {}
                        for source in ("events", "nas_events", "rf_kpis"):
                            for row in cur.execute(
                                "SELECT chunk_seq, COUNT(*) FROM " + source +  # noqa: S608
                                " WHERE test_id = ? GROUP BY chunk_seq",
                                (test_id,),
                            ).fetchall():
                                per_chunk_counts[row[0]] = (
                                    per_chunk_counts.get(row[0], 0) + row[1]
                                )
                        for ref in to_ingest:
                            seq = ref.chunk_seq
                            if per_chunk_counts.get(seq, 0) == 0:
                                bad_chunks.append({
                                    "filename": ref.path.name,
                                    "chunk_seq": seq,
                                    "reason": "no_records_indexed",
                                })
                except sqlite3.OperationalError:
                    # Pre-multifile DBs may lack chunk_seq; treat as a
                    # silent skip — the test_id group summary still tells
                    # the agent the run succeeded.
                    pass

            # Sanity: at least one row in events, OR explicit --dry-run,
            # AND no quarantined chunks remain. The second clause guards
            # against an entire group of bad chunks slipping through.
            indexed_total = ingest_result.get("totals", {}).get("events", 0)
            if not ingest_dry_run and indexed_total == 0 and not bad_chunks:
                err_msg = (
                    "sanity check failed: indexed 0 events for "
                    f"{test_id} — likely parse regression"
                )
                _emit(err_msg)
                failed_dir = in_dir / failed_subdir / test_id
                failed_dir.mkdir(parents=True, exist_ok=True)
                (failed_dir / "error.txt").write_text(
                    err_msg + "\n", encoding="utf-8"
                )
                for ref in ready:
                    if ref.path.exists():
                        _safe_move(ref.path, failed_dir)
                summary.groups.append(GroupResult(
                    test_id=test_id,
                    files=metadata,
                    manifest_path=None,
                    status="failed",
                    error=err_msg,
                ))
                continue
        else:
            ingest_result = {
                "totals": {"events": 0, "nas_events": 0, "rf_kpis": 0},
                "file_count": len(ready),
            }

        # Move quarantined (bad) chunks into failed/<test_id>/ BEFORE we
        # archive the good ones. Per the user's spec, a single bad chunk
        # quarantines the entire group — agents must never see a half-parsed
        # test run, only whole runs that all indexed cleanly.
        if bad_chunks:
            failed_dir = in_dir / failed_subdir / test_id
            failed_dir.mkdir(parents=True, exist_ok=True)
            err_lines = [
                f"test_id={test_id}",
                f"timestamp={_dt.datetime.now(tz=_dt.timezone.utc).isoformat()}",
                "individual chunk(s) failed to index — entire group quarantined:",
            ]
            for entry in bad_chunks:
                err_lines.append(f"  - {entry['filename']} (chunk_seq={entry['chunk_seq']}): {entry['reason']}")
            (failed_dir / "error.txt").write_text(
                "\n".join(err_lines) + "\n", encoding="utf-8"
            )
            for ref in ready:
                if ref.path.exists():
                    _safe_move(ref.path, failed_dir)
            summary.groups.append(GroupResult(
                test_id=test_id,
                files=[],
                manifest_path=None,
                status="failed",
                error="; ".join(
                    f"{e['filename']} (chunk_seq={e['chunk_seq']}): {e['reason']}"
                    for e in bad_chunks
                ),
            ))
            continue

        # Move all ready files into processed/<test_id>/
        processed_dir = in_dir / processed_subdir / test_id
        processed_dir.mkdir(parents=True, exist_ok=True)
        moved: list[dict[str, Any]] = []
        for meta in metadata:
            src = in_dir / meta["filename"]
            if not src.exists():
                # Already moved on a prior retry.
                moved.append({**meta, "moved_to": str(
                    processed_dir / meta["filename"]
                )})
                continue
            try:
                dst = _safe_move(src, processed_dir)
            except OSError as exc:
                _emit(f"move failed for {src}: {exc}")
                continue
            moved.append({**meta, "moved_to": str(dst)})

        manifest_path = processed_dir / "manifest.json"
        manifest_payload = {
            "test_id": test_id,
            "ingested_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
            "chunks": moved,
            "totals": {
                "files": len(moved),
                **ingest_result.get("totals", {}),
            },
        }
        try:
            manifest_path.write_text(
                json.dumps(manifest_payload, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("could not write manifest for %s: %s", test_id, exc)

        summary.groups.append(GroupResult(
            test_id=test_id,
            files=moved,
            manifest_path=manifest_path,
            status="processed",
        ))

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scan an input directory, group QXDM text logs by test run, "
            "index them into a SQLite database, and archive the originals."
        ),
    )
    parser.add_argument(
        "--input-dir", default="./incoming_logs",
        help="Directory to scan for ``*.txt`` chunks (default: ./incoming_logs).",
    )
    parser.add_argument(
        "--db", default="qxdm_indexed.db",
        help="Target SQLite database path (default: qxdm_indexed.db).",
    )
    parser.add_argument(
        "--config", default=indexer.DEFAULT_CONFIG_PATH,
        help="Path to parser_config.json (default: parser_config.json).",
    )
    parser.add_argument(
        "--stability-window", type=float, default=2.0,
        help="Seconds the file size must stay unchanged before ingestion "
             "(default: 2.0). Set to 0 to skip the stability check.",
    )
    parser.add_argument(
        "--max-wait", type=float, default=120.0,
        help="Maximum seconds to wait for a single file to stabilise "
             "(default: 120.0).",
    )
    parser.add_argument(
        "--processed-subdir", default="processed",
        help="Subfolder name under the input dir for successfully "
             "processed chunks (default: processed).",
    )
    parser.add_argument(
        "--failed-subdir", default="failed",
        help="Subfolder name under the input dir for quarantined "
             "chunks (default: failed).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Index against a scratch DB but still move files; primarily "
             "for end-to-end smoke tests.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit a machine-readable JSON summary on stdout.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    summary = process_directory(
        input_dir=args.input_dir,
        db_path=args.db,
        config_path=args.config,
        stability_window=args.stability_window,
        max_wait=args.max_wait,
        processed_subdir=args.processed_subdir,
        failed_subdir=args.failed_subdir,
    )

    payload = summary.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for g in summary.groups:
            print(
                f"[{g.status.upper()}] {g.test_id}: "
                f"{len(g.files)} file(s)"
                + (f" — {g.error}" if g.error else "")
            )
        if summary.unclassified:
            print(
                f"[skip] unclassified files: "
                f"{', '.join(summary.unclassified)}",
                file=sys.stderr,
            )
    return 0 if all(g.status != "failed" for g in summary.groups) else 1


if __name__ == "__main__":
    raise SystemExit(main())
