#!/usr/bin/env python3
"""Agent-facing helper for the candidate-config workflow.

The agent's loop looks like::

    1. Run ``indexer.py`` to populate ``parser_health`` and ``parser_failures``.
    2. ``qxdm_tool.py parser-failures`` -> read samples, edit
       ``parser_config.candidate.json``.
    3. ``promote_config.py validate <log> --candidate parser_config.candidate.json``
       -> dry-run the candidate and diff health against the production DB.
    4. If the diff looks healthy, ``promote_config.py promote --candidate ...``
       -> atomic replace over ``parser_config.json`` (tempfile +
       ``os.replace``; see ``parser_config_schema.atomic_write_text``).

This script is deliberately small. It exists so the agent doesn't have to
memorise the indexer CLI surface.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import indexer
from parser_config_schema import ConfigValidationError, atomic_write_text, load_config


def _read_health(db_path: str) -> dict[str, dict[str, int]]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        rows = cur.execute(
            "SELECT parser_name, matched, parsed, failed, invalid_value "
            "FROM parser_health"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        conn.close()
        raise SystemExit(
            f"error: {db_path} has no parser_health table — re-run indexer.py"
        ) from exc
    conn.close()
    return {
        row[0]: {
            "matched": row[1],
            "parsed": row[2],
            "failed": row[3],
            "invalid_value": row[4],
        }
        for row in rows
    }


def cmd_validate(args: argparse.Namespace) -> int:
    """Dry-run the candidate and diff parser-health against production."""
    if not Path(args.log).is_file():
        print(f"error: log file not found: {args.log}", file=sys.stderr)
        return 2
    try:
        load_config(args.candidate)
    except ConfigValidationError as exc:
        print(f"INVALID candidate: {exc}", file=sys.stderr)
        return 2

    baseline = _read_health(args.production_db)

    candidate_db = args.candidate_db
    print(
        f"[*] Dry-run with candidate: {args.candidate} -> {candidate_db}",
        file=sys.stderr,
    )
    indexer.parse_log(
        args.log,
        db_path=candidate_db,
        append=False,
        config_path=args.candidate,
        dry_run=True,
    )

    candidate_health = _read_health(candidate_db)

    diff = indexer.diff_health(baseline, candidate_health)

    regressions: list[str] = []
    for name in set(baseline) | set(candidate_health):
        prev = baseline.get(name, {"matched": 0, "parsed": 0, "failed": 0, "invalid_value": 0})
        post = candidate_health.get(name, {"matched": 0, "parsed": 0, "failed": 0, "invalid_value": 0})
        # Reject the candidate if any parser's failure count went UP or
        # parsed count went DOWN — the point of a candidate is to fail less
        # and parse more, not more of the same.
        # Also flag a dropped parser: any parser present in baseline but
        # absent in candidate indicates the candidate no longer extracts that
        # message type — always a regression, even if parsed was 0 (failed-only).
        if name not in candidate_health:
            regressions.append(
                f"{name}: parser dropped (baseline matched {prev['matched']}, "
                f"parsed {prev['parsed']}, failed {prev['failed']})"
            )
            continue
        failed_delta = diff[name]["failed_delta"]
        parsed_delta = diff[name]["parsed_delta"]
        if failed_delta > 0:
            regressions.append(
                f"{name}: failed {prev['failed']} -> {post['failed']} "
                f"(+{failed_delta})"
            )
        if parsed_delta < 0:
            regressions.append(
                f"{name}: parsed {prev['parsed']} -> {post['parsed']} "
                f"({parsed_delta})"
            )

    summary = {
        "candidate": str(Path(args.candidate).resolve()),
        "production_db": args.production_db,
        "candidate_db": candidate_db,
        "baseline": baseline,
        "candidate_health": candidate_health,
        "diff": diff,
        "regressions": regressions,
        "ok_to_promote": not regressions,
    }
    print(json.dumps(summary, indent=2))

    if regressions:
        print(
            f"[!] Candidate introduced regressions: {len(regressions)}",
            file=sys.stderr,
        )
        return 1
    print("[✓] Candidate is healthy enough to promote.", file=sys.stderr)
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """Validate, then atomically replace parser_config.json."""
    try:
        load_config(args.candidate)
    except ConfigValidationError as exc:
        print(f"INVALID candidate: {exc}", file=sys.stderr)
        return 2
    src = Path(args.candidate)
    dst = Path(args.target)
    if not src.exists():
        print(f"error: candidate not found: {src}", file=sys.stderr)
        return 1
    if src.resolve() == dst.resolve():
        # Avoid shutil.SameFileError when candidate and target resolve to
        # the same path (e.g. typos in --candidate / --target).
        print(f"[=] Candidate and target are the same file ({src}); nothing to do.")
        return 0
    # Atomic replace: write to a temp file in dst's directory, then
    # os.replace over dst. Avoids truncating parser_config.json if the
    # process is interrupted (Ctrl-C, ENOSPC) mid-copy.
    atomic_write_text(dst, Path(args.candidate).read_bytes())
    print(f"[✓] Promoted {src} -> {dst}", file=sys.stderr)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and promote parser_config candidates.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    val = sub.add_parser(
        "validate",
        help="Dry-run a candidate config and diff its parser-health against the production DB",
    )
    val.add_argument("log", help="Path to the QXDM decoded log")
    val.add_argument(
        "--candidate", required=True,
        help="Path to parser_config.candidate.json",
    )
    val.add_argument(
        "--production-db", default="qxdm_indexed.db",
        help="Existing SQLite DB whose parser_health is the baseline (default: qxdm_indexed.db)",
    )
    val.add_argument(
        "--candidate-db", default="qxdm_candidate.db",
        help="Scratch DB for the dry-run (default: qxdm_candidate.db)",
    )

    prom = sub.add_parser(
        "promote",
        help="Validate a candidate config and copy it over parser_config.json",
    )
    prom.add_argument(
        "--candidate", required=True,
        help="Path to parser_config.candidate.json",
    )
    prom.add_argument(
        "--target", default="parser_config.json",
        help="Destination config file (default: parser_config.json)",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "promote":
        return cmd_promote(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())