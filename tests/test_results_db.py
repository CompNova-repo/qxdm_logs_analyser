"""Tests for the results DB (stored analyses, findings and captured evidence).

    python3 -m pytest tests/test_results_db.py -v
"""

from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import indexer  # noqa: E402
import qxdm_tool  # noqa: E402
import results_db  # noqa: E402
from test_parser_sanity import SAMPLE_LOG  # noqa: E402

TEST_ID = "SAMPLE"


@pytest.fixture()
def evidence_db(tmp_path: Path) -> Path:
    log = tmp_path / "SAMPLE.txt"
    log.write_text(SAMPLE_LOG, encoding="utf-8")
    db = tmp_path / "evidence.db"
    indexer.parse_log(
        str(log), db_path=str(db), config_path=str(ROOT / "parser_config.json"),
        quiet_overlaps=True, test_id=TEST_ID, source_file=log.name, chunk_seq=1,
    )
    return db


@pytest.fixture()
def results(tmp_path: Path) -> Path:
    return tmp_path / "results.db"


def _payload() -> dict:
    return {
        "test_id": TEST_ID,
        "title": "Sample congestion reject",
        "verdict": "Core rejects with cause #22; RF fine.",
        "severity": "P2",
        "primary_owner": "Core",
        "summary": "One cause #22 reject with T3346 = 300 s.",
        "report_markdown": "# Sample\n\nOne reject.",
        "agent": {"name": "test", "model": "none", "prompt_version": "t1"},
        "findings": [
            {
                "code": "DEF-1",
                "title": "Congestion reject",
                "layer": "core",
                "statement": "Cause #22 with T3346 300 s.",
                "spec_reference": "TS 24.501",
                "confidence": "high",
                "evidence": [
                    {"kind": "query", "command": ["nas", "--test-id", TEST_ID],
                     "quote": ["\"cause_code\": 22", "\"t3346_timer\": 300"]},
                ],
                "recommendations": [{"action": "Check AMF load", "owner": "Core", "priority": "p2"}],
            },
            {
                "code": "RF-OK",
                "title": "RF fine",
                "layer": "rf",
                "statement": "Two LTE samples, good SNR.",
                "confidence": "medium",
                "evidence": [
                    {"kind": "query", "command": ["rf-summary", "--test-id", TEST_ID],
                     "quote": ["\"records\": 2"]},
                ],
            },
        ],
        "metrics": [{"name": "rejects", "value": 1, "unit": "count"}],
    }


def _row_count(db: Path, table: str) -> int:
    if not db.exists():
        return 0
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------------------
# Saving and reading back
# ---------------------------------------------------------------------------


def test_save_and_read_back(evidence_db: Path, results: Path) -> None:
    aid = results_db.save_analysis(results, evidence_db, _payload())
    listed = results_db.list_analyses(results, test_id=TEST_ID)
    assert [a["analysis_id"] for a in listed] == [aid]
    assert listed[0]["status"] == "draft"
    assert listed[0]["finding_count"] == 2 and listed[0]["ungrounded_count"] == 0

    a = results_db.get_analysis(results, aid)
    assert a["run"]["nas_count"] == 1 and a["run"]["rf_count"] == 2
    assert a["run"]["source_files"] == ["SAMPLE.txt"]
    assert a["report_markdown"].startswith("# Sample")
    assert {m["name"]: m["value"] for m in a["metrics"]} == {"rejects": 1.0}
    assert a["findings"][0]["recommendations"][0]["action"] == "Check AMF load"


def test_evidence_output_is_captured_by_the_tool(evidence_db: Path, results: Path) -> None:
    aid = results_db.save_analysis(results, evidence_db, _payload())
    ev = results_db.get_analysis(results, aid)["findings"][0]["evidence"][0]
    expected = qxdm_tool.run_query(str(evidence_db), ["nas", "--test-id", TEST_ID])
    assert ev["output_text"] == expected
    assert ev["output_sha256"] == results_db._sha256_text(expected)
    assert ev["command"] == ["nas", "--test-id", TEST_ID]


def test_agent_cannot_supply_evidence_output(evidence_db: Path, results: Path) -> None:
    p = _payload()
    p["findings"][0]["evidence"][0]["output"] = "[{\"cause_code\": 99}]"
    with pytest.raises(results_db.AnalysisRejected, match="unknown field"):
        results_db.save_analysis(results, evidence_db, p)


# ---------------------------------------------------------------------------
# Grounding rules
# ---------------------------------------------------------------------------


def test_quote_missing_from_output_rejects_everything(evidence_db: Path, results: Path) -> None:
    p = _payload()
    p["findings"][1]["evidence"][0]["quote"] = ["Raster ARFCN = 126490"]
    with pytest.raises(results_db.AnalysisRejected) as exc:
        results_db.save_analysis(results, evidence_db, p)
    assert any("quote not found" in e for e in exc.value.errors)
    # all-or-nothing: the valid first finding was not written either
    assert _row_count(results, "analyses") == 0
    assert _row_count(results, "findings") == 0


def test_finding_without_query_evidence_is_rejected(evidence_db: Path, results: Path) -> None:
    p = _payload()
    p["findings"][0]["evidence"] = [{"kind": "note", "note": "seen in raw log", "source": "line 1"}]
    with pytest.raises(results_db.AnalysisRejected, match="no query evidence"):
        results_db.save_analysis(results, evidence_db, p)


def test_allow_ungrounded_stores_flagged_finding(evidence_db: Path, results: Path) -> None:
    p = _payload()
    p["findings"][0]["evidence"] = [{"kind": "note", "note": "seen in raw log", "source": "line 1"}]
    aid = results_db.save_analysis(results, evidence_db, p, allow_ungrounded=True)
    f = results_db.get_analysis(results, aid)["findings"][0]
    assert f["grounded"] is False
    assert results_db.list_analyses(results)[0]["ungrounded_count"] == 1


@pytest.mark.parametrize("command, message", [
    (["save-analysis", "x.json"], "not an allowed evidence query"),
    (["nas", "--db", "/other.db", "--test-id", TEST_ID], "may not override --db"),
    (["nas", "--test-id", "OTHER"], "must be scoped with --test-id"),
    (["nas"], "must be scoped with --test-id"),
    (["window", "--test-id", TEST_ID], "exactly one of timestamp"),
])
def test_bad_evidence_commands_are_rejected(
    evidence_db: Path, results: Path, command: list, message: str,
) -> None:
    p = _payload()
    p["findings"][0]["evidence"][0] = {"kind": "query", "command": command}
    with pytest.raises(results_db.AnalysisRejected, match=message):
        results_db.save_analysis(results, evidence_db, p)


def test_empty_window_is_not_evidence(evidence_db: Path, results: Path) -> None:
    p = _payload()
    p["findings"][0]["evidence"][0] = {
        "kind": "query", "command": ["window", "--sequence", "999999", "--test-id", TEST_ID]}
    with pytest.raises(results_db.AnalysisRejected, match="returned no data"):
        results_db.save_analysis(results, evidence_db, p)


def test_unknown_test_id_is_rejected(evidence_db: Path, results: Path) -> None:
    p = _payload()
    p["test_id"] = "NOPE"
    for f in p["findings"]:
        for ev in f["evidence"]:
            ev["command"] = [a if a != TEST_ID else "NOPE" for a in ev["command"]]
    with pytest.raises(results_db.AnalysisRejected, match="no rows"):
        results_db.save_analysis(results, evidence_db, p)


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p.pop("verdict"), "'verdict' is required"),
    (lambda p: p.update(findings=[]), "non-empty list"),
    (lambda p: p["findings"][0].update(layer="kernel"), "'layer' must be one of"),
    (lambda p: p["findings"][0].update(confidence="certain"), "'confidence' must be one of"),
    (lambda p: p["findings"][1].update(code="DEF-1"), "duplicate finding code"),
    (lambda p: p.update(report_file="r.md"), "exactly one of 'report_markdown' or 'report_file'"),
    (lambda p: p["metrics"].append({"name": "x", "value": "7"}), "'value' must be a number"),
])
def test_schema_validation(evidence_db: Path, results: Path, mutate, message: str) -> None:
    p = copy.deepcopy(_payload())
    mutate(p)
    with pytest.raises(results_db.AnalysisRejected, match=message):
        results_db.save_analysis(results, evidence_db, p)


# ---------------------------------------------------------------------------
# Runs: tie each analysis to the exact index it came from
# ---------------------------------------------------------------------------


def test_same_index_reuses_run_reindex_creates_new_run(
    evidence_db: Path, results: Path, tmp_path: Path,
) -> None:
    a1 = results_db.save_analysis(results, evidence_db, _payload())
    a2 = results_db.save_analysis(results, evidence_db, _payload())
    r1 = results_db.get_analysis(results, a1)["run"]
    r2 = results_db.get_analysis(results, a2)["run"]
    assert r1["run_id"] == r2["run_id"]

    # Re-index with an extra RF sample: different counts, so a new run row.
    log = tmp_path / "SAMPLE.txt"
    log.write_text(SAMPLE_LOG + SAMPLE_LOG.split("\n\n")[1] + "\n\n", encoding="utf-8")
    evidence_db.unlink()
    indexer.parse_log(
        str(log), db_path=str(evidence_db), config_path=str(ROOT / "parser_config.json"),
        quiet_overlaps=True, test_id=TEST_ID, source_file=log.name, chunk_seq=1,
    )
    p = _payload()
    p["findings"][1]["evidence"][0]["quote"] = ["\"records\": 3"]
    a3 = results_db.save_analysis(results, evidence_db, p)
    r3 = results_db.get_analysis(results, a3)["run"]
    assert r3["run_id"] != r1["run_id"]
    assert r3["rf_count"] == 3 and r1["rf_count"] == 2
    assert r3["index_fingerprint"] != r1["index_fingerprint"]


def test_parser_config_hash_is_recorded(evidence_db: Path, results: Path) -> None:
    aid = results_db.save_analysis(
        results, evidence_db, _payload(),
        parser_config=ROOT / "parser_config.json", indexer_commit="abc1234",
    )
    run = results_db.get_analysis(results, aid)["run"]
    assert run["parser_config_sha256"] == results_db._sha256_file(ROOT / "parser_config.json")
    assert run["indexer_commit"] == "abc1234"


# ---------------------------------------------------------------------------
# Review and finding lifecycle
# ---------------------------------------------------------------------------


def test_review_and_finding_status(evidence_db: Path, results: Path) -> None:
    aid = results_db.save_analysis(results, evidence_db, _payload())
    results_db.review_analysis(results, aid, "accepted", "Pulkit", "checked against log")
    review = results_db.get_analysis(results, aid)["review"]
    assert review["status"] == "accepted" and review["reviewer"] == "Pulkit"
    assert results_db.list_analyses(results, status="accepted")[0]["analysis_id"] == aid

    with pytest.raises(ValueError, match="review status"):
        results_db.review_analysis(results, aid, "draft", "Pulkit")
    with pytest.raises(ValueError, match="reviewer is required"):
        results_db.review_analysis(results, aid, "rejected", "  ")
    with pytest.raises(KeyError):
        results_db.review_analysis(results, "missing", "accepted", "Pulkit")

    core = results_db.list_findings(results, layer="core")
    assert [f["code"] for f in core] == ["DEF-1"]
    results_db.set_finding_status(results, core[0]["finding_id"], "fixed")
    assert results_db.list_findings(results, status="open")[0]["code"] == "RF-OK"
    assert results_db.list_findings(results, status="fixed")[0]["code"] == "DEF-1"
    with pytest.raises(ValueError):
        results_db.set_finding_status(results, core[0]["finding_id"], "closed")


# ---------------------------------------------------------------------------
# window --sequence and the CLI
# ---------------------------------------------------------------------------


def test_window_sequence_anchors_on_exact_block(evidence_db: Path) -> None:
    nas = json.loads(qxdm_tool.run_query(str(evidence_db), ["nas", "--test-id", TEST_ID]))
    seq = nas[0]["sequence"]
    out = qxdm_tool.run_query(
        str(evidence_db), ["window", "--sequence", str(seq), "-n", "0", "--test-id", TEST_ID])
    assert "_5gmm_cause = 22" in out
    assert out.count("[source:") == 1


def test_cli_round_trip(evidence_db: Path, results: Path, tmp_path: Path) -> None:
    p = _payload()
    p.pop("report_markdown")
    p["report_file"] = "report.md"
    (tmp_path / "report.md").write_text("# From file\n", encoding="utf-8")
    (tmp_path / "analysis.json").write_text(json.dumps(p), encoding="utf-8")

    def cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "qxdm_tool.py"), "--db", str(evidence_db), *args],
            capture_output=True, text=True, cwd=tmp_path,
        )

    saved = cli("save-analysis", "--results-db", str(results), str(tmp_path / "analysis.json"))
    assert saved.returncode == 0, saved.stderr
    aid = json.loads(saved.stdout)["analysis_id"]
    shown = cli("show-analysis", "--results-db", str(results), aid, "--markdown")
    assert shown.stdout.strip() == "# From file"
    listed = json.loads(cli("list-analyses", "--results-db", str(results)).stdout)
    assert listed[0]["analysis_id"] == aid

    p["findings"][0]["evidence"][0]["quote"] = ["not in the output"]
    (tmp_path / "bad.json").write_text(json.dumps(p), encoding="utf-8")
    bad = cli("save-analysis", "--results-db", str(results), str(tmp_path / "bad.json"))
    assert bad.returncode == 2
    assert json.loads(bad.stderr)["rejected"] is True
