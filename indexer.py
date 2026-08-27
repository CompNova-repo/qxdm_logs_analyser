#!/usr/bin/env python3
import argparse
import re
import sqlite3
import sys
from pathlib import Path

def init_db(db_path="qxdm_indexed.db", append=False):
    db_file = Path(db_path)
    if not append and db_file.exists():
        db_file.unlink()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    # 1. Main signaling & OTA event table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            sequence INTEGER,
            msg_code TEXT,
            subsys TEXT,
            msg_type TEXT,
            summary TEXT,
            raw_block TEXT
        )
    """)
    
    # 2. NAS Signaling table (LTE & 5G NR registration / reject causes)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nas_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            sequence INTEGER,
            rat TEXT,
            msg_id TEXT,
            cause_code INTEGER,
            cause_str TEXT,
            t3346_timer INTEGER,
            t3502_timer INTEGER,
            raw_block TEXT
        )
    """)

    # 3. Radio Physical KPIs (LTE 0x1544 & NR 0xB97F searcher)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rf_kpis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            sequence INTEGER,
            rat TEXT,
            arfcn INTEGER,
            pci INTEGER,
            rsrp REAL,
            rsrq REAL,
            rssi REAL,
            snr REAL
        )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_seq ON events(sequence)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(msg_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nas_ts ON nas_events(timestamp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rf_ts ON rf_kpis(timestamp)")
    conn.commit()
    return conn

def decode_gprs_timer2(block_text, name):
    """Decode a GPRS Timer 2 IE (3GPP TS 24.008 10.5.7.4) into seconds.

    Returns None when the timer IE is absent (``<name>_incl = 0`` or no
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

    # Anchor on the timer's own sub-block so we never read a sibling's unit.
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


def parse_log(file_path, db_path="qxdm_indexed.db", append=False):
    conn = init_db(db_path, append=append)
    cur = conn.cursor()
    
    start_seq = 0
    if append:
        row = cur.execute("SELECT MAX(sequence) FROM events").fetchone()
        if row and row[0] is not None:
            start_seq = row[0] + 1
    
    block_header_re = re.compile(
        r"^(\d{4} \w{3} \s*\d{1,2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+\[([0-9A-Fa-f]+)\]\s+(0x[0-9A-Fa-f]+)\s+(.+)$"
    )
    
    seq_num = start_seq
    current_ts = None
    current_code = None
    current_type = None
    current_seq = None
    current_block = []
    
    records_events = []
    records_nas = []
    records_rf = []

    def flush_block():
        nonlocal current_block, current_ts, current_code, current_type, current_seq
        if not current_block or not current_ts:
            return
        block_text = "\n".join(current_block)
        code_str = str(current_code).upper()
        block_upper = block_text.upper()

        # NOTE: code_str is upper-cased, so every literal compared against it must
        # also be upper-case ("0XB80A", not "0xB80A"). Mixed-case literals silently
        # never match, which previously dropped all NR5G NAS reject blocks.
        NAS_CODES = ["0XB80A", "0XB0EC", "0XB0ED", "0XB814"]
        NR_SEARCHER_CODES = ["0XB97F"]

        # Match 5GMM/EMM as a whole token so "_5gmm_cause" / "EVENT_LTE_EMM_*" hit
        # but arbitrary substrings do not. Case-insensitive via block_upper.
        nas_text_hit = re.search(r"(?:^|[^A-Z])(5GMM|EMM)(?:[^A-Z]|$)", block_upper)

        # 1. NR5G NAS MM5G OTA (0xB80A) & LTE NAS (0xB0EC / 0xB0ED)
        if any(k in code_str for k in NAS_CODES) or nas_text_hit:
            is_nr = (
                any(k in code_str for k in ["0XB80A", "0XB814"])
                or "5GMM" in block_upper
                or "NR5G" in block_upper
            )
            rat = "5GNR" if is_nr else "LTE"
            # "_5gmm_cause = 22 (0x16) (Congestion)" -> code 22, str "Congestion".
            # The optional hex group must be consumed explicitly, otherwise the
            # human-readable cause capture lands on "0x16".
            cause_match = re.search(
                r"(?:_5gmm_cause|emm_cause|cause_val)\s*=\s*(\d+)"
                r"(?:\s*\(0[xX][0-9A-Fa-f]+\))?"
                r"(?:\s*\(([^)]*)\))?",
                block_text,
                re.IGNORECASE,
            )

            cause_code = int(cause_match.group(1)) if cause_match else None
            if cause_match and cause_match.group(2):
                cause_str = cause_match.group(2).strip()
            else:
                cause_str = str(cause_code) if cause_code is not None else "UNKNOWN"

            # Store the decoded back-off duration in seconds, or NULL when the IE
            # is absent. NULL therefore means "network omitted the timer".
            t3346_val = decode_gprs_timer2(block_text, "t3346")
            t3502_val = decode_gprs_timer2(block_text, "t3502")

            records_nas.append((current_ts, current_seq, rat, current_type, cause_code, cause_str, t3346_val, t3502_val, block_text))
            records_events.append((current_ts, current_seq, current_code, current_code, current_type, f"NAS [{rat}] Cause={cause_str}", block_text))

        # 2. NR5G Searcher / ML1 Measurements (0xB97F)
        elif any(k in code_str for k in NR_SEARCHER_CODES) or "NR5G ML1 SEARCHER" in block_upper:
            arfcn = re.search(r"(?:Raster\s+ARFCN|arfcn|carrier_freq)\s*=\s*(\d+)", block_text, re.IGNORECASE)
            arfcn_val = int(arfcn.group(1)) if arfcn else None

            # 0xB97F reports per-cell quality as an ASCII table, not key=value:
            #   |#  |PCI   |SFN   |Beams|RSRP        |RSRQ        |...
            #   |  0|   323|     2|    1|     -78.359|     -10.344|...
            emitted = False
            for row in re.finditer(r"^\s*\|\s*\d+\|.*$", block_text, re.MULTILINE):
                cols = [c.strip() for c in row.group(0).split("|")]
                # cols[1]=#, [2]=PCI, [3]=SFN, [4]=NumBeams, [5]=RSRP, [6]=RSRQ
                if len(cols) < 7:
                    continue
                try:
                    pci_val = int(cols[2])
                    rsrp_val = float(cols[5])
                except ValueError:
                    continue
                try:
                    rsrq_val = float(cols[6])
                except ValueError:
                    rsrq_val = None
                # Skip zero-filled placeholder rows for cells that were listed but
                # not actually measured. Valid SS-RSRP is -156..-31 dBm
                # (3GPP TS 38.133 Table 10.1.6.1-1).
                if not (-156.0 <= rsrp_val <= -31.0):
                    continue
                records_rf.append((
                    current_ts, current_seq, "5GNR",
                    arfcn_val, pci_val, rsrp_val, rsrq_val, None, None,
                ))
                emitted = True

            # Fall back to key=value form for other NR measurement layouts.
            if not emitted:
                ss_rsrp = re.search(r"(?:ss_rsrp|rsrp)\s*=\s*(-?\d+(?:\.\d+)?)", block_text, re.IGNORECASE)
                ss_rsrq = re.search(r"(?:ss_rsrq|rsrq)\s*=\s*(-?\d+(?:\.\d+)?)", block_text, re.IGNORECASE)
                pci = re.search(r"pci\s*=\s*(\d+)", block_text, re.IGNORECASE)
                if ss_rsrp:
                    records_rf.append((
                        current_ts,
                        current_seq,
                        "5GNR",
                        arfcn_val if arfcn_val is not None else 126490,
                        int(pci.group(1)) if pci else None,
                        float(ss_rsrp.group(1)),
                        float(ss_rsrq.group(1)) if ss_rsrq else None,
                        None,
                        None
                    ))

        # 3. LTE RF / NAS Signal Info (0x1544)
        elif "nas_sig_info" in block_text or "lte_sig_info" in block_text:
            rsrp = re.search(r"rsrp\s*=\s*(-?\d+(?:\.\d+)?)", block_text)
            rsrq = re.search(r"rsrq\s*=\s*(-?\d+(?:\.\d+)?)", block_text)
            rssi = re.search(r"rssi\s*=\s*(-?\d+(?:\.\d+)?)", block_text)
            snr = re.search(r"snr\s*=\s*(-?\d+(?:\.\d+)?)", block_text)
            
            if rsrp and rsrq:
                snr_val = float(snr.group(1)) if snr else None
                if snr_val and abs(snr_val) > 100:
                    snr_val /= 10.0
                records_rf.append((
                    current_ts,
                    current_seq,
                    "LTE",
                    None,
                    None,
                    float(rsrp.group(1)), 
                    float(rsrq.group(1)), 
                    float(rssi.group(1)) if rssi else None, 
                    snr_val
                ))

        # 4. Signaling Events & RRC Messages (0x1FFB, 0xB821, 0xB0C0)
        elif any(k in code_str for k in ["0X1FFB", "0XB821", "0XB0C0"]) or "Event" in str(current_type):
            event_match = re.search(r"Payload String = (.*)", block_text)
            summary = event_match.group(1) if event_match else current_type
            records_events.append((current_ts, current_seq, current_code, current_code, current_type, summary, block_text))
            
        elif "CM State Info" in str(current_type) or "RRC" in block_text:
            records_events.append((current_ts, current_seq, current_code, current_code, current_type, current_type, block_text))

    print(f"[*] Parsing log and building index: {file_path} -> {db_path}")
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line_str = line.rstrip()
            match = block_header_re.match(line_str)
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

    cur.executemany("INSERT INTO events (timestamp, sequence, msg_code, subsys, msg_type, summary, raw_block) VALUES (?, ?, ?, ?, ?, ?, ?)", records_events)
    cur.executemany("INSERT INTO nas_events (timestamp, sequence, rat, msg_id, cause_code, cause_str, t3346_timer, t3502_timer, raw_block) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", records_nas)
    cur.executemany("INSERT INTO rf_kpis (timestamp, sequence, rat, arfcn, pci, rsrp, rsrq, rssi, snr) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", records_rf)
    
    conn.commit()
    conn.close()
    print(f"[✓] Indexed {len(records_events)} Events, {len(records_nas)} NAS Records, and {len(records_rf)} RF Measurements.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parse QXDM log file into SQLite index.")
    parser.add_argument("log_file", help="Path to QXDM decoded log text file")
    parser.add_argument("db_path", nargs="?", default="qxdm_indexed.db", help="Target SQLite database path (default: qxdm_indexed.db)")
    parser.add_argument("--append", action="store_true", help="Append to existing database instead of overwriting")
    
    args = parser.parse_args()
    parse_log(args.log_file, db_path=args.db_path, append=args.append)

