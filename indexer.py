#!/usr/bin/env python3
"""Stream a decoded QXDM/QCAT text log into a compact SQLite index."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Optional


DEFAULT_DB_PATH = "qxdm_indexed.db"

BLOCK_HEADER_RE = re.compile(
    r"^(\d{4}\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"\[([0-9A-Fa-f]+)\]\s+(0x[0-9A-Fa-f]+)\s+(.+)$"
)

RF_PATTERNS = {
    "rsrp": re.compile(r"\brsrp\s*=\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE),
    "rsrq": re.compile(r"\brsrq\s*=\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE),
    "rssi": re.compile(r"\brssi\s*=\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE),
    "snr": re.compile(r"\b(?:snr|sinr)\s*=\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE),
}

IMPORTANT_EVENT_TERMS = (
    "Event",
    "RRC",
    "CM State Info",
    "State = Closing",
    "Release",
    "RLF",
    "Fail",
    "SCELL_STATE",
)

DEVICE_INFO_TERMS = (
    "SM_LAHAINA",
    "Build ID",
    "IMEI",
    "IMSI",
    "SIM",
    "SYS_MODE",
)


def init_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            subsys TEXT,
            msg_code TEXT,
            msg_type TEXT,
            summary TEXT,
            raw_block TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rf_kpis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            rsrp REAL,
            rsrq REAL,
            rssi REAL,
            snr REAL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_seq ON events(sequence)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(msg_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rf_ts ON rf_kpis(timestamp)")
    conn.commit()
    return conn


def normalize_rf_value(metric: str, raw_value: Optional[float]) -> Optional[float]:
    if raw_value is None:
        return None

    # QXDM mixes direct dB/dBm values with tenths of dB/dBm values depending on
    # the log packet family. Keep direct values and scale only impossible ones.
    scale_thresholds = {
        "rsrp": 200,
        "rsrq": 50,
        "rssi": 200,
        "snr": 60,
    }
    if abs(raw_value) > scale_thresholds[metric]:
        return raw_value / 10.0
    return raw_value


def search_float(pattern: re.Pattern[str], block_text: str) -> Optional[float]:
    match = pattern.search(block_text)
    if not match:
        return None
    return float(match.group(1))


def extract_payload_summary(block_text: str, fallback: str) -> str:
    payload_match = re.search(r"Payload String\s*=\s*(.*)", block_text)
    if payload_match:
        return payload_match.group(1).strip()

    lines = [line.strip() for line in block_text.splitlines() if line.strip()]
    for line in lines[1:]:
        if any(term in line for term in IMPORTANT_EVENT_TERMS):
            return line

    return fallback


def parse_log(file_path: str, db_path: str = DEFAULT_DB_PATH, append: bool = False) -> None:
    input_path = Path(file_path)
    output_path = Path(db_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Log file not found: {input_path}")

    if output_path.exists() and not append:
        output_path.unlink()

    conn = init_db(str(output_path))
    cur = conn.cursor()

    current_ts: Optional[str] = None
    current_subsys: Optional[str] = None
    current_code: Optional[str] = None
    current_type: Optional[str] = None
    current_block: list[str] = []
    sequence = 0

    records_events: list[tuple[str, int, Optional[str], Optional[str], Optional[str], str, str]] = []
    records_rf: list[tuple[str, int, Optional[float], Optional[float], Optional[float], Optional[float]]] = []
    metadata: dict[str, str] = {
        "source_file": str(input_path),
    }

    def flush_block() -> None:
        nonlocal sequence

        if not current_block or not current_ts:
            return

        sequence += 1
        block_text = "\n".join(current_block)
        msg_type = current_type or ""
        msg_code = current_code or ""

        rsrp = search_float(RF_PATTERNS["rsrp"], block_text)
        rsrq = search_float(RF_PATTERNS["rsrq"], block_text)
        rssi = search_float(RF_PATTERNS["rssi"], block_text)
        raw_snr = search_float(RF_PATTERNS["snr"], block_text)
        rsrp = normalize_rf_value("rsrp", rsrp)
        rsrq = normalize_rf_value("rsrq", rsrq)
        rssi = normalize_rf_value("rssi", rssi)
        snr = normalize_rf_value("snr", raw_snr)

        if rsrp is not None or rsrq is not None or rssi is not None or snr is not None:
            records_rf.append((current_ts, sequence, rsrp, rsrq, rssi, snr))

        looks_like_event = (
            msg_code.upper() == "0X1FFB"
            or any(term in msg_type for term in IMPORTANT_EVENT_TERMS)
            or any(term in block_text for term in IMPORTANT_EVENT_TERMS)
        )
        looks_like_device_info = (
            msg_code.upper() in {"0X1FEA", "0X1FF0"}
            and any(term in block_text for term in DEVICE_INFO_TERMS)
        )

        if looks_like_event or looks_like_device_info:
            summary = extract_payload_summary(block_text, msg_type)
            records_events.append(
                (
                    current_ts,
                    sequence,
                    current_subsys,
                    current_code,
                    current_type,
                    summary,
                    block_text,
                )
            )

    print(f"[*] Processing {input_path} into {output_path}...")
    with input_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            line_str = line.rstrip()
            match = BLOCK_HEADER_RE.match(line_str)
            if match:
                flush_block()
                current_ts = " ".join(match.group(1).split())
                current_subsys = match.group(2)
                current_code = match.group(3)
                current_type = match.group(4).strip()
                current_block = [line_str]
            elif current_block:
                current_block.append(line_str)
        flush_block()

    cur.executemany(
        """
        INSERT INTO events (timestamp, sequence, subsys, msg_code, msg_type, summary, raw_block)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        records_events,
    )
    cur.executemany(
        """
        INSERT INTO rf_kpis (timestamp, sequence, rsrp, rsrq, rssi, snr)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        records_rf,
    )
    metadata["event_records"] = str(len(records_events))
    metadata["rf_records"] = str(len(records_rf))
    cur.executemany(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
        metadata.items(),
    )
    conn.commit()
    conn.close()

    print(
        json.dumps(
            {
                "status": "done",
                "db_path": str(output_path),
                "event_records": len(records_events),
                "rf_records": len(records_rf),
            },
            indent=2,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index a decoded QXDM/QCAT text log into SQLite."
    )
    parser.add_argument("log_file", help="Decoded QXDM/QCAT text log")
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_DB_PATH,
        help=f"SQLite DB path, default: {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to an existing DB instead of replacing it.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    parse_log(args.log_file, args.output, append=args.append)
