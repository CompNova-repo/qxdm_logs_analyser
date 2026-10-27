#!/usr/bin/env python3
"""Small deterministic query tool for the QXDM SQLite index."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = "qxdm_indexed.db"

# Factored anomaly detection patterns — each entry documents which event it catches.
# Using a named list makes LIKE overlap visible and lets the agent tune patterns
# without deciphering a monolithic WHERE clause.
ANOMALY_PATTERNS: list[tuple[str, str]] = [
    # RRC Connection Release via Event DL_MSG
    ("msg_type LIKE '%EVENT_LTE_RRC_DL_MSG%' AND summary LIKE '%Connection Release%'", "matches DL RRC Connection Release via EVENT_LTE_RRC_DL_MSG"),
    # RRC State Change closing
    ("msg_type LIKE '%EVENT_LTE_RRC_STATE_CHANGE%' AND summary LIKE '%Closing%'", "matches RRC state closing"),
    # SCell teardown due to PCell RLF
    ("msg_type LIKE '%EVENT_LTE_SCELL_STATE_CHANGE%' AND summary LIKE '%PCell RLF%'", "matches SCell teardown from PCell RLF"),
    # MAC reset for connection release
    ("msg_type LIKE '%EVENT_LTE_MAC_RESET%' AND summary LIKE '%Connection release%'", "matches MAC reset connection release"),
    # Generic 3GPP release strings — kept distinct so overlap is explicit
    ("msg_type LIKE '%RRCConnectionRelease%'", "matches 3GPP RRCConnectionRelease msg_type"),  # catches NR RRC release
    ("msg_type LIKE '%RRC Release'", "matches RRC Release msg_type variant"),  # catches LTE RRC Release
    ("summary LIKE '%DL_RRCConnectionRelease%'", "matches DL_RRCConnectionRelease in summary"),
    ("summary LIKE '%STATUS FAILURE%'", "matches STATUS FAILURE"),
    ("summary LIKE '%CONNECTION_RELEASE%'", "matches CONNECTION_RELEASE summary token"),
]

# Regex to surface releaseCause from raw_block without a second window query.
_RELEASE_CAUSE_RE = re.compile(r"releaseCause\s*[:=]?\s*([^\s,;\)}]+)", re.IGNORECASE)



def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(
            f"SQLite index not found: {path}. Run: python3 indexer.py <log_file.txt>"
        )
    return sqlite3.connect(str(path))


def query_anomalies(
    db_path: str, include_cause: bool = False,
    test_id: str | None = None,
) -> list[dict[str, Any]]:
    """Scan for RRC releases, state changes, SCell teardowns, and RLFs.

    Patterns are defined in :data:`ANOMALY_PATTERNS` so overlap is explicit.
    When ``include_cause`` is True (or always for convenience), ``releaseCause``
    is extracted from ``raw_block`` via regex so callers do not need a second
    ``window`` query to diagnose releases. When ``test_id`` is supplied,
    results are restricted to that single logical test run.
    """
    conn = connect(db_path)
    cur = conn.cursor()
    where_clause = " OR ".join(f"({pat})" for pat, _ in ANOMALY_PATTERNS)
    clauses = [where_clause]
    params: list[Any] = []
    if test_id:
        clauses.append("test_id = ?")
        params.append(test_id)
    full_where = " AND ".join(f"({c})" for c in clauses)
    query = f"""
        SELECT timestamp, sequence, msg_code, msg_type, summary, raw_block
        FROM events
        WHERE {full_where}
        ORDER BY sequence ASC
    """
    rows = cur.execute(query, params).fetchall()
    conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        ts, seq, code, msg_type, summary, raw_block = row
        entry: dict[str, Any] = {
            "timestamp": ts,
            "sequence": seq,
            "msg_code": code,
            "event": msg_type,
            "summary": summary,
        }
        # Surface releaseCause inline — matches CLAUDE.md guidance to inspect IEs
        # like releaseCause without a follow-up window call.
        if raw_block:
            m = _RELEASE_CAUSE_RE.search(raw_block)
            if m:
                entry["releaseCause"] = m.group(1).strip()
            # Also surface raw_block snippet when include_cause requested for full IE inspection
            if include_cause:
                entry["raw_block"] = raw_block[:2000]
        out.append(entry)
    return out


def get_rf_summary(db_path: str, test_id: str | None = None) -> dict[str, Any]:
    """Calculate min, max, and average for RF KPIs.

    When ``test_id`` is supplied, results are scoped to that logical test
    run; otherwise the aggregation spans every indexed run in the DB.
    """
    conn = connect(db_path)
    cur = conn.cursor()
    params: list[Any] = []
    where = ""
    if test_id:
        where = " WHERE test_id = ?"
        params.append(test_id)
    query = f"""
        SELECT
            COUNT(*),
            AVG(rsrp), MIN(rsrp), MAX(rsrp),
            AVG(rsrq), MIN(rsrq), MAX(rsrq),
            AVG(rssi), MIN(rssi), MAX(rssi),
            AVG(snr), MIN(snr), MAX(snr)
        FROM rf_kpis
        {where}
    """
    row = cur.execute(query, params).fetchone()
    conn.close()

    if not row or row[0] == 0:
        return {"status": "No RF metrics found", "test_id": test_id}

    return {
        "test_id": test_id,
        "records": row[0],
        "avg_rsrp_dbm": round(row[1], 2) if row[1] is not None else None,
        "min_rsrp_dbm": row[2],
        "max_rsrp_dbm": row[3],
        "avg_rsrq_db": round(row[4], 2) if row[4] is not None else None,
        "min_rsrq_db": row[5],
        "max_rsrq_db": row[6],
        "avg_rssi_dbm": round(row[7], 2) if row[7] is not None else None,
        "min_rssi_dbm": row[8],
        "max_rssi_dbm": row[9],
        "avg_snr_db": round(row[10], 2) if row[10] is not None else None,
        "min_snr_db": row[11],
        "max_snr_db": row[12],
    }


def get_context_window(
    db_path: str, timestamp: str | None, window_count: int = 5,
    test_id: str | None = None, sequence: int | None = None,
) -> str:
    """Retrieve raw signaling blocks before and after an event.

    Anchor on ``sequence`` (exact block, as returned by ``nas``, ``events`` and
    ``anomalies``) or on ``timestamp`` (first indexed event at or before it).
    Many blocks can share one millisecond, so ``sequence`` is the precise
    anchor. Slices across chunk boundaries if ``test_id`` spans multiple files
    because the global ``sequence`` column already bridges chunk seams.
    """
    if (timestamp is None) == (sequence is None):
        raise ValueError("give exactly one of timestamp or --sequence")
    conn = connect(db_path)
    cur = conn.cursor()
    if sequence is not None:
        clauses = ["sequence = ?"]
        params: list[Any] = [sequence]
        order = "sequence ASC"
    else:
        clauses = ["timestamp <= ?"]
        params = [timestamp]
        order = "timestamp DESC, sequence ASC"
    if test_id:
        clauses.append("test_id = ?")
        params.append(test_id)
    where = " AND ".join(f"({c})" for c in clauses)
    target = cur.execute(
        f"""
        SELECT sequence
        FROM events
        WHERE {where}
        ORDER BY {order}
        LIMIT 1
        """,
        params,
    ).fetchone()

    if not target:
        conn.close()
        if sequence is not None:
            return f"No indexed event found at sequence: {sequence}"
        return f"No indexed event found at or before timestamp: {timestamp}"

    target_sequence = target[0]
    slice_clauses = ["sequence BETWEEN ? AND ?"]
    slice_params: list[Any] = [target_sequence - window_count, target_sequence + window_count]
    if test_id:
        slice_clauses.append("test_id = ?")
        slice_params.append(test_id)
    slice_where = " AND ".join(f"({c})" for c in slice_clauses)
    rows = cur.execute(
        f"""
        SELECT timestamp, msg_type, raw_block, source_file
        FROM events
        WHERE {slice_where}
        ORDER BY sequence ASC
        """,
        slice_params,
    ).fetchall()
    conn.close()

    return "\n---\n".join(
        f"[source: {row[3]} @ {row[0]}]\n{row[2]}" for row in rows
    )


def list_events(
    db_path: str, limit: int, test_id: str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    cur = conn.cursor()
    clauses = []
    params: list[Any] = []
    if test_id:
        clauses.append("test_id = ?")
        params.append(test_id)
    where = ""
    if clauses:
        where = " WHERE " + " AND ".join(f"({c})" for c in clauses)
    params.append(limit)
    rows = cur.execute(
        f"""
        SELECT timestamp, sequence, msg_code, msg_type, summary,
               test_id, source_file, chunk_seq
        FROM events
        {where}
        ORDER BY sequence ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    conn.close()
    return [
        {
            "timestamp": row[0],
            "sequence": row[1],
            "msg_code": row[2],
            "event": row[3],
            "summary": row[4],
            "test_id": row[5],
            "source_file": row[6],
            "chunk_seq": row[7],
        }
        for row in rows
    ]


def query_nas(
    db_path: str, all_events: bool = False, test_id: str | None = None,
) -> list[dict[str, Any]]:
    """Queries 5GMM/EMM rejects, timers, and cause codes.

    Scope to a single test run via ``test_id`` when set.
    """
    conn = connect(db_path)
    cur = conn.cursor()
    clauses: list[str] = []
    params: list[Any] = []
    if not all_events:
        clauses.append("cause_code IS NOT NULL")
    if test_id:
        clauses.append("test_id = ?")
        params.append(test_id)
    query = "SELECT timestamp, rat, msg_id, cause_code, cause_str, t3346_timer, t3502_timer, source_file, sequence FROM nas_events"
    if clauses:
        query += " WHERE " + " AND ".join(f"({c})" for c in clauses)
    query += " ORDER BY timestamp ASC, sequence ASC"
    rows = cur.execute(query, params).fetchall()
    conn.close()
    return [
        {
            "timestamp": r[0],
            "rat": r[1],
            "msg": r[2],
            "cause_code": r[3],
            "cause_str": r[4],
            "t3346_timer": r[5],
            "t3502_timer": r[6],
            "source_file": r[7],
            "sequence": r[8],
        }
        for r in rows
    ]


def list_tests(db_path: str) -> list[dict[str, Any]]:
    """Return per-test summary rows: test_id, chunk count, time span, record counts."""
    conn = connect(db_path)
    cur = conn.cursor()
    out: list[dict[str, Any]] = []
    try:
        # Aggregate events/nas/rf counts per test_id in one pass each. UNION ALL
        # keeps the agent's query plan simple even when one of the tables has
        # no rows for a given test_id.
        cur.execute(
            """
            CREATE TEMP VIEW IF NOT EXISTS _test_events AS
            SELECT test_id, COUNT(*) AS n FROM events GROUP BY test_id
            """
        )
        cur.execute(
            """
            CREATE TEMP VIEW IF NOT EXISTS _test_nas AS
            SELECT test_id, COUNT(*) AS n FROM nas_events GROUP BY test_id
            """
        )
        cur.execute(
            """
            CREATE TEMP VIEW IF NOT EXISTS _test_rf AS
            SELECT test_id, COUNT(*) AS n FROM rf_kpis GROUP BY test_id
            """
        )

        rows = cur.execute(
            """
            SELECT
                e.test_id,
                COUNT(DISTINCT e.source_file) AS chunks,
                MIN(e.chunk_seq) AS first_chunk,
                MAX(e.chunk_seq) AS last_chunk,
                MIN(e.timestamp) AS first_timestamp,
                MAX(e.timestamp) AS last_timestamp,
                COALESCE(e_n.n, 0) AS event_count,
                COALESCE(n.n, 0)   AS nas_count,
                COALESCE(r.n, 0)   AS rf_count
            FROM events e
            LEFT JOIN _test_events e_n ON e_n.test_id = e.test_id
            LEFT JOIN _test_nas n ON n.test_id = e.test_id
            LEFT JOIN _test_rf r ON r.test_id = e.test_id
            GROUP BY e.test_id
            ORDER BY MIN(e.timestamp) ASC
            """
        ).fetchall()
        # Also pick up test_ids that exist only in nas_events / rf_kpis
        # (rare but possible if events table was dropped on a re-index).
        extras = cur.execute(
            """
            SELECT test_id, 'nas' AS kind FROM nas_events
            UNION SELECT test_id, 'rf' AS kind FROM rf_kpis
            """
        ).fetchall()
        seen = {row[0] for row in rows}
        for tid, kind in extras:
            if not tid or tid in seen:
                continue
            out.append({
                "test_id": tid,
                "chunks": 0,
                "first_chunk": None,
                "last_chunk": None,
                "first_timestamp": None,
                "last_timestamp": None,
                "event_count": 0,
                "nas_count": 1 if kind == "nas" else 0,
                "rf_count": 1 if kind == "rf" else 0,
                "note": "test_id present in {kind} only",
            })
    except sqlite3.OperationalError:
        # Pre-multifile DBs have no test_id column; surface empty result
        # instead of crashing so the agent can still list legacy data.
        conn.close()
        return []
    conn.close()

    for tid, chunks, first_chunk, last_chunk, first_ts, last_ts, ev_n, nas_n, rf_n in rows:
        out.append({
            "test_id": tid or "(unset)",
            "chunks": chunks,
            "first_chunk_seq": first_chunk,
            "last_chunk_seq": last_chunk,
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
            "event_count": ev_n,
            "nas_event_count": nas_n,
            "rf_kpi_count": rf_n,
        })
    out.sort(key=lambda r: (r.get("first_timestamp") or "", r["test_id"]))
    return out


def parser_health(db_path: str) -> dict[str, Any]:
    """Summarise parser match/parse/fail counts from the last indexer run."""
    conn = connect(db_path)
    cur = conn.cursor()
    try:
        rows = cur.execute(
            "SELECT parser_name, matched, parsed, failed, invalid_value "
            "FROM parser_health ORDER BY parser_name ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {
            "parsers": {},
            "data": {},
            "warning": "parser_health table is missing — re-run indexer.py",
        }
    conn.close()
    parsers = {
        row[0]: {
            "matched": row[1],
            "parsed": row[2],
            "failed": row[3],
            "invalid_value": row[4],
        }
        for row in rows
    }
    return {"parsers": parsers, "data": parsers}


def parser_failures(
    db_path: str,
    parser_name: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Return representative failure samples the indexer captured.

    If ``parser_name`` is ``None`` every parser with at least one failure is
    included, returning up to ``limit`` samples *per parser* (not first-N
    globally). Otherwise only that parser is queried.

    Always returns an envelope ``{"failures": [...], "data": [...], "warning": ...}``
    so callers can handle all health commands uniformly. When the
    ``parser_failures`` table is absent the envelope contains an empty list
    plus a warning.
    """
    conn = connect(db_path)
    cur = conn.cursor()
    try:
        if parser_name:
            rows = cur.execute(
                "SELECT parser_name, msg_code, timestamp, failure_reason, sample_block "
                "FROM parser_failures WHERE parser_name = ? "
                "ORDER BY id ASC LIMIT ?",
                (parser_name, limit),
            ).fetchall()
        else:
            # Per-parser sampling: fetch all and slice per parser in Python to
            # ensure later parsers are not starved by an early parser's rows.
            all_rows = cur.execute(
                "SELECT parser_name, msg_code, timestamp, failure_reason, sample_block "
                "FROM parser_failures ORDER BY id ASC",
            ).fetchall()
            # Group by parser and take first `limit` per group
            from collections import defaultdict
            grouped: dict[str, list] = defaultdict(list)
            for r in all_rows:
                if len(grouped[r[0]]) < limit:
                    grouped[r[0]].append(r)
            # Flatten in parser_name order for deterministic output
            rows = []
            for pname in sorted(grouped.keys()):
                rows.extend(grouped[pname])
    except sqlite3.OperationalError:
        conn.close()
        return {
            "failures": [],
            "data": [],
            "warning": "parser_failures table is missing — re-run indexer.py",
        }
    conn.close()
    failures = [
        {
            "parser": row[0],
            "msg_code": row[1],
            "timestamp": row[2],
            "failure_reason": row[3],
            "sample_block": row[4],
        }
        for row in rows
    ]
    return {"failures": failures, "data": failures}


def parser_overlaps(db_path: str, limit: int = 20) -> dict[str, Any]:
    """Return blocks claimed by multiple parsers and the parser selected."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT timestamp, msg_code, selected_parser, claiming_parsers, sample_block "
            "FROM parser_overlaps ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {"overlaps": [], "data": [], "warning": "parser_overlaps table is missing — re-run indexer.py"}
    conn.close()
    overlaps = [
        {
            "timestamp": row[0], "msg_code": row[1], "selected_parser": row[2],
            "claiming_parsers": row[3].split(","), "sample_block": row[4],
        }
        for row in rows
    ]
    return {"overlaps": overlaps, "data": overlaps}


def unknown_types(db_path: str, limit: int = 20) -> dict[str, Any]:
    """List message codes the indexer did not match, plus a few samples each."""
    conn = connect(db_path)
    cur = conn.cursor()
    try:
        code_rows = cur.execute(
            "SELECT msg_code, occurrences, sample_count "
            "FROM unknown_msg_codes ORDER BY occurrences DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {"unknown_msg_codes": [], "data": [], "warning": "unknown_msg_codes table is missing"}

    samples_table_missing = False
    out_codes = []
    for code, occ, sample_count in code_rows:
        try:
            samples = cur.execute(
                "SELECT timestamp, msg_type, sample_block FROM unknown_samples "
                "WHERE msg_code = ? ORDER BY id ASC LIMIT 3",
                (code,),
            ).fetchall()
        except sqlite3.OperationalError:
            # Symmetric with the outer SELECT: if ``unknown_samples`` is
            # missing but ``unknown_msg_codes`` is populated (e.g. partial
            # re-index or manual table drop), return the code summary with
            # an empty sample list rather than crashing with an unhandled
            # OperationalError. The envelope-level warning surfaces the
            # partial state to the agent.
            samples_table_missing = True
            samples = []
        out_codes.append({
            "msg_code": code,
            "occurrences": occ,
            "sample_count": sample_count,
            "samples": [
                {"timestamp": s[0], "msg_type": s[1], "sample_block": s[2]}
                for s in samples
            ],
        })
    conn.close()
    envelope: dict[str, Any] = {"unknown_msg_codes": out_codes, "data": out_codes}
    if samples_table_missing:
        envelope["warning"] = (
            "unknown_samples table is missing — sample details omitted"
        )
    # Ensure generic envelope alias for uniform handling
    if "warning" in envelope:
        envelope["data"] = out_codes
    return envelope


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query a SQLite index produced by indexer.py."
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"SQLite DB path, default: {DEFAULT_DB_PATH}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list-tests",
        help="List every test_id indexed in the DB with chunk + record counts",
    )

    anomalies_parser = subparsers.add_parser("anomalies", help="List likely teardown/failure events")
    anomalies_parser.add_argument(
        "--with-cause", action="store_true",
        help="Include raw_block and extracted releaseCause in anomalies output",
    )
    anomalies_parser.add_argument(
        "--cause", type=str, default=None,
        help="Filter anomalies to those whose releaseCause matches this value",
    )
    anomalies_parser.add_argument(
        "--test-id", dest="test_id", default=None,
        help="Scope results to a single logical test run.",
    )

    rf_summary_parser = subparsers.add_parser("rf-summary", help="Summarize RF KPIs")
    rf_summary_parser.add_argument(
        "--test-id", dest="test_id", default=None,
        help="Scope RF summary to a single logical test run.",
    )

    nas_parser = subparsers.add_parser(
        "nas", help="Query 5GMM/EMM rejects, timers, and cause codes"
    )
    nas_parser.add_argument(
        "--all",
        action="store_true",
        help="List all NAS events (not only those with cause codes)",
    )
    nas_parser.add_argument(
        "--test-id", dest="test_id", default=None,
        help="Scope NAS query to a single logical test run.",
    )

    window_parser = subparsers.add_parser(
        "window", help="Print raw indexed event blocks around a timestamp"
    )
    window_parser.add_argument(
        "timestamp", nargs="?", default=None,
        help='Example: "2024 Nov 1 11:17:29.011". Omit when using --sequence.',
    )
    window_parser.add_argument(
        "--sequence", type=int, default=None,
        help="Anchor on this exact block sequence (from nas/events/anomalies output)",
    )
    window_parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=5,
        help="Number of indexed events before and after the target, default: 5",
    )
    window_parser.add_argument(
        "--test-id", dest="test_id", default=None,
        help="Scope window to a single logical test run.",
    )

    events_parser = subparsers.add_parser("events", help="List indexed events")
    events_parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=50,
        help="Maximum events to print, default: 50",
    )
    events_parser.add_argument(
        "--test-id", dest="test_id", default=None,
        help="Scope event listing to a single logical test run.",
    )

    health_parser = subparsers.add_parser(
        "parser-health",
        help="Summarise match/parse/fail counts for every parser in the last indexer run",
    )

    failures_parser = subparsers.add_parser(
        "parser-failures",
        help="Print representative failed blocks for a parser (default: every parser)",
    )
    failures_parser.add_argument(
        "parser_name",
        nargs="?",
        help="Optional parser name to filter on, e.g. nr_searcher",
    )

    overlaps_parser = subparsers.add_parser(
        "parser-overlaps",
        help="List blocks claimed by multiple parsers and the parser that won",
    )
    overlaps_parser.add_argument("--limit", type=int, default=20)
    failures_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum samples to print, default: 10",
    )

    unknown_parser = subparsers.add_parser(
        "unknown-types",
        help="List message codes that no parser claimed, with a few samples each",
    )
    unknown_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum distinct unknown codes to print, default: 20",
    )

    _add_results_commands(subparsers)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


#: Read-only evidence queries. Only these may be recorded as evidence by
#: ``save-analysis``; everything else (including the results commands) is
#: refused so evidence can never be produced by a write or by another DB.
EVIDENCE_COMMANDS = frozenset({
    "list-tests", "anomalies", "rf-summary", "nas", "window", "events",
    "parser-health", "parser-failures", "parser-overlaps", "unknown-types",
})

#: Evidence commands that accept ``--test-id``. Evidence recorded for an
#: analysis must be scoped to that analysis's test run.
TEST_SCOPED_COMMANDS = frozenset({"anomalies", "rf-summary", "nas", "window", "events"})


def _dispatch_query(args: argparse.Namespace) -> str:
    """Run one evidence query and return exactly what the CLI would print."""
    if args.command == "list-tests":
        return json.dumps(list_tests(args.db), indent=2)
    if args.command == "anomalies":
        anomalies = query_anomalies(
            args.db,
            include_cause=getattr(args, "with_cause", False),
            test_id=getattr(args, "test_id", None),
        )
        if getattr(args, "cause", None):
            anomalies = [a for a in anomalies if a.get("releaseCause") == args.cause]
        return json.dumps(anomalies, indent=2)
    if args.command == "rf-summary":
        return json.dumps(
            get_rf_summary(args.db, test_id=getattr(args, "test_id", None)), indent=2,
        )
    if args.command == "nas":
        return json.dumps(
            query_nas(args.db, all_events=args.all, test_id=getattr(args, "test_id", None)),
            indent=2,
        )
    if args.command == "window":
        return get_context_window(
            args.db, args.timestamp, args.count, test_id=getattr(args, "test_id", None),
            sequence=args.sequence,
        )
    if args.command == "events":
        return json.dumps(
            list_events(args.db, args.limit, test_id=getattr(args, "test_id", None)),
            indent=2,
        )
    if args.command == "parser-health":
        return json.dumps(parser_health(args.db), indent=2)
    if args.command == "parser-failures":
        return json.dumps(parser_failures(args.db, args.parser_name, args.limit), indent=2)
    if args.command == "parser-overlaps":
        return json.dumps(parser_overlaps(args.db, args.limit), indent=2)
    if args.command == "unknown-types":
        return json.dumps(unknown_types(args.db, args.limit), indent=2)
    raise ValueError(f"not an evidence query: {args.command}")


def run_query(db_path: str, argv: list[str]) -> str:
    """Run an evidence query given as CLI arguments, against ``db_path``.

    Used by ``save-analysis`` to capture evidence itself. ``argv`` is the part
    after ``qxdm_tool.py`` (e.g. ``["nas", "--test-id", "UE23"]``). It must not
    contain ``--db``: the evidence DB is fixed by the caller.
    """
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("evidence command must be a non-empty list of strings")
    if argv[0] not in EVIDENCE_COMMANDS:
        raise ValueError(f"{argv[0]!r} is not an allowed evidence query")
    if any(a == "--db" or a.startswith("--db=") for a in argv):
        raise ValueError("evidence commands may not override --db")
    parser = build_parser()
    parser.exit_on_error = False
    try:
        args = parser.parse_args(["--db", db_path, *argv])
    except (argparse.ArgumentError, SystemExit) as exc:
        raise ValueError(f"invalid evidence command {argv!r}: {exc}") from None
    return _dispatch_query(args)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command in EVIDENCE_COMMANDS:
            print(_dispatch_query(args))
        else:
            return _run_results_command(args)
        return 0
    except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Results DB commands (stored analyses, findings and evidence)
# ---------------------------------------------------------------------------


def _add_results_commands(subparsers: Any) -> None:
    def results_db_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--results-db", default="qxdm_results.db",
            help="Results DB path, default: qxdm_results.db",
        )

    p = subparsers.add_parser(
        "save-analysis",
        help="Validate an analysis JSON, capture its evidence from --db, and store it",
    )
    results_db_arg(p)
    p.add_argument("analysis_file", help="Path to the analysis JSON (see docs/RESULTS_DB.md)")
    p.add_argument(
        "--allow-ungrounded", action="store_true",
        help="Store findings that only have note evidence (they are flagged ungrounded)",
    )
    p.add_argument("--parser-config", default=None, help="parser_config.json used to build --db")
    p.add_argument("--indexer-commit", default=None, help="Git commit of the indexer used to build --db")

    p = subparsers.add_parser("list-analyses", help="List stored analyses")
    results_db_arg(p)
    p.add_argument("--test-id", dest="test_id", default=None)
    p.add_argument("--status", default=None)

    p = subparsers.add_parser("show-analysis", help="Show one stored analysis with findings and evidence")
    results_db_arg(p)
    p.add_argument("analysis_id")
    p.add_argument("--markdown", action="store_true", help="Print only the stored markdown report")

    p = subparsers.add_parser("review-analysis", help="Record a human review decision")
    results_db_arg(p)
    p.add_argument("analysis_id")
    p.add_argument("--status", required=True, help="reviewed | accepted | rejected | superseded")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--note", default="")

    p = subparsers.add_parser("findings", help="List stored findings across analyses")
    results_db_arg(p)
    p.add_argument("--test-id", dest="test_id", default=None)
    p.add_argument("--status", default=None)
    p.add_argument("--layer", default=None)
    p.add_argument("--code", default=None)

    p = subparsers.add_parser("set-finding-status", help="Update a finding's status")
    results_db_arg(p)
    p.add_argument("finding_id", type=int)
    p.add_argument("--status", required=True, help="open | fixed | false_positive | wont_fix")


def _run_results_command(args: argparse.Namespace) -> int:
    import results_db

    try:
        if args.command == "save-analysis":
            payload = json.loads(Path(args.analysis_file).read_text(encoding="utf-8"))
            analysis_id = results_db.save_analysis(
                args.results_db, args.db, payload,
                base_dir=Path(args.analysis_file).resolve().parent,
                allow_ungrounded=args.allow_ungrounded,
                parser_config=args.parser_config,
                indexer_commit=args.indexer_commit,
            )
            print(json.dumps({"analysis_id": analysis_id}, indent=2))
        elif args.command == "list-analyses":
            print(json.dumps(results_db.list_analyses(
                args.results_db, test_id=args.test_id, status=args.status), indent=2))
        elif args.command == "show-analysis":
            analysis = results_db.get_analysis(args.results_db, args.analysis_id)
            print(analysis["report_markdown"] if args.markdown else json.dumps(analysis, indent=2))
        elif args.command == "review-analysis":
            results_db.review_analysis(
                args.results_db, args.analysis_id, args.status, args.reviewer, args.note)
            print(json.dumps(results_db.get_analysis(args.results_db, args.analysis_id)["review"], indent=2))
        elif args.command == "findings":
            print(json.dumps(results_db.list_findings(
                args.results_db, test_id=args.test_id, status=args.status,
                layer=args.layer, code=args.code), indent=2))
        elif args.command == "set-finding-status":
            results_db.set_finding_status(args.results_db, args.finding_id, args.status)
            print(json.dumps({"finding_id": args.finding_id, "status": args.status}, indent=2))
        return 0
    except results_db.AnalysisRejected as exc:
        print(json.dumps({"rejected": True, "errors": exc.errors}, indent=2), file=sys.stderr)
        return 2
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
