# QXDM Log Indexing Workflow

This directory contains a deterministic pre-parser and query tool for decoded
QXDM/QCAT text logs. The scripts keep large logs out of an LLM context window by
first indexing the useful blocks into SQLite.

## Files

- `indexer.py` streams a decoded text log into `qxdm_indexed.db`.
- `qxdm_tool.py` queries the SQLite index for anomalies, NAS reject causes, RF summaries, and raw
  context windows around failure points.

Both scripts use only the Python standard library.

## Usage

After placing the decoded log text file in this directory, run:

```bash
python3 indexer.py UE23_TCP_UDP_UL_75_01-11.06-36-49-875.txt
```

By default, `indexer.py` replaces the generated `qxdm_indexed.db` each time it
runs. To append into an existing database instead:

```bash
python3 indexer.py --append UE23_TCP_UDP_UL_75_01-11.06-36-49-875.txt
```

Then query the index:

```bash
python3 qxdm_tool.py anomalies
python3 qxdm_tool.py nas
python3 qxdm_tool.py rf-summary
python3 qxdm_tool.py window "2024 Nov 1 11:17:29.011"
```

Useful extra command:

```bash
python3 qxdm_tool.py events -n 25
```

## Agent Prompt

Use this prompt with Hermes or another local agentic runtime:

```text
You are a Cellular RF Protocol Engineer. Analyze the QXDM log stored in qxdm_indexed.db.
1. Use `python3 qxdm_tool.py anomalies` to discover failure events and state changes.
2. Use `python3 qxdm_tool.py rf-summary` to evaluate physical layer channel conditions.
3. Use `python3 qxdm_tool.py window <timestamp>` around the failure point to extract the raw signaling delta.
4. Provide a root cause summary and identify why the session ended.
```
