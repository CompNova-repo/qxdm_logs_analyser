# Multi-File Test Run Aggregation — Design

## Goals
Single test run → many `.txt` chunks (rollover at 250 MB) → one unified
SQLite index → AI agents query by `test_id` without reading raw files.

## Schema additions (idempotent `ALTER TABLE`)
Added to: `events`, `nas_events`, `rf_kpis`.

| Column        | Type    | Default | Notes                                    |
|---------------|---------|---------|------------------------------------------|
| `test_id`     | TEXT    | `''`    | Test run identifier, indexed             |
| `source_file` | TEXT    | `''`    | Original filename, indexed              |
| `chunk_seq`   | INTEGER | `0`     | Position in sequence, indexed            |

Composite indexes:
* `idx_events_test_ts (test_id, timestamp)`
* `idx_events_test_msgtype (test_id, msg_type)`
* `idx_nas_test_ts (test_id, timestamp)`
* `idx_rf_test_ts (test_id, timestamp)`

## Filename → test_id / chunk_seq
Order matched (first hit wins):

1. `<id>_(session|part|chunk|seg|seq)[-_]?<seq>.txt`  → explicit sequence.
2. `<id>_<ts>[-_]<seq>.txt`                            → timestamp + sequence.
3. `<id>.txt`                                          → fallback, test_id = stem, chunk_seq = 0.

Anything that doesn't match a pattern is rejected from group ingestion.
Unknown files remain in the input directory; the batch processor logs
them and continues.

## Multi-file ingestion
* `indexer.py` gains `parse_files(files, ...)` that loops over files
  in supplied order, calling `parse_log` for each with `--append=True`
  and a fresh `chunk_seq` and `source_file` provenance.
* Existing single-file CLI behaviour is unchanged.
* Global `sequence` continues across files via the existing
  `MAX(sequence)+1` start offset.

## Batch processor (`batch_processor.py`)
| Step | Action |
|------|--------|
| 1 | Read CLI `--input-dir`, `--db` (default `qxdm_indexed.db`). |
| 2 | Glob `*.txt` in input dir (skip `processed/` and `failed/`). |
| 3 | Group by regex-extracted test_id, sort by (chunk_seq, filename). |
| 4 | Per group, run a two-pass stability check (size unchanged for `--stability-window` seconds). |
| 5 | SHA-256 each file. Call `indexer.parse_files` with `--test-id`. |
| 6 | If record count > 0 and no fatal parser exception: atomically `shutil.move` each file to `processed/<test_id>/`. Write `manifest.json`. |
| 7 | On failure for any file: move entire group to `failed/<test_id>/` and write `error.txt`. |

Default input dir: `./incoming_logs`. CLI overrides.

## Manifest format (`processed/<test_id>/manifest.json`)
```json
{
  "test_id": "RUN123",
  "ingested_at": "2026-09-11T12:34:56Z",
  "chunks": [
    {"filename": "RUN123_session_001.txt", "size_bytes": 1234, "sha256": "abc...", "chunk_seq": 1}
  ],
  "totals": {"files": 3, "events": 5432, "nas_events": 12, "rf_kpis": 987}
}
```

## `qxdm_tool.py` additions
| Subcommand        | Behaviour                                    |
|-------------------|----------------------------------------------|
| `list-tests`      | All `test_id`s with chunk count + time span. |
| `anomalies --test-id <id>` | Filter existing anomaly query.    |
| `rf-summary --test-id <id>` | Per-test RF aggregation.          |
| `nas --test-id <id>` | Filter NAS.                              |
| `window "<ts>" --test-id <id> --window 10` | Sliced context. |
| `events --test-id <id>` | Filter event listing.                  |

`window` keeps cross-chunk ordering because `sequence` is global.

## Windows compatibility
* All I/O via `pathlib.Path`.
* `shutil.move` for file relocation (handles cross-volume).
* `os.replace` only used where same-volume is guaranteed.
* No forward-only path concatenation in new code.
