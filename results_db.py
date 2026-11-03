#!/usr/bin/env python3
"""Persistent store for analysis results (RCAs, findings, evidence).

The evidence DB built by ``indexer.py`` is disposable: it can be rebuilt from
the decoded logs at any time and changes whenever the parser changes. This
module keeps what was *concluded* from it, in a separate SQLite file, so
results survive a re-index and can be reviewed, filtered and compared later.

Grounding rule, enforced on save
--------------------------------
Every finding needs at least one ``query`` evidence item. The agent supplies
only the ``qxdm_tool.py`` command (for example ``["nas", "--test-id", "UE23"]``)
and optional ``quote`` strings. ``save_analysis`` runs the command itself
against the evidence DB, stores the captured output and its SHA-256, and
rejects the whole analysis if any quote is not present verbatim in that
output. Agents cannot type evidence in by hand.

Saves are all-or-nothing: a rejected analysis writes nothing.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 1

ANALYSIS_STATUSES = ("draft", "reviewed", "accepted", "rejected", "superseded")
REVIEW_STATUSES = ("reviewed", "accepted", "rejected", "superseded")
FINDING_STATUSES = ("open", "fixed", "false_positive", "wont_fix")
LAYERS = ("core", "ran", "rf", "device", "transport", "tooling", "other")
CONFIDENCE = ("high", "medium", "low")
PRIORITIES = ("p1", "p2", "p3", "p4")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per distinct state of a test run's evidence index. Re-indexing with
-- a different parser produces different counts, hence a new run row, so every
-- analysis stays tied to the exact index it was derived from.
CREATE TABLE IF NOT EXISTS runs (
    run_id               INTEGER PRIMARY KEY,
    test_id              TEXT NOT NULL,
    index_fingerprint    TEXT NOT NULL,
    evidence_db_path     TEXT NOT NULL,
    source_files         TEXT NOT NULL,          -- JSON list
    chunk_count          INTEGER NOT NULL,
    first_timestamp      TEXT,
    last_timestamp       TEXT,
    event_count          INTEGER NOT NULL,
    nas_count            INTEGER NOT NULL,
    rf_count             INTEGER NOT NULL,
    parser_config_sha256 TEXT,
    indexer_commit       TEXT,
    first_seen_at        TEXT NOT NULL,
    UNIQUE (test_id, index_fingerprint)
);

CREATE TABLE IF NOT EXISTS analyses (
    analysis_id      TEXT PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES runs(run_id),
    test_id          TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    title            TEXT NOT NULL,
    verdict          TEXT NOT NULL,
    severity         TEXT,
    primary_owner    TEXT,
    summary          TEXT NOT NULL,
    report_markdown  TEXT NOT NULL,
    agent_name       TEXT,
    agent_model      TEXT,
    prompt_version   TEXT,
    status           TEXT NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft','reviewed','accepted','rejected','superseded')),
    reviewer         TEXT,
    reviewed_at      TEXT,
    review_note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_analyses_test ON analyses(test_id, created_at);

CREATE TABLE IF NOT EXISTS findings (
    finding_id     INTEGER PRIMARY KEY,
    analysis_id    TEXT NOT NULL REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    code           TEXT NOT NULL,
    title          TEXT NOT NULL,
    layer          TEXT NOT NULL
                   CHECK (layer IN ('core','ran','rf','device','transport','tooling','other')),
    statement      TEXT NOT NULL,
    spec_reference TEXT,
    confidence     TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    grounded       INTEGER NOT NULL CHECK (grounded IN (0, 1)),
    status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','fixed','false_positive','wont_fix')),
    UNIQUE (analysis_id, code)
);
CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status, layer);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id   INTEGER PRIMARY KEY,
    finding_id    INTEGER NOT NULL REFERENCES findings(finding_id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('query', 'note')),
    command       TEXT,           -- JSON list, query evidence only
    output_text   TEXT,           -- captured by save_analysis, never by the agent
    output_sha256 TEXT,
    quotes        TEXT,           -- JSON list of strings verified in output_text
    note          TEXT,
    source        TEXT,           -- where a note came from (e.g. raw log line)
    captured_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendations (
    recommendation_id INTEGER PRIMARY KEY,
    finding_id        INTEGER NOT NULL REFERENCES findings(finding_id) ON DELETE CASCADE,
    action            TEXT NOT NULL,
    owner             TEXT,
    priority          TEXT CHECK (priority IS NULL OR priority IN ('p1','p2','p3','p4')),
    status            TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','done','dropped'))
);

CREATE TABLE IF NOT EXISTS metrics (
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    value       REAL NOT NULL,
    unit        TEXT,
    PRIMARY KEY (analysis_id, name)
);
"""

_ANALYSIS_KEYS = {
    "test_id", "title", "verdict", "severity", "primary_owner", "summary",
    "report_markdown", "report_file", "agent", "findings", "metrics",
}
_FINDING_KEYS = {
    "code", "title", "layer", "statement", "spec_reference", "confidence",
    "evidence", "recommendations",
}
_QUERY_EVIDENCE_KEYS = {"kind", "command", "quote", "note"}
_NOTE_EVIDENCE_KEYS = {"kind", "note", "source"}


class AnalysisRejected(ValueError):
    """Raised when an analysis fails validation; nothing is written."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


QueryRunner = Callable[[str, list], str]


def _now() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(65536), b""):
            h.update(buf)
    return h.hexdigest()


def connect(results_db: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the results DB with foreign keys enforced."""
    conn = sqlite3.connect(str(results_db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif int(row["value"]) > SCHEMA_VERSION:
        conn.close()
        raise ValueError(
            f"results DB schema v{row['value']} is newer than this tool (v{SCHEMA_VERSION})"
        )
    return conn


# ---------------------------------------------------------------------------
# Validation (pure, no I/O)
# ---------------------------------------------------------------------------


def _check_keys(obj: dict, allowed: set, where: str, errors: list[str]) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        errors.append(f"{where}: unknown field(s) {unknown}")


def _need_str(obj: dict, key: str, where: str, errors: list[str], required: bool = True) -> None:
    val = obj.get(key)
    if val is None:
        if required:
            errors.append(f"{where}: '{key}' is required")
    elif not isinstance(val, str) or not val.strip():
        errors.append(f"{where}: '{key}' must be a non-empty string")


def validate_analysis(payload: Any, allow_ungrounded: bool = False) -> list[str]:
    """Return a list of problems with ``payload``; empty means structurally valid."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["analysis must be a JSON object"]
    _check_keys(payload, _ANALYSIS_KEYS, "analysis", errors)
    for key in ("test_id", "title", "verdict", "summary"):
        _need_str(payload, key, "analysis", errors)
    for key in ("severity", "primary_owner"):
        _need_str(payload, key, "analysis", errors, required=False)
    if bool(payload.get("report_markdown")) == bool(payload.get("report_file")):
        errors.append("analysis: give exactly one of 'report_markdown' or 'report_file'")
    agent = payload.get("agent", {})
    if not isinstance(agent, dict):
        errors.append("analysis: 'agent' must be an object")

    findings = payload.get("findings")
    if not isinstance(findings, list) or not findings:
        errors.append("analysis: 'findings' must be a non-empty list")
        findings = []
    codes: set = set()
    for i, f in enumerate(findings):
        where = f"findings[{i}]"
        if not isinstance(f, dict):
            errors.append(f"{where}: must be an object")
            continue
        where = f"finding {f.get('code', i)!s}"
        _check_keys(f, _FINDING_KEYS, where, errors)
        for key in ("code", "title", "statement"):
            _need_str(f, key, where, errors)
        _need_str(f, "spec_reference", where, errors, required=False)
        if f.get("code") in codes:
            errors.append(f"{where}: duplicate finding code")
        codes.add(f.get("code"))
        if f.get("layer") not in LAYERS:
            errors.append(f"{where}: 'layer' must be one of {list(LAYERS)}")
        if f.get("confidence") not in CONFIDENCE:
            errors.append(f"{where}: 'confidence' must be one of {list(CONFIDENCE)}")

        evidence = f.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{where}: needs at least one evidence item")
            evidence = []
        n_query = 0
        for j, ev in enumerate(evidence):
            ew = f"{where} evidence[{j}]"
            if not isinstance(ev, dict):
                errors.append(f"{ew}: must be an object")
                continue
            kind = ev.get("kind")
            if kind == "query":
                n_query += 1
                _check_keys(ev, _QUERY_EVIDENCE_KEYS, ew, errors)
                cmd = ev.get("command")
                if not (isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd)):
                    errors.append(f"{ew}: 'command' must be a non-empty list of strings")
                quote = ev.get("quote", [])
                if not (isinstance(quote, list) and all(isinstance(q, str) and q for q in quote)):
                    errors.append(f"{ew}: 'quote' must be a list of non-empty strings")
            elif kind == "note":
                _check_keys(ev, _NOTE_EVIDENCE_KEYS, ew, errors)
                _need_str(ev, "note", ew, errors)
                _need_str(ev, "source", ew, errors)
            else:
                errors.append(f"{ew}: 'kind' must be 'query' or 'note'")
        if evidence and n_query == 0 and not allow_ungrounded:
            errors.append(
                f"{where}: has no query evidence; every finding must trace to a "
                "qxdm_tool query (use --allow-ungrounded to store it flagged)"
            )

        recs = f.get("recommendations", [])
        if not isinstance(recs, list):
            errors.append(f"{where}: 'recommendations' must be a list")
            recs = []
        for k, r in enumerate(recs):
            rw = f"{where} recommendations[{k}]"
            if not isinstance(r, dict):
                errors.append(f"{rw}: must be an object")
                continue
            _check_keys(r, {"action", "owner", "priority"}, rw, errors)
            _need_str(r, "action", rw, errors)
            if r.get("priority") is not None and r.get("priority") not in PRIORITIES:
                errors.append(f"{rw}: 'priority' must be one of {list(PRIORITIES)}")

    metrics = payload.get("metrics", [])
    if not isinstance(metrics, list):
        errors.append("analysis: 'metrics' must be a list")
        metrics = []
    names: set = set()
    for i, m in enumerate(metrics):
        mw = f"metrics[{i}]"
        if not isinstance(m, dict):
            errors.append(f"{mw}: must be an object")
            continue
        _check_keys(m, {"name", "value", "unit"}, mw, errors)
        _need_str(m, "name", mw, errors)
        if m.get("name") in names:
            errors.append(f"{mw}: duplicate metric name")
        names.add(m.get("name"))
        if isinstance(m.get("value"), bool) or not isinstance(m.get("value"), (int, float)):
            errors.append(f"{mw}: 'value' must be a number")
    return errors


# ---------------------------------------------------------------------------
# Evidence DB fingerprint
# ---------------------------------------------------------------------------


def _git_commit(directory: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=directory,
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def describe_run(evidence_db: str | Path, test_id: str) -> dict[str, Any]:
    """Summarise ``test_id`` in the evidence DB and fingerprint that state."""
    if not Path(evidence_db).exists():
        raise FileNotFoundError(f"evidence DB not found: {evidence_db}")
    conn = sqlite3.connect(str(evidence_db))
    try:
        counts = {}
        for table, key in (("events", "event_count"), ("nas_events", "nas_count"),
                           ("rf_kpis", "rf_count")):
            counts[key] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE test_id = ?", (test_id,)  # noqa: S608
            ).fetchone()[0]
        files = sorted({
            r[0] for table in ("events", "nas_events", "rf_kpis")
            for r in conn.execute(
                f"SELECT DISTINCT source_file FROM {table} WHERE test_id = ?",  # noqa: S608
                (test_id,),
            )
        })
        chunks = conn.execute(
            "SELECT COUNT(DISTINCT chunk_seq) FROM events WHERE test_id = ?", (test_id,)
        ).fetchone()[0]
        span = conn.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM events WHERE test_id = ?", (test_id,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"evidence DB has no multi-file provenance columns: {exc}") from None
    finally:
        conn.close()
    if not any(counts.values()):
        raise ValueError(f"test_id {test_id!r} has no rows in the evidence DB {evidence_db}")
    state = {
        "test_id": test_id, "source_files": files, "chunk_count": chunks,
        "first_timestamp": span[0], "last_timestamp": span[1], **counts,
    }
    state["index_fingerprint"] = _sha256_text(json.dumps(state, sort_keys=True))
    return state


def _upsert_run(conn: sqlite3.Connection, evidence_db: str | Path, state: dict,
                parser_config_sha256: str | None, indexer_commit: str | None) -> int:
    row = conn.execute(
        "SELECT run_id FROM runs WHERE test_id = ? AND index_fingerprint = ?",
        (state["test_id"], state["index_fingerprint"]),
    ).fetchone()
    if row:
        return int(row["run_id"])
    cur = conn.execute(
        """INSERT INTO runs (test_id, index_fingerprint, evidence_db_path, source_files,
               chunk_count, first_timestamp, last_timestamp, event_count, nas_count,
               rf_count, parser_config_sha256, indexer_commit, first_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (state["test_id"], state["index_fingerprint"], str(Path(evidence_db).resolve()),
         json.dumps(state["source_files"]), state["chunk_count"], state["first_timestamp"],
         state["last_timestamp"], state["event_count"], state["nas_count"],
         state["rf_count"], parser_config_sha256, indexer_commit, _now()),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def _default_runner() -> QueryRunner:
    import qxdm_tool

    return qxdm_tool.run_query


def save_analysis(
    results_db: str | Path,
    evidence_db: str | Path,
    payload: dict,
    *,
    base_dir: str | Path | None = None,
    allow_ungrounded: bool = False,
    parser_config: str | Path | None = None,
    indexer_commit: str | None = None,
    query_runner: QueryRunner | None = None,
) -> str:
    """Validate ``payload``, capture its evidence, and store it. Returns the id.

    Raises :class:`AnalysisRejected` with every problem found; nothing is
    written in that case.
    """
    errors = validate_analysis(payload, allow_ungrounded=allow_ungrounded)
    if errors:
        raise AnalysisRejected(errors)

    import qxdm_tool

    runner = query_runner or _default_runner()
    test_id = payload["test_id"]
    base = Path(base_dir) if base_dir else Path.cwd()

    if payload.get("report_file"):
        report_path = (base / payload["report_file"]).resolve()
        if not report_path.is_file():
            raise AnalysisRejected([f"analysis: report_file not found: {report_path}"])
        report = report_path.read_text(encoding="utf-8")
    else:
        report = payload["report_markdown"]

    try:
        state = describe_run(evidence_db, test_id)
    except (FileNotFoundError, ValueError) as exc:
        raise AnalysisRejected([str(exc)]) from None

    # Capture every query once, before any write, so a bad command or a
    # missing quote rejects the analysis without leaving partial rows.
    captured: dict[tuple, str] = {}
    for f in payload["findings"]:
        for j, ev in enumerate(f["evidence"]):
            if ev["kind"] != "query":
                continue
            where = f"finding {f['code']} evidence[{j}]"
            cmd = ev["command"]
            if cmd[0] in qxdm_tool.TEST_SCOPED_COMMANDS:
                scoped = [cmd[k + 1] for k, a in enumerate(cmd[:-1]) if a == "--test-id"]
                if scoped != [test_id]:
                    errors.append(f"{where}: must be scoped with --test-id {test_id}")
                    continue
            key = tuple(cmd)
            if key not in captured:
                try:
                    captured[key] = runner(str(evidence_db), list(cmd))
                except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
                    errors.append(f"{where}: {exc}")
                    continue
            output = captured[key]
            if output.startswith("No indexed event found"):
                errors.append(f"{where}: query returned no data: {output.strip()}")
            for q in ev.get("quote", []):
                if q not in output:
                    errors.append(f"{where}: quote not found in captured output: {q!r}")
    if errors:
        raise AnalysisRejected(errors)

    config_sha = None
    if parser_config:
        config_sha = _sha256_file(Path(parser_config))
    else:
        default_cfg = Path(__file__).resolve().parent / "parser_config.json"
        if default_cfg.is_file():
            config_sha = _sha256_file(default_cfg)
    commit = indexer_commit or _git_commit(Path(__file__).resolve().parent)

    analysis_id = uuid.uuid4().hex[:12]
    agent = payload.get("agent", {})
    now = _now()
    conn = connect(results_db)
    try:
        with conn:
            run_id = _upsert_run(conn, evidence_db, state, config_sha, commit)
            conn.execute(
                """INSERT INTO analyses (analysis_id, run_id, test_id, created_at, title,
                       verdict, severity, primary_owner, summary, report_markdown,
                       agent_name, agent_model, prompt_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (analysis_id, run_id, test_id, now, payload["title"], payload["verdict"],
                 payload.get("severity"), payload.get("primary_owner"), payload["summary"],
                 report, agent.get("name"), agent.get("model"), agent.get("prompt_version")),
            )
            for f in payload["findings"]:
                grounded = any(ev["kind"] == "query" for ev in f["evidence"])
                cur = conn.execute(
                    """INSERT INTO findings (analysis_id, code, title, layer, statement,
                           spec_reference, confidence, grounded)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (analysis_id, f["code"], f["title"], f["layer"], f["statement"],
                     f.get("spec_reference"), f["confidence"], int(grounded)),
                )
                finding_id = cur.lastrowid
                for ev in f["evidence"]:
                    if ev["kind"] == "query":
                        out = captured[tuple(ev["command"])]
                        conn.execute(
                            """INSERT INTO evidence (finding_id, kind, command, output_text,
                                   output_sha256, quotes, note, captured_at)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (finding_id, "query", json.dumps(ev["command"]), out,
                             _sha256_text(out), json.dumps(ev.get("quote", [])),
                             ev.get("note"), now),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO evidence (finding_id, kind, note, source, captured_at)
                               VALUES (?,?,?,?,?)""",
                            (finding_id, "note", ev["note"], ev["source"], now),
                        )
                for r in f.get("recommendations", []):
                    conn.execute(
                        """INSERT INTO recommendations (finding_id, action, owner, priority)
                           VALUES (?,?,?,?)""",
                        (finding_id, r["action"], r.get("owner"), r.get("priority")),
                    )
            for m in payload.get("metrics", []):
                conn.execute(
                    "INSERT INTO metrics (analysis_id, name, value, unit) VALUES (?,?,?,?)",
                    (analysis_id, m["name"], float(m["value"]), m.get("unit")),
                )
    finally:
        conn.close()
    return analysis_id


# ---------------------------------------------------------------------------
# Read / review
# ---------------------------------------------------------------------------


def _where(clauses: Iterable[tuple[str, Any]]) -> tuple[str, list]:
    parts, params = [], []
    for sql, val in clauses:
        if val is not None:
            parts.append(sql)
            params.append(val)
    return (" WHERE " + " AND ".join(parts)) if parts else "", params


def list_analyses(results_db: str | Path, test_id: str | None = None,
                  status: str | None = None) -> list[dict[str, Any]]:
    conn = connect(results_db)
    try:
        where, params = _where([("a.test_id = ?", test_id), ("a.status = ?", status)])
        rows = conn.execute(
            f"""SELECT a.analysis_id, a.test_id, a.created_at, a.title, a.verdict,
                       a.severity, a.primary_owner, a.status, a.reviewer,
                       r.run_id, r.index_fingerprint,
                       (SELECT COUNT(*) FROM findings f WHERE f.analysis_id = a.analysis_id)
                           AS finding_count,
                       (SELECT COUNT(*) FROM findings f WHERE f.analysis_id = a.analysis_id
                           AND f.grounded = 0) AS ungrounded_count
                FROM analyses a JOIN runs r ON r.run_id = a.run_id
                {where} ORDER BY a.created_at DESC, a.analysis_id""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_analysis(results_db: str | Path, analysis_id: str) -> dict[str, Any]:
    conn = connect(results_db)
    try:
        a = conn.execute("SELECT * FROM analyses WHERE analysis_id = ?", (analysis_id,)).fetchone()
        if a is None:
            raise KeyError(f"no analysis with id {analysis_id!r}")
        run = dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (a["run_id"],)).fetchone())
        run["source_files"] = json.loads(run["source_files"])
        findings = []
        for f in conn.execute(
            "SELECT * FROM findings WHERE analysis_id = ? ORDER BY finding_id", (analysis_id,)
        ):
            fd = dict(f)
            fd["grounded"] = bool(fd["grounded"])
            fd["evidence"] = []
            for e in conn.execute(
                "SELECT * FROM evidence WHERE finding_id = ? ORDER BY evidence_id", (f["finding_id"],)
            ):
                ed = {k: e[k] for k in e.keys() if e[k] is not None}
                for k in ("command", "quotes"):
                    if k in ed:
                        ed[k] = json.loads(ed[k])
                fd["evidence"].append(ed)
            fd["recommendations"] = [dict(r) for r in conn.execute(
                "SELECT recommendation_id, action, owner, priority, status FROM recommendations "
                "WHERE finding_id = ? ORDER BY recommendation_id", (f["finding_id"],))]
            findings.append(fd)
        metrics = [dict(m) for m in conn.execute(
            "SELECT name, value, unit FROM metrics WHERE analysis_id = ? ORDER BY name",
            (analysis_id,))]
        out = {k: a[k] for k in a.keys() if k not in ("reviewer", "reviewed_at", "review_note")}
        out["review"] = {"status": a["status"], "reviewer": a["reviewer"],
                         "reviewed_at": a["reviewed_at"], "note": a["review_note"]}
        out["run"] = run
        out["findings"] = findings
        out["metrics"] = metrics
        return out
    finally:
        conn.close()


def review_analysis(results_db: str | Path, analysis_id: str, status: str,
                    reviewer: str, note: str = "") -> None:
    if status not in REVIEW_STATUSES:
        raise ValueError(f"review status must be one of {list(REVIEW_STATUSES)}")
    if not reviewer.strip():
        raise ValueError("reviewer is required")
    conn = connect(results_db)
    try:
        with conn:
            cur = conn.execute(
                """UPDATE analyses SET status = ?, reviewer = ?, reviewed_at = ?, review_note = ?
                   WHERE analysis_id = ?""",
                (status, reviewer.strip(), _now(), note, analysis_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no analysis with id {analysis_id!r}")
    finally:
        conn.close()


def list_findings(results_db: str | Path, test_id: str | None = None,
                  status: str | None = None, layer: str | None = None,
                  code: str | None = None) -> list[dict[str, Any]]:
    conn = connect(results_db)
    try:
        where, params = _where([("a.test_id = ?", test_id), ("f.status = ?", status),
                                ("f.layer = ?", layer), ("f.code = ?", code)])
        rows = conn.execute(
            f"""SELECT f.finding_id, a.test_id, f.analysis_id, a.status AS analysis_status,
                       f.code, f.title, f.layer, f.confidence, f.grounded, f.status,
                       f.spec_reference, a.created_at
                FROM findings f JOIN analyses a ON a.analysis_id = f.analysis_id
                {where} ORDER BY a.created_at DESC, f.finding_id""",
            params,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["grounded"] = bool(d["grounded"])
            out.append(d)
        return out
    finally:
        conn.close()


def set_finding_status(results_db: str | Path, finding_id: int, status: str) -> None:
    if status not in FINDING_STATUSES:
        raise ValueError(f"finding status must be one of {list(FINDING_STATUSES)}")
    conn = connect(results_db)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE findings SET status = ? WHERE finding_id = ?", (status, finding_id))
            if cur.rowcount == 0:
                raise KeyError(f"no finding with id {finding_id}")
    finally:
        conn.close()
