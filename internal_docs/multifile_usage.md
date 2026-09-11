# Multi-File Pipeline — Operator Guide

## End-to-end flow

```
┌─────────────────────────┐
│  ./incoming_logs/       │  raw *.txt chunks (≤ 250 MB each)
└──────────┬──────────────┘
           │ batch_processor.py
           ▼
┌─────────────────────────┐    ┌─────────────────────────┐
│ per-chunk size check    │───►│ failed/<test_id>/        │ quarantine on failure
└──────────┬──────────────┘    └─────────────────────────┘
           │
           ▼
┌─────────────────────────┐
│ indexer.parse_files()   │  append=True for every chunk
│ (test_id, chunk_seq,    │  global sequence via MAX(sequence)+1
│  source_file on every   │
│  row)                   │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐    ┌─────────────────────────┐
│ qxdm_indexed.db         │◄───│ qxdm_tool.py queries     │
│ (events, nas_events,    │    │  (--test-id filter)      │
│  rf_kpis)               │    └─────────────────────────┘
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ processed/<test_id>/    │  manifest.json + chunks
└─────────────────────────┘
```

## Common commands

```bash
# 1. One-shot batch ingestion
python3 batch_processor.py --input-dir ./incoming_logs --db ./qxdm_indexed.db

# 2. Force a complete rebuild (delete DB first)
rm -f qxdm_indexed.db
python3 batch_processor.py --input-dir ./incoming_logs --db ./qxdm_indexed.db

# 3. Append a single chunk into an existing run
python3 indexer.py --test-id RUN123 --append \
    --db qxdm_indexed.db RUN123_session_005.txt

# 4. Inspect runs
python3 qxdm_tool.py list-tests --db ./qxdm_indexed.db

# 5. Drill into a run
python3 qxdm_tool.py anomalies --test-id RUN123 --db ./qxdm_indexed.db
python3 qxdm_tool.py rf-summary --test-id RUN123 --db ./qxdm_indexed.db
python3 qxdm_tool.py nas --test-id RUN123 --db ./qxdm_indexed.db

# 6. Pull a context window that may straddle chunk boundaries
python3 qxdm_tool.py window "2024 Nov 1 11:17:29.011" --test-id RUN123 \
    --count 10 --db ./qxdm_indexed.db
```

## Manifest shape

`<input>/processed/<test_id>/manifest.json`:

```json
{
  "test_id": "RUN123",
  "ingested_at": "2026-09-11T12:34:56.000000+00:00",
  "chunks": [
    {
      "filename": "RUN123_session_001.txt",
      "size_bytes": 1234,
      "sha256": "abc...",
      "chunk_seq": 1,
      "reason": "explicit_chunk_suffix",
      "moved_to": "<input>/processed/RUN123/RUN123_session_001.txt"
    }
  ],
  "totals": {
    "files": 3,
    "events": 5432,
    "nas_events": 12,
    "rf_kpis": 987
  }
}
```

## Quarantine shape

`<input>/failed/<test_id>/error.txt`:

```
test_id=RUN123
timestamp=2026-09-11T12:34:56.000000+00:00
individual chunk(s) failed to index — entire group quarantined:
  - RUN123_session_002.txt (chunk_seq=2): no_records_indexed
```

The bad chunk (and any good siblings) sit next to `error.txt` so an
operator can re-parse after fixing the file.
