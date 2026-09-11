# Multi-File QXDM Analysis and Hermes Workflow

This document explains how a set of decoded QXDM text-log chunks becomes one
logical test run, how the SQLite schema preserves file provenance, how an agent
should avoid analyzing an incomplete run, and how to run a four-file example.

## 1. End-to-end architecture

The repository implements the deterministic ingestion and query layers. Hermes
is an external agentic runtime: there is no Hermes integration or Hermes skill
in this repository today.

```text
Raw QXDM .txt chunks
        |
        v
batch_processor.py
  - discovers and groups files
  - waits for stable file sizes
  - hashes each file
        |
        v
indexer.parse_files()
  - parses every chunk
  - records test_id/source_file/chunk_seq
  - appends normalized records to SQLite
        |
        v
qxdm_tool.py
  - scopes queries by test_id
  - aggregates RF, NAS, and events across chunks
  - returns small raw context windows
        |
        v
Hermes
  - invokes qxdm_tool.py
  - correlates the returned evidence
  - produces an engineering interpretation
```

This separation keeps large raw logs outside the LLM context window. The
indexer reads the complete files, `qxdm_tool.py` returns compact evidence, and
Hermes reasons over that evidence instead of reading the original logs.

### File grouping

The batch processor recognizes names such as:

```text
RUN123_session_001.txt
RUN123_part2.txt
RUN123_chunk_003.txt
RUN123_20260911123456-004.txt
RUN123_005.txt
```

These names produce a common `test_id` (`RUN123`) and a numeric `chunk_seq`.
Files are grouped by `test_id` and sorted by `(chunk_seq, filename)` before
ingestion.

A plain name such as `alpha.txt` is a fallback single-file group with
`test_id=alpha` and `chunk_seq=0`. Consequently, four files named `alpha.txt`,
`beta.txt`, `gamma.txt`, and `delta.txt` are four test runs to the batch
processor, not four chunks of one test run.

### Indexing the group

For each group, the batch processor:

1. waits until every source file has a stable size;
2. records its size and SHA-256 hash;
3. calls `indexer.parse_files()` with the group's `test_id`;
4. checks whether every chunk yielded at least one event, NAS record, or RF KPI;
5. moves successful source files to `processed/<test_id>/` and writes a
   `manifest.json`; or
6. moves the source group to `failed/<test_id>/` and writes `error.txt` if a
   chunk fails.

SHA-256 values are provenance, not deduplication. Identical input files are
each parsed, so repeating the same content four times will repeat its parsed
records four times.

`indexer.parse_files()` calls `parse_log()` for each input with `append=True`.
Each database row receives the logical run ID, original filename, and chunk
number. The global `events.sequence` value continues from
`MAX(sequence) + 1`, which lets an event window cross a chunk boundary.

### Query and agent analysis

Once ingestion finishes, the analysis boundary is `test_id`, not an individual
file. `qxdm_tool.py` supports test-scoped `anomalies`, `rf-summary`, `nas`,
`events`, and `window` queries. `list-tests` reports the distinct source-file
count, chunk range, timestamp range, and record counts for each run.

Hermes should use this sequence:

1. run `list-tests` and select the intended `test_id`;
2. verify that the indexed chunk count equals the expected file count;
3. run `anomalies --test-id <ID>`;
4. run `rf-summary --test-id <ID>`;
5. run `nas --test-id <ID>`;
6. retrieve `window` output around important timestamps; and
7. derive all numeric and protocol conclusions from those command results.

The indexer supplies facts, `qxdm_tool.py` supplies focused aggregates and
evidence, and Hermes supplies the engineering interpretation. For example,
Hermes may correlate an RRC release with degraded RF measurements or a NAS
reject, but it should never invent a cause value or count.

## 2. Protections against single-file or partial-run analysis

### Existing protections

- Multi-file use of `indexer.py` requires `--test-id`; otherwise the CLI exits
  with an error.
- Every explicitly supplied path is validated before multi-file parsing starts.
- The batch processor collects and sorts a complete filename-derived group
  before invoking the indexer.
- `test_id`, `source_file`, and `chunk_seq` are stored on every analytical row.
- `list-tests` exposes the number of distinct source files for verification.
- A zero-record chunk causes the source group to be quarantined.
- Successfully handled inputs leave the incoming directory and receive a
  manifest, so a normal rerun does not ingest them again.
- End-to-end tests cover grouping, provenance, manifests, global ordering,
  quarantine behavior, query filters, and rerun idempotency.

Before analysis, Hermes should require both of these conditions:

1. `list-tests` reports the expected number of chunks for the chosen test ID;
2. `processed/<test_id>/manifest.json` lists every expected input filename.

### Current limitations

The current implementation cannot know how many chunks a capture was supposed
to produce. For example, it may accept chunks `001`, `002`, and `004` without
knowing that `003` is missing. There is no expected-count field, capture-complete
marker, or contiguous-sequence requirement.

There are also two implementation caveats:

1. `batch_processor.py` constructs filename-derived `sequences`, but currently
   does not pass them as `chunk_sequences=sequences` to `parse_files()`.
   `parse_files()` therefore assigns `1..N` in sorted order. Normal
   `001,002,003,004` input behaves correctly, but a missing or non-1 starting
   suffix is hidden in the database provenance.
2. Each file is committed independently. If the batch processor later
   quarantines a group, moving its source files does not remove already-written
   rows from SQLite. An agent must therefore use the successful manifest as an
   additional readiness gate rather than assuming every visible `test_id` is a
   completed run.

Production hardening should add an expected chunk count or completion marker,
contiguous chunk validation, run-level transaction/cleanup behavior, and an
explicit run status that prevents querying anything except a `READY` run.

## 3. Database schema

### Analytical tables

The principal tables are:

```sql
CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id     TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    chunk_seq   INTEGER NOT NULL DEFAULT 0,
    timestamp   TEXT,
    sequence    INTEGER,
    msg_code    TEXT,
    subsys      TEXT,
    msg_type    TEXT,
    summary     TEXT,
    raw_block   TEXT
);

CREATE TABLE nas_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id      TEXT NOT NULL DEFAULT '',
    source_file  TEXT NOT NULL DEFAULT '',
    chunk_seq    INTEGER NOT NULL DEFAULT 0,
    timestamp    TEXT,
    sequence     INTEGER,
    rat          TEXT,
    msg_id       TEXT,
    cause_code   INTEGER,
    cause_str    TEXT,
    t3346_timer  INTEGER,
    t3502_timer  INTEGER,
    raw_block    TEXT
);

CREATE TABLE rf_kpis (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id     TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    chunk_seq   INTEGER NOT NULL DEFAULT 0,
    timestamp   TEXT,
    sequence    INTEGER,
    rat         TEXT,
    arfcn       INTEGER,
    pci         INTEGER,
    rsrp        REAL,
    rsrq        REAL,
    rssi        REAL,
    snr         REAL
);
```

The code also creates parser-diagnostic tables:

- `parser_health`
- `parser_failures`
- `unknown_msg_codes`
- `unknown_samples`
- `parser_overlaps`

Test-aware indexes cover common combinations including `(test_id, timestamp)`,
`(test_id, msg_type)`, and `(test_id, chunk_seq)`.

### Test-run representation

A test run is part of the schema as a denormalized `test_id` on `events`,
`nas_events`, and `rf_kpis`. It is not yet represented by a separate
`test_runs` table, and file metadata is persisted in an external
`processed/<test_id>/manifest.json` rather than a normalized `test_files`
table.

The checked-in `qxdm_indexed_v2.db` has a legacy schema without the multi-file
provenance columns. The current code defaults to `qxdm_indexed.db` and performs
idempotent column migrations when an older database is opened for append. For
new multi-file work, create a fresh database rather than treating the checked-in
legacy database as canonical.

## 4. Four-file example

### Direct indexer invocation

This is the least ambiguous approach when filenames do not follow the batch
processor's shared-run convention:

```bash
rm -f ./run4.db

python3 indexer.py \
  --test-id RUN4 \
  --db ./run4.db \
  ./logs/file-a.txt \
  ./logs/file-b.txt \
  ./logs/file-c.txt \
  ./logs/file-d.txt
```

Verify and inspect the run:

```bash
python3 qxdm_tool.py --db ./run4.db list-tests
python3 qxdm_tool.py --db ./run4.db anomalies --test-id RUN4 --with-cause
python3 qxdm_tool.py --db ./run4.db rf-summary --test-id RUN4
python3 qxdm_tool.py --db ./run4.db nas --test-id RUN4 --all
```

Delete the experimental database first because `parse_files()` appends its
inputs. With four identical files, expect repeated records and approximately
four times the single-file counts.

### Batch-processor invocation

Use names that share an explicit test ID:

```text
incoming_logs/RUN4_chunk_001.txt
incoming_logs/RUN4_chunk_002.txt
incoming_logs/RUN4_chunk_003.txt
incoming_logs/RUN4_chunk_004.txt
```

Then run:

```bash
rm -f ./run4.db
python3 batch_processor.py --input-dir ./incoming_logs --db ./run4.db

python3 qxdm_tool.py --db ./run4.db list-tests
cat ./incoming_logs/processed/RUN4/manifest.json
```

Confirm that `list-tests` reports four chunks and that the manifest contains
all four filenames before asking Hermes to analyze `RUN4`.

## 5. Should Hermes run the indexer through a skill?

Keep deterministic ingestion separate from analysis by default:

```text
batch_processor.py -> readiness verification -> Hermes/qxdm_tool.py
```

This makes it easier to inspect file grouping and validate completeness before
the agent begins reasoning. It also avoids casually giving the analysis agent a
state-changing operation that moves input files.

A Hermes skill can still be useful as a thin orchestrator. It should call the
existing Python programs rather than reimplement parsing, and it should:

1. accept an input directory, database path, explicit test ID, and expected
   file count;
2. preview and validate all filenames;
3. reject multiple unexpected test IDs or missing chunk numbers;
4. invoke `batch_processor.py`;
5. require a successful processed manifest;
6. run `list-tests` and verify the expected chunk count; and
7. only then run test-scoped analysis queries and synthesize conclusions.

Until database-level run readiness and atomic ingestion are implemented, the
manifest and expected chunk count must be mandatory gates in such a skill.
