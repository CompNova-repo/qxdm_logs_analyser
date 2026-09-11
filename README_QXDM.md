# QXDM Log Indexing Workflow

This directory contains a deterministic pre-parser and query tool for
decoded QXDM/QCAT text logs, **plus** a multi-file aggregation pipeline.
The scripts keep large logs out of an LLM context window by first
indexing the useful blocks into SQLite.

## Files

- `indexer.py` streams one or more decoded text logs into
  `qxdm_indexed.db`. Multi-file ingestion is supported via `--test-id`
  or `--dir`.
- `batch_processor.py` scans an input directory, groups `.txt` chunks
  by their test-run id, indexes them as one logical run, and archives
  the originals to `processed/<test_id>/`.
- `qxdm_tool.py` queries the SQLite index for anomalies, NAS reject
  causes, RF summaries, multi-file aggregations, and raw context
  windows around failure points.

All scripts use only the Python standard library and are Windows-safe
(`pathlib.Path` everywhere).

## Quick start: single file

```bash
python3 indexer.py UE23_TCP_UDP_UL_75_01-11.06-36-49-875.txt
python3 qxdm_tool.py anomalies
python3 qxdm_tool.py nas
python3 qxdm_tool.py rf-summary
python3 qxdm_tool.py window "2024 Nov 1 11:17:29.011"
python3 qxdm_tool.py parser-overlaps
```

## Quick start: multi-file test run

```bash
# 1. Drop the chunks into a folder.
mkdir -p incoming_logs
cp RUN123_session_001.txt incoming_logs/
cp RUN123_session_002.txt incoming_logs/

# 2. Let batch_processor group, hash, index, and archive them.
python3 batch_processor.py --input-dir incoming_logs

# 3. List indexed runs and drill into one.
python3 qxdm_tool.py list-tests
python3 qxdm_tool.py anomalies --test-id RUN123
python3 qxdm_tool.py rf-summary --test-id RUN123
python3 qxdm_tool.py window "2024 Nov 1 11:17:29.011" --test-id RUN123 --count 10
```

## CLI reference (abbreviated)

```bash
python3 indexer.py [--test-id ID] [--dir DIR] [--db DB]
                  [--config parser_config.json] [--append]
                  [--dry-run] [--promote] [--quiet-overlaps]
                  <log_file>... | (--dir <DIR>)

python3 batch_processor.py [--input-dir DIR] [--db DB] [--config PATH]
                          [--stability-window SEC] [--max-wait SEC]
                          [--processed-subdir NAME] [--failed-subdir NAME]
                          [--dry-run] [--json]

python3 qxdm_tool.py [--db DB]
    list-tests
    anomalies [--test-id ID] [--with-cause] [--cause <code>]
    rf-summary [--test-id ID]
    nas [--test-id ID] [--all]
    events [--test-id ID] [-n N]
    window "<timestamp>" [--test-id ID] [-n N]
    parser-health
    parser-failures [<parser_name>] [-n N]
    parser-overlaps [-n N]
    unknown-types [-n N]
```

## Agent Prompt

Use this prompt with Hermes or another local agentic runtime:

```text
You are a Cellular RF Protocol Engineer. The QXDM test run is already
indexed in qxdm_indexed.db — do NOT read the raw .txt files.

1. Run `python3 qxdm_tool.py list-tests --db qxdm_indexed.db` to find
   the available test_id(s).
2. Run `python3 qxdm_tool.py anomalies --test-id <ID>` to discover
   RRC releases, SCell teardowns, and NAS reject causes.
3. Run `python3 qxdm_tool.py rf-summary --test-id <ID>` to evaluate
   physical-layer channel conditions across the whole session.
4. For the failure point, run
   `python3 qxdm_tool.py window "<timestamp>" --test-id <ID> --count 10`
   to extract a 5–10 KB raw signaling slice that crosses chunk
   boundaries when necessary.
5. Derive every numeric and protocol conclusion from tool output.
   Never invent cause values or counts.
```
