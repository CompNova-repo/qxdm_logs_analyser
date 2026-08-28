"""Validate ``parser_config.json`` before the indexer trusts it.

The validator is deliberately strict: malformed JSON, invalid regexes, missing
required fields, and unknown parser names all raise ``ConfigValidationError``
with a message that names the offending path. The goal is for the agent (or a
human editing the JSON) to find out *exactly* what is broken before the
indexer is asked to use the config.

This module also exposes :func:`load_config`, the single entry point the
indexer should use. It loads JSON from disk, validates the structure, compiles
all regex patterns once, and returns a frozen dict that the indexer can
consume directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Each parser must define a ``name`` and a ``match`` block. The match block
# decides whether a parser is responsible for a given QXDM block.
_PARSER_REQUIRED = ("name", "match")
_KNOWN_PARSERS = (
    "nas_5gmm",
    "nas_lte",
    "nr_searcher",
    "lte_rf",
    "rrc_event",
    "rrc_state",
)
_KNOWN_FLAGS = {
    "IGNORECASE",
    "MULTILINE",
    "DOTALL",
    "UNICODE",
    "VERBOSE",
    "ASCII",
    "LOCALE",
}


class ConfigValidationError(ValueError):
    """Raised when ``parser_config.json`` fails validation."""


def _require(cond: bool, path: str, msg: str) -> None:
    if not cond:
        raise ConfigValidationError(f"{path}: {msg}")


def _require_keys(obj: dict[str, Any], keys: tuple[str, ...], path: str) -> None:
    for key in keys:
        _require(key in obj, f"{path}.{key}", "required field is missing")


def _compile_pattern(pattern: str, flags: list[str], path: str) -> re.Pattern[str]:
    """Compile a regex and verify that every flag name is recognised."""
    flag_value = 0
    for flag in flags:
        _require(
            flag in _KNOWN_FLAGS,
            f"{path}.flags",
            f"unknown regex flag {flag!r} (known: {sorted(_KNOWN_FLAGS)})",
        )
        flag_value |= getattr(re, flag)
    try:
        return re.compile(pattern, flag_value)
    except re.error as exc:
        raise ConfigValidationError(f"{path}: invalid regex {pattern!r}: {exc}") from exc


def _validate_extract(extract: dict[str, Any], path: str) -> dict[str, dict[str, Any]]:
    """Pre-compile every regex inside an ``extract`` block.

    Each entry must be an object with a ``regex`` key — except for the special
    ``timers`` entry, which is a list of timer names that the Python timer
    decoder walks directly (no regex involved at config-load time).
    """
    compiled: dict[str, dict[str, Any]] = {}
    for field, spec in extract.items():
        spec_path = f"{path}.{field}"
        # ``timers`` is a list of names consumed by decode_gprs_timer2 — pass
        # through after a lightweight type check.
        if field == "timers":
            _require(
                isinstance(spec, list) and all(isinstance(t, str) for t in spec),
                spec_path,
                "'timers' must be a list of timer names",
            )
            compiled[field] = {"spec": {"list": spec}}
            continue
        if field == "table_row":
            # table_row spec describes how to split the captured ASCII row;
            # it may carry nested column indexes and validation ranges that
            # the Python side applies after the split. No top-level pattern.
            _require(
                isinstance(spec, dict),
                spec_path,
                "'table_row' must be an object",
            )
            compiled[field] = {"spec": spec}
            continue
        if field == "fallback_kv":
            _require(isinstance(spec, dict), spec_path, "must be an object")
            compiled[field] = _validate_extract(spec, spec_path)
            continue
        _require(
            isinstance(spec, dict),
            spec_path,
            f"expected object with a 'regex' key, got {type(spec).__name__}",
        )
        _require(
            "regex" in spec,
            spec_path,
            "missing 'regex' key",
        )
        _require(
            isinstance(spec["regex"], str),
            spec_path,
            "'regex' must be a string",
        )
        flags = spec.get("flags", []) or []
        _require(
            isinstance(flags, list) and all(isinstance(f, str) for f in flags),
            f"{spec_path}.flags",
            "'flags' must be a list of strings",
        )
        compiled[field] = {
            "pattern": _compile_pattern(spec["regex"], flags, spec_path),
            "spec": spec,
        }
    return compiled


def _validate_parser(parser: dict[str, Any], path: str) -> dict[str, Any]:
    _require_keys(parser, _PARSER_REQUIRED, path)
    _require(
        isinstance(parser["name"], str) and parser["name"],
        f"{path}.name",
        "must be a non-empty string",
    )
    _require(
        parser["name"] in _KNOWN_PARSERS,
        f"{path}.name",
        f"unknown parser {parser['name']!r} (known: {_KNOWN_PARSERS})",
    )

    match = parser["match"]
    _require(isinstance(match, dict), f"{path}.match", "must be an object")

    if "msg_codes" in match:
        _require(
            isinstance(match["msg_codes"], list),
            f"{path}.match.msg_codes",
            "must be a list",
        )
    if "msg_codes_uppercase" in match:
        _require(
            isinstance(match["msg_codes_uppercase"], list),
            f"{path}.match.msg_codes_uppercase",
            "must be a list",
        )
    if "text_tokens_any" in match:
        _require(
            isinstance(match["text_tokens_any"], list),
            f"{path}.match.text_tokens_any",
            "must be a list",
        )
    if "text_token_regex" in match:
        _require(
            isinstance(match["text_token_regex"], str),
            f"{path}.match.text_token_regex",
            "must be a regex string",
        )
        try:
            re.compile(match["text_token_regex"])
        except re.error as exc:
            raise ConfigValidationError(
                f"{path}.match.text_token_regex: invalid regex: {exc}"
            ) from exc
    if "msg_type_contains" in match:
        _require(
            isinstance(match["msg_type_contains"], list),
            f"{path}.match.msg_type_contains",
            "must be a list",
        )

    compiled: dict[str, Any] = {"name": parser["name"], "match": match, "extract": {}}
    if "extract" in parser:
        _require(
            isinstance(parser["extract"], dict),
            f"{path}.extract",
            "must be an object",
        )
        compiled["extract"] = _validate_extract(parser["extract"], f"{path}.extract")

    if "rat_detection" in parser:
        compiled["rat_detection"] = parser["rat_detection"]
    if "emit_event" in parser:
        compiled["emit_event"] = parser["emit_event"]

    return compiled


def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a parsed JSON object and return a frozen, compiled config dict."""
    _require(isinstance(raw, dict), "$", "config root must be a JSON object")
    _require(
        "parsers" in raw and isinstance(raw["parsers"], list) and raw["parsers"],
        "$.parsers",
        "must be a non-empty list of parser definitions",
    )

    compiled_parsers: dict[str, dict[str, Any]] = []
    seen_names: set[str] = set()
    for idx, parser in enumerate(raw["parsers"]):
        compiled = _validate_parser(parser, f"$.parsers[{idx}]")
        _require(
            compiled["name"] not in seen_names,
            f"$.parsers[{idx}].name",
            f"duplicate parser name {compiled['name']!r}",
        )
        seen_names.add(compiled["name"])
        compiled_parsers.append(compiled)

    header = raw.get("block_header", {})
    _require(
        "pattern" in header and isinstance(header["pattern"], str),
        "$.block_header.pattern",
        "must be a string",
    )
    header_flags = header.get("flags", []) or []
    header_pattern = _compile_pattern(
        header["pattern"], header_flags, "$.block_header"
    )

    validation = raw.get("validation", {})
    sampling = raw.get("failure_sampling", {})

    return {
        "schema_version": raw.get("_schema_version", 0),
        "block_header_pattern": header_pattern,
        "parsers": compiled_parsers,
        "validation": validation,
        "failure_sampling": sampling,
    }


def load_config(path: str | Path) -> dict[str, Any]:
    """Load ``parser_config.json`` from ``path`` and return a compiled config."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigValidationError(f"config file not found: {path}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigValidationError(
            f"config file {path} is not valid JSON: {exc}"
        ) from exc
    return validate_config(raw)


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "parser_config.json"
    try:
        cfg = load_config(target)
    except ConfigValidationError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    parser_names = ", ".join(p["name"] for p in cfg["parsers"])
    print(f"OK: {target} -> {len(cfg['parsers'])} parsers ({parser_names})")