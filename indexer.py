#!/usr/bin/env python3
"""QXDM log → SQLite indexer with an external, JSON-driven parser config.

The hardcoded message codes / regex patterns that used to live here now live
in ``parser_config.json`` (loadable as ``parser_config.candidate.json`` for
dry-run validation). Only the things that don't belong in a JSON config —
timer decoding per 3GPP TS 24.008 10.5.7.4, NR per-cell ASCII-table parsing,
numeric validation, and the SQLite writes — remain in code.

Run::

    python3 indexer.py <log_file> [db_path] [--append] [--config candidate.json]

The ``--dry-run`` flag runs the indexer against the whole log without writing
the events/NAS/RF tables, but always populates ``parser_health`` and
``parser_failures`` so the agent can diff candidate vs. production extraction
quality. ``--promote`` atomically replaces ``parser_config.json`` with the
candidate after a dry-run whose failure counts dropped.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from parser_config_schema import ConfigValidationError, load_config

DEFAULT_CONFIG_PATH = "parser_config.json"


# ---------------------------------------------------------------------------
# Config-aware parsing primitives
# ---------------------------------------------------------------------------


def _safe_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _range(value: float | None, lo: float, hi: float) -> bool:
    return value is not None and lo <= value <= hi


def decode_gprs_timer2(block_text: str, name: str) -> int | None:
    """Decode a GPRS Timer 2 IE (3GPP TS 24.008 10.5.7.4) into seconds.

    Returns ``None`` when the timer IE is absent (``<name>_incl = 0`` or no
    sub-block), which is semantically distinct from a zero-length timer.

    Expected QXDM shape::

        t3502_incl = 1 (0x1)
        t3502
          length = 1 (0x1)
          unit = 1 (0x1)
          timer_2_value = 12 (0xc)
    """
    incl = re.search(rf"{name}_incl\s*=\s*(\d+)", block_text)
    if incl and incl.group(1) == "0":
        return None

    sub = re.search(
        rf"^\s*{name}\s*$(.{{0,400}}?)timer_2_value\s*=\s*(\d+)",
        block_text,
        re.MULTILINE | re.DOTALL,
    )
    if not sub:
        return None

    value = int(sub.group(2))
    unit_match = re.search(r"unit\s*=\s*(\d+)", sub.group(1))
    unit = int(unit_match.group(1)) if unit_match else 0

    # TS 24.008 10.5.7.4 unit encoding for GPRS Timer 2.
    multipliers = {0: 2, 1: 60, 2: 360}
    if unit == 7:  # timer deactivated
        return 0
    return value * multipliers.get(unit, 2)


def _block_matches(parser_cfg: dict[str, Any], code_upper: str,
                   msg_type: str, block_upper: str, block_text: str) -> bool:
    """Decide whether a parser is responsible for this block."""
    match = parser_cfg["match"]
    if match.get("msg_codes_uppercase") and code_upper in match["msg_codes_uppercase"]:
        return True
    if match.get("msg_codes"):
        # Lowercase form is what QXDM emits natively.
        if any(code_upper == c.upper() for c in match["msg_codes"]):
            return True
    if match.get("text_tokens_any"):
        if any(tok.upper() in block_upper for tok in match["text_tokens_any"]):
            return True
    if match.get("text_token_regex"):
        # Pre-compiled regex on the upper-cased block text. Use this when you
        # need whole-word matching (e.g. "5GMM" / "EMM" surrounded by
        # non-uppercase delimiters) instead of plain substring containment.
        pattern = match["text_token_regex"]
        if isinstance(pattern, str):
            return re.search(pattern, block_upper) is not None
        return pattern.search(block_upper) is not None
    if match.get("msg_type_contains") and msg_type:
        if any(tok in msg_type for tok in match["msg_type_contains"]):
            return True
    if match.get("msg_type_starts_with") and msg_type:
        if msg_type.startswith(match["msg_type_starts_with"]):
            return True
    if match.get("block_text_contains"):
        if any(tok in block_text for tok in match["block_text_contains"]):
            return True
    return False


def _detect_nas_rat(parser_cfg: dict[str, Any], code_upper: str,
                    block_upper: str) -> str:
    rd = parser_cfg.get("rat_detection", {})
    if rd.get("nr_codes_uppercase") and code_upper in rd["nr_codes_uppercase"]:
        return rd.get("rat_nr", "5GNR")
    if rd.get("nr_text_tokens"):
        if any(tok.upper() in block_upper for tok in rd["nr_text_tokens"]):
            return rd.get("rat_nr", "5GNR")
    return rd.get("rat_lte", "LTE")


# ---------------------------------------------------------------------------
# Parser-result accumulator + parser-health metrics
# ---------------------------------------------------------------------------


@dataclass
class ParserStats:
    matched: int = 0
    parsed: int = 0
    failed: int = 0
    invalid_value: int = 0


@dataclass
class IndexerState:
    events: list[tuple] = field(default_factory=list)
    nas: list[tuple] = field(default_factory=list)
    rf: list[tuple] = field(default_factory=list)
    stats: dict[str, ParserStats] = field(
        default_factory=lambda: defaultdict(ParserStats)
    )
    # parser_name -> list of (msg_code, timestamp, failure_reason, sample_block)
    failure_samples: dict[str, list[tuple]] = field(default_factory=lambda: defaultdict(list))
    unknown_msg_codes: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    unknown_samples: dict[str, list[tuple]] = field(default_factory=lambda: defaultdict(list))
    total_blocks: int = 0


def _record_failure(state: IndexerState, parser_name: str, msg_code: str,
                    timestamp: str, reason: str, block_text: str,
                    max_samples: int, max_block_chars: int) -> None:
    """Store a representative failed block for the agent to inspect."""
    samples = state.failure_samples[parser_name]
    if len(samples) >= max_samples:
        return
    samples.append(
        (msg_code, timestamp, reason, block_text[:max_block_chars])
    )


def _record_unknown(state: IndexerState, msg_code: str, timestamp: str,
                    msg_type: str, block_text: str,
                    max_samples: int, max_block_chars: int) -> None:
    state.unknown_msg_codes[msg_code] += 1
    samples = state.unknown_samples[msg_code]
    if len(samples) >= max_samples:
        return
    samples.append((timestamp, msg_type, block_text[:max_block_chars]))


# ---------------------------------------------------------------------------
# Per-parser handlers
# ---------------------------------------------------------------------------


def _parse_nas(state: IndexerState, parser_cfg: dict[str, Any], ts: str, seq: int,
               code: str, code_upper: str, msg_type: str, block_text: str,
               block_upper: str) -> bool:
    rat = _detect_nas_rat(parser_cfg, code_upper, block_upper)
    extract = parser_cfg["extract"]
    cause_pat = extract["cause"]["pattern"]
    cause_spec = extract["cause"]["spec"]
    cause_match = cause_pat.search(block_text)
    cause_code = _safe_int(cause_match.group(1)) if cause_match else None
    if cause_match and cause_match.group(2):
        cause_str = cause_match.group(2).strip()
    else:
        cause_str = (
            cause_spec.get("fallback_cause_str", "UNKNOWN")
            if cause_code is not None
            else "UNKNOWN"
        )

    timers = extract["timers"]["spec"]["list"]
    timer_values = {name: decode_gprs_timer2(block_text, name) for name in timers}

    state.nas.append(
        (ts, seq, rat, msg_type, cause_code, cause_str,
         timer_values.get("t3346"), timer_values.get("t3502"), block_text)
    )

    summary_format = parser_cfg.get("emit_event", {}).get(
        "summary_format", "NAS [{rat}] Cause={cause_str}"
    )
    summary = summary_format.format(rat=rat, cause_str=cause_str)
    state.events.append((ts, seq, code, code, msg_type, summary, block_text))
    return True


def _parse_nr_searcher(state: IndexerState, parser_cfg: dict[str, Any], ts: str,
                       seq: int, code: str, msg_type: str,
                       block_text: str, validation: dict[str, Any],
                       max_block_chars: int) -> bool:
    extract = parser_cfg["extract"]
    arfcn_match = extract["arfcn"]["pattern"].search(block_text)
    arfcn_val = _safe_int(arfcn_match.group(1)) if arfcn_match else None
    arfcn_fallback = extract["arfcn"]["spec"].get("fallback_value")

    table_spec = extract["table_row"]["spec"]
    row_re = re.compile(
        table_spec["regex"],
        sum(getattr(re, f) for f in table_spec.get("flags", [])),
    )
    emitted = False
    rsrp_min = table_spec["validations"]["rsrp_min_dbm"]
    rsrp_max = table_spec["validations"]["rsrp_max_dbm"]
    col = table_spec["column_indexes"]
    rsrq_bounds = validation.get("rf", {})
    for row in row_re.finditer(block_text):
        cols = [c.strip() for c in row.group(0).split(table_spec["split_on"])]
        if len(cols) <= max(col["pci"], col["rsrp"], col["rsrq"]):
            continue
        pci_val = _safe_int(cols[col["pci"]])
        rsrp_val = _safe_float(cols[col["rsrp"]])
        rsrq_val = _safe_float(cols[col["rsrq"]])
        if pci_val is None or rsrp_val is None:
            continue
        if not _range(rsrp_val, rsrp_min, rsrp_max):
            state.stats["nr_searcher"].invalid_value += 1
            continue
        if rsrq_val is not None and rsrq_bounds:
            if not _range(rsrq_val, rsrq_bounds.get("rsrq_min_db", -30),
                          rsrq_bounds.get("rsrq_max_db", 10)):
                state.stats["nr_searcher"].invalid_value += 1
                continue
        state.rf.append(
            (ts, seq, "5GNR", arfcn_val, pci_val, rsrp_val, rsrq_val, None, None)
        )
        emitted = True

    if not emitted:
        fb = extract.get("fallback_kv", {})
        rsrp_pat = fb.get("rsrp", {}).get("pattern")
        rsrq_pat = fb.get("rsrq", {}).get("pattern")
        pci_pat = fb.get("pci", {}).get("pattern")
        ss_rsrp = rsrp_pat.search(block_text) if rsrp_pat else None
        if ss_rsrp:
            rsrp_val = _safe_float(ss_rsrp.group(1))
            rsrq_match = rsrq_pat.search(block_text) if rsrq_pat else None
            rsrq_val = _safe_float(rsrq_match.group(1)) if rsrq_match else None
            pci_match = pci_pat.search(block_text) if pci_pat else None
            pci_val = _safe_int(pci_match.group(1)) if pci_match else None
            if rsrp_val is not None and _range(rsrp_val, rsrp_min, rsrp_max):
                state.rf.append(
                    (ts, seq, "5GNR",
                     arfcn_val if arfcn_val is not None else arfcn_fallback,
                     pci_val, rsrp_val, rsrq_val, None, None)
                )
                emitted = True

    return emitted


def _parse_lte_rf(state: IndexerState, parser_cfg: dict[str, Any], ts: str,
                  seq: int, code: str, msg_type: str, block_text: str,
                  validation: dict[str, Any]) -> bool:
    extract = parser_cfg["extract"]
    rsrp_m = extract["rsrp"]["pattern"].search(block_text)
    rsrq_m = extract["rsrq"]["pattern"].search(block_text)
    if not (rsrp_m and rsrq_m):
        return False
    rsrp_val = _safe_float(rsrp_m.group(1))
    rsrq_val = _safe_float(rsrq_m.group(1))
    rssi_val = _safe_float(extract["rssi"]["pattern"].search(block_text).group(1)) \
        if extract["rssi"]["pattern"].search(block_text) else None
    snr_raw = extract["snr"]["pattern"].search(block_text)
    snr_val = _safe_float(snr_raw.group(1)) if snr_raw else None
    scale_thresh = extract["snr"]["spec"].get("scale_divide_by_10_when_abs_gt")
    if snr_val is not None and scale_thresh and abs(snr_val) > scale_thresh:
        snr_val /= 10.0

    bounds = validation.get("rf", {})
    if rsrp_val is None or not _range(rsrp_val,
                                      bounds.get("rsrp_min_dbm", -156),
                                      bounds.get("rsrp_max_dbm", -31)):
        state.stats["lte_rf"].invalid_value += 1
        return False
    if rsrq_val is None or not _range(rsrq_val,
                                      bounds.get("rsrq_min_db", -30),
                                      bounds.get("rsrq_max_db", 10)):
        state.stats["lte_rf"].invalid_value += 1
        return False

    state.rf.append(
        (ts, seq, "LTE", None, None, rsrp_val, rsrq_val, rssi_val, snr_val)
    )
    return True


def _parse_rrc_event(state: IndexerState, parser_cfg: dict[str, Any], ts: str,
                     seq: int, code: str, msg_type: str,
                     block_text: str) -> bool:
    payload_pat = parser_cfg["extract"].get("payload", {}).get("pattern")
    if payload_pat:
        m = payload_pat.search(block_text)
        summary = m.group(1).strip() if m else msg_type
    else:
        summary = msg_type
    state.events.append((ts, seq, code, code, msg_type, summary, block_text))
    return True


def _parse_rrc_state(state: IndexerState, ts: str, seq: int, code: str,
                     msg_type: str, block_text: str) -> bool:
    state.events.append((ts, seq, code, code, msg_type, msg_type, block_text))
    return True


# ---------------------------------------------------------------------------
# Database setup (events + parser-health schema)
# ---------------------------------------------------------------------------


def init_db(db_path: str = "qxdm_indexed.db", append: bool = False,
            keep_health: bool = True) -> sqlite3.Connection:
    db_file = Path(db_path)
    if not append and db_file.exists():
        db_file.unlink()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, sequence INTEGER, msg_code TEXT, subsys TEXT,
            msg_type TEXT, summary TEXT, raw_block TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nas_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, sequence INTEGER, rat TEXT, msg_id TEXT,
            cause_code INTEGER, cause_str TEXT,
            t3346_timer INTEGER, t3502_timer INTEGER, raw_block TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rf_kpis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, sequence INTEGER, rat TEXT, arfcn INTEGER,
            pci INTEGER, rsrp REAL, rsrq REAL, rssi REAL, snr REAL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_seq ON events(sequence)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(msg_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nas_ts ON nas_events(timestamp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rf_ts ON rf_kpis(timestamp)")

    # Parser health is rewritten on every run so the agent always sees
    # statistics for the most recent indexing pass.
    cur.execute("DROP TABLE IF EXISTS parser_health")
    cur.execute("DROP TABLE IF EXISTS parser_failures")
    cur.execute("DROP TABLE IF EXISTS unknown_msg_codes")
    cur.execute("""
        CREATE TABLE parser_health (
            parser_name TEXT PRIMARY KEY,
            matched INTEGER NOT NULL,
            parsed INTEGER NOT NULL,
            failed INTEGER NOT NULL,
            invalid_value INTEGER NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE parser_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parser_name TEXT, msg_code TEXT, timestamp TEXT,
            failure_reason TEXT, sample_block TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE unknown_msg_codes (
            msg_code TEXT PRIMARY KEY,
            occurrences INTEGER NOT NULL,
            sample_count INTEGER NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE unknown_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_code TEXT, timestamp TEXT, msg_type TEXT, sample_block TEXT
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Main parse loop
# ---------------------------------------------------------------------------


def parse_log(
    file_path: str,
    db_path: str = "qxdm_indexed.db",
    append: bool = False,
    config_path: str = DEFAULT_CONFIG_PATH,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Index ``file_path`` into ``db_path`` and return parser-health summary."""
    config = load_config(config_path)
    conn = init_db(db_path, append=append)
    cur = conn.cursor()

    start_seq = 0
    if append:
        row = cur.execute("SELECT MAX(sequence) FROM events").fetchone()
        if row and row[0] is not None:
            start_seq = row[0] + 1

    seq_num = start_seq
    current_ts = None
    current_code = None
    current_type = None
    current_seq = None
    current_block: list[str] = []

    state = IndexerState()
    sampling = config.get("failure_sampling", {}) or {}
    max_samples = int(sampling.get("max_samples_per_parser", 20))
    max_unknown = int(sampling.get("max_samples_per_unknown_code", 10))
    max_block_chars = int(sampling.get("max_block_size_chars", 4000))
    validation = config.get("validation", {})

    def flush_block() -> None:
        nonlocal current_block, current_ts, current_code, current_type, current_seq
        if not current_block or not current_ts:
            return
        block_text = "\n".join(current_block)
        code_upper = str(current_code).upper()
        block_upper = block_text.upper()
        state.total_blocks += 1

        matched_parser: str | None = None
        for parser in config["parsers"]:
            name = parser["name"]
            if _block_matches(parser, code_upper, current_type,
                              block_upper, block_text):
                state.stats[name].matched += 1
                matched_parser = name
                parsed_ok = False
                failure_reason = ""
                try:
                    if name in ("nas_5gmm", "nas_lte"):
                        parsed_ok = _parse_nas(
                            state, parser, current_ts, current_seq,
                            current_code, code_upper, current_type,
                            block_text, block_upper,
                        )
                        if not parsed_ok:
                            failure_reason = "cause regex missed"
                    elif name == "nr_searcher":
                        parsed_ok = _parse_nr_searcher(
                            state, parser, current_ts, current_seq,
                            current_code, current_type, block_text,
                            validation, max_block_chars,
                        )
                        if not parsed_ok:
                            failure_reason = "no per-cell rows and no key=value fallback"
                    elif name == "lte_rf":
                        parsed_ok = _parse_lte_rf(
                            state, parser, current_ts, current_seq,
                            current_code, current_type, block_text,
                            validation,
                        )
                        if not parsed_ok:
                            failure_reason = "missing rsrp/rsrq"
                    elif name == "rrc_event":
                        parsed_ok = _parse_rrc_event(
                            state, parser, current_ts, current_seq,
                            current_code, current_type, block_text,
                        )
                    elif name == "rrc_state":
                        parsed_ok = _parse_rrc_state(
                            state, current_ts, current_seq, current_code,
                            current_type, block_text,
                        )
                except Exception as exc:  # noqa: BLE001 — agent needs to see the sample
                    failure_reason = f"exception: {type(exc).__name__}: {exc}"
                    parsed_ok = False

                if parsed_ok:
                    state.stats[name].parsed += 1
                else:
                    state.stats[name].failed += 1
                    _record_failure(
                        state, name, current_code, current_ts,
                        failure_reason or "no extract produced",
                        block_text, max_samples, max_block_chars,
                    )
                break

        if matched_parser is None:
            _record_unknown(state, code_upper, current_ts, current_type,
                            block_text, max_unknown, max_block_chars)

    print(f"[*] Parsing log and building index: {file_path} -> {db_path}")
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line_str = line.rstrip()
            match = config["block_header_pattern"].match(line_str)
            if match:
                flush_block()
                current_ts = match.group(1)
                current_code = match.group(3)
                current_type = match.group(4).strip()
                current_seq = seq_num
                seq_num += 1
                current_block = [line_str]
            else:
                if current_block is not None:
                    current_block.append(line_str)
        flush_block()

    if not dry_run:
        cur.executemany(
            "INSERT INTO events (timestamp, sequence, msg_code, subsys, msg_type, summary, raw_block) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            state.events,
        )
        cur.executemany(
            "INSERT INTO nas_events (timestamp, sequence, rat, msg_id, cause_code, cause_str, t3346_timer, t3502_timer, raw_block) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            state.nas,
        )
        cur.executemany(
            "INSERT INTO rf_kpis (timestamp, sequence, rat, arfcn, pci, rsrp, rsrq, rssi, snr) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            state.rf,
        )

    # Parser-health tables are written even during dry-run so the agent can
    # diff candidate vs. production health without re-running an indexer.
    health_rows = [
        (name, st.matched, st.parsed, st.failed, st.invalid_value)
        for name, st in state.stats.items()
    ]
    if health_rows:
        cur.executemany(
            "INSERT INTO parser_health (parser_name, matched, parsed, failed, invalid_value) "
            "VALUES (?, ?, ?, ?, ?)",
            health_rows,
        )

    failure_rows: list[tuple] = []
    for parser_name, samples in state.failure_samples.items():
        for msg_code, ts, reason, sample in samples:
            failure_rows.append((parser_name, msg_code, ts, reason, sample))
    if failure_rows:
        cur.executemany(
            "INSERT INTO parser_failures (parser_name, msg_code, timestamp, failure_reason, sample_block) "
            "VALUES (?, ?, ?, ?, ?)",
            failure_rows,
        )

    unknown_rows = [
        (code, occ, len(state.unknown_samples[code]))
        for code, occ in state.unknown_msg_codes.items()
    ]
    if unknown_rows:
        cur.executemany(
            "INSERT INTO unknown_msg_codes (msg_code, occurrences, sample_count) "
            "VALUES (?, ?, ?)",
            unknown_rows,
        )

    unknown_sample_rows: list[tuple] = []
    for code, samples in state.unknown_samples.items():
        for ts, msg_type, sample in samples:
            unknown_sample_rows.append((code, ts, msg_type, sample))
    if unknown_sample_rows:
        cur.executemany(
            "INSERT INTO unknown_samples (msg_code, timestamp, msg_type, sample_block) "
            "VALUES (?, ?, ?, ?)",
            unknown_sample_rows,
        )

    conn.commit()
    conn.close()

    health = {
        "total_blocks": state.total_blocks,
        "parsers": {
            name: {
                "matched": st.matched,
                "parsed": st.parsed,
                "failed": st.failed,
                "invalid_value": st.invalid_value,
            }
            for name, st in state.stats.items()
        },
        "unknown_msg_codes": dict(state.unknown_msg_codes),
    }
    print(
        f"[✓] Indexed {len(state.events)} Events, {len(state.nas)} NAS Records, "
        f"and {len(state.rf)} RF Measurements. "
        f"({state.total_blocks} blocks scanned, dry_run={dry_run})"
    )
    return health


def diff_health(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Return a compact diff between two health payloads."""
    diff: dict[str, Any] = {"parsers": {}, "totals": {}}
    for name, post in after["parsers"].items():
        prev = before["parsers"].get(name, {})
        diff["parsers"][name] = {
            "matched_delta": post["matched"] - prev.get("matched", 0),
            "parsed_delta": post["parsed"] - prev.get("parsed", 0),
            "failed_delta": post["failed"] - prev.get("failed", 0),
            "invalid_value_delta": post["invalid_value"] - prev.get("invalid_value", 0),
            "parsed_after": post["parsed"],
            "failed_after": post["failed"],
        }
    diff["totals"] = {
        "matched_after": sum(p["matched"] for p in after["parsers"].values()),
        "parsed_after": sum(p["parsed"] for p in after["parsers"].values()),
        "failed_after": sum(p["failed"] for p in after["parsers"].values()),
    }
    return diff


def promote_candidate(candidate_path: str, target_path: str = DEFAULT_CONFIG_PATH) -> None:
    """Replace ``target_path`` with ``candidate_path`` once the candidate has
    been validated by dry-run."""
    src = Path(candidate_path)
    dst = Path(target_path)
    if not src.exists():
        raise FileNotFoundError(f"candidate config not found: {src}")
    # Validate before swapping so we don't leave the indexer pointing at a
    # broken config.
    load_config(src)
    shutil.copyfile(src, dst)
    print(f"[✓] Promoted {src} → {dst}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Parse QXDM log file into SQLite index.",
    )
    parser.add_argument("log_file", help="Path to QXDM decoded log text file")
    parser.add_argument(
        "db_path", nargs="?", default="qxdm_indexed.db",
        help="Target SQLite database path",
    )
    parser.add_argument(
        "--append", action="store_true",
        help="Append to existing database instead of overwriting",
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH,
        help="Path to parser_config.json (or candidate.json)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip events/nas/rf writes; still populate parser_health and "
             "parser_failures so the agent can diff against the production DB",
    )
    parser.add_argument(
        "--promote", action="store_true",
        help="After indexing, copy --config to parser_config.json "
             "(use after --dry-run has confirmed the candidate)",
    )

    args = parser.parse_args()
    try:
        health = parse_log(
            args.log_file, db_path=args.db_path, append=args.append,
            config_path=args.config, dry_run=args.dry_run,
        )
    except ConfigValidationError as exc:
        print(f"error: invalid parser config ({args.config}): {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if args.promote:
        promote_candidate(args.config)

    print(json.dumps(health, indent=2))