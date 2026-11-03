# Results DB

The evidence DB (`qxdm_indexed.db`, built by `indexer.py`) holds what the logs
say. It is disposable: re-indexing rebuilds it, and its contents change when the
parser changes.

The results DB (`qxdm_results.db`, `results_db.py`) holds what was concluded
from it: analyses, findings, the evidence behind each finding, recommendations
and key metrics. It is permanent and survives re-indexing, so results can be
reviewed, filtered and compared later.

```
runs ──< analyses ──< findings ──< evidence
                 │              └──< recommendations
                 └──< metrics
```

| Table | One row per | Key columns |
|---|---|---|
| `runs` | distinct state of a test's evidence index | `test_id`, `index_fingerprint`, source files, chunk and row counts, `parser_config_sha256`, `indexer_commit` |
| `analyses` | RCA attempt | verdict, severity, primary owner, summary, full markdown report, agent/model/prompt version, review status |
| `findings` | conclusion | code, layer, statement, spec reference, confidence, `grounded`, status |
| `evidence` | proof of a finding | `qxdm_tool` command, **captured output** and its SHA-256, verified quotes; or a flagged note |
| `recommendations` | action per finding | action, owner, priority, status |
| `metrics` | headline number per analysis | name, value, unit |

## Grounding rule (enforced)

The agent never types evidence. It gives the `qxdm_tool.py` command and,
optionally, strings it expects in the output. `save-analysis`:

1. runs every command itself against `--db` (read-only queries only; `--db`
   cannot be overridden; test-scoped queries must use the analysis `test_id`),
2. rejects the analysis if any quote is missing from the captured output, or a
   query returns no data,
3. rejects any finding with no query evidence, unless `--allow-ungrounded` is
   given, in which case the finding is stored with `grounded = 0`,
4. stores the captured output and SHA-256 with each evidence row.

Saves are all-or-nothing. Every analysis is tied to a `runs` row whose
fingerprint covers the test's counts, chunks, time span and source files, so a
re-index with a different parser creates a new run instead of silently changing
what old analyses refer to.

## Commands

All are subcommands of `qxdm_tool.py`. `--results-db` defaults to
`qxdm_results.db`.

```bash
# store an analysis (evidence is captured from --db)
python3 qxdm_tool.py --db qxdm_indexed.db save-analysis analyses/UE23/analysis.json \
    [--allow-ungrounded] [--parser-config parser_config.json] [--indexer-commit <sha>]

python3 qxdm_tool.py list-analyses [--test-id UE23] [--status draft]
python3 qxdm_tool.py show-analysis <analysis_id> [--markdown]
python3 qxdm_tool.py review-analysis <analysis_id> --status accepted --reviewer "Name" [--note "..."]
python3 qxdm_tool.py findings [--test-id UE23] [--status open] [--layer core] [--code DEF-1]
python3 qxdm_tool.py set-finding-status <finding_id> --status fixed
```

Review statuses: `reviewed`, `accepted`, `rejected`, `superseded` (new analyses
start as `draft`). Finding statuses: `open`, `fixed`, `false_positive`,
`wont_fix`. Layers: `core`, `ran`, `rf`, `device`, `transport`, `tooling`,
`other`.

To anchor evidence on one exact block, use `window --sequence N` (sequence
numbers come from `nas`, `events` and `anomalies` output). Many blocks share a
millisecond, so `window <timestamp>` may not include the block you mean.

## Analysis JSON

```json
{
  "test_id": "UE23",
  "title": "...",
  "verdict": "...",
  "severity": "P1",
  "primary_owner": "Core / AMF",
  "summary": "...",
  "report_file": "report.md",
  "agent": {"name": "...", "model": "...", "prompt_version": "..."},
  "findings": [
    {
      "code": "DEF-1",
      "title": "...",
      "layer": "core",
      "statement": "...",
      "spec_reference": "3GPP TS 24.501 §5.5.1.3.5",
      "confidence": "high",
      "evidence": [
        {"kind": "query", "command": ["nas", "--test-id", "UE23"],
         "quote": ["\"t3346_timer\": null"]},
        {"kind": "note", "note": "...", "source": "raw log line 14890"}
      ],
      "recommendations": [{"action": "...", "owner": "Core / AMF", "priority": "p1"}]
    }
  ],
  "metrics": [{"name": "registration_rejects", "value": 20, "unit": "count"}]
}
```

Use either `report_markdown` (inline) or `report_file` (path relative to the
JSON file). Unknown fields are rejected, including any attempt to supply
evidence output. `analyses/UE23/` is a complete example.

## Known limits

- `parser_config_sha256` and `indexer_commit` are recorded at save time unless
  passed explicitly. They describe the index correctly only if the index was
  built with the same config and commit. The indexer should record these in
  the evidence DB itself.
- Metrics are stored as given; keep them derivable from the stored evidence.
- SQLite suits a single host. Move to Postgres when several people or a
  dashboard need concurrent access; the schema carries over.
