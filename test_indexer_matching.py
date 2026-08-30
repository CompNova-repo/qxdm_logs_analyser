"""Regression tests for parser claim selection and overlap diagnostics."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from indexer import _block_matches, parse_log
from parser_config_schema import ConfigValidationError, load_config, validate_config


def _parser(name: str) -> dict:
    config = load_config(Path(__file__).with_name("parser_config.json"))
    return next(parser for parser in config["parsers"] if parser["name"] == name)


@pytest.mark.parametrize(
    ("name", "code", "text", "expected"),
    [
        ("nas_lte", "0xFFFF", "EVENT_LTE_EMM_OUTGOING_MSG", False),
        ("nas_lte", "0xB0EC", "EMM cause emm_cause = 7", True),
        ("nas_5gmm", "0xFFFF", "payload._5gmm_cause = 7", False),
        ("nas_5gmm", "0xB80A", "payload._5gmm_cause = 7", True),
    ],
)
def test_nas_match_requires_code_and_protocol_token(
    name: str, code: str, text: str, expected: bool,
) -> None:
    parser = _parser(name)
    assert _block_matches(parser, code.upper(), "type", text.upper(), text) is expected


def test_match_mode_must_be_known() -> None:
    raw = json.loads(Path("parser_config.json").read_text())
    raw["parsers"][0]["match"]["mode"] = "sometimes"
    with pytest.raises(ConfigValidationError, match="must be either 'any' or 'all'"):
        validate_config(raw)


def test_overlap_is_reported_and_persisted(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    raw = json.loads(Path("parser_config.json").read_text())
    lte = raw["parsers"][1]
    lte["match"]["msg_codes"] = ["0xB80A"]
    lte["match"]["msg_codes_uppercase"] = ["0XB80A"]
    lte["match"]["text_token_regex"] = "(?:^|[^A-Z])(5GMM)(?:[^A-Z]|$)"
    config_path = tmp_path / "overlap.json"
    config_path.write_text(json.dumps(raw))
    log_path = tmp_path / "sample.txt"
    log_path.write_text(
        "2026 Aug 28 12:00:00.000 [1] 0xB80A TEST 5GMM\n"
        "_5gmm_cause = 7 (0x7) (service not allowed)\n"
    )
    db_path = tmp_path / "sample.db"

    health = parse_log(str(log_path), str(db_path), config_path=str(config_path))

    assert health["parser_overlaps"] == 1
    assert "nas_5gmm, nas_lte; selected nas_5gmm" in capsys.readouterr().err
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT selected_parser, claiming_parsers FROM parser_overlaps"
        ).fetchone()
    assert row == ("nas_5gmm", "nas_5gmm,nas_lte")
