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
    if "mode" in match:
        _require(
            match["mode"] in ("any", "all"),
            f"{path}.match.mode",
            "must be either 'any' or 'all'",
        )

    def _validate_str_list(key: str) -> None:
        spec_path = f"{path}.match.{key}"
        _require(
            isinstance(match[key], list),
            spec_path,
            "must be a list",
        )
        _require(
            all(isinstance(item, str) and item for item in match[key]),
            spec_path,
            "must be a list of non-empty strings",
        )

    if "msg_codes" in match:
        _validate_str_list("msg_codes")
    if "msg_codes_uppercase" in match:
        _validate_str_list("msg_codes_uppercase")
    if "text_tokens_any" in match:
        _validate_str_list("text_tokens_any")
    if "text_token_regex" in match:
        _require(
            isinstance(match["text_token_regex"], str),
            f"{path}.match.text_token_regex",
            "must be a regex string",
        )
        try:
            # NOTE: no re.IGNORECASE — indexer upper-cases the block text
            # before searching (``block_upper = block_text.upper()``), so the
            # regex only needs to match uppercase forms. Leaving the flag on
            # costs a tiny bit of per-block CPU and misleads maintainers.
            match["text_token_regex_pattern"] = re.compile(
                match["text_token_regex"]
            )
        except re.error as exc:
            raise ConfigValidationError(
                f"{path}.match.text_token_regex: invalid regex: {exc}"
            ) from exc
    if "msg_type_contains" in match:
        _validate_str_list("msg_type_contains")

    compiled: dict[str, Any] = {"name": parser["name"], "match": match, "extract": {}}
    if "extract" in parser:
        _require(
            isinstance(parser["extract"], dict),
            f"{path}.extract",
            "must be an object",
        )
        compiled["extract"] = _validate_extract(parser["extract"], f"{path}.extract")

    if "rat_detection" in parser:
        rd_path = f"{path}.rat_detection"
        rd = parser["rat_detection"]
        _require(isinstance(rd, dict), rd_path, "must be an object")
        if "nr_codes_uppercase" in rd:
            _require(
                isinstance(rd["nr_codes_uppercase"], list)
                and all(isinstance(x, str) and x for x in rd["nr_codes_uppercase"]),
                f"{rd_path}.nr_codes_uppercase",
                "must be a list of non-empty strings",
            )
        if "nr_text_tokens" in rd:
            _require(
                isinstance(rd["nr_text_tokens"], list)
                and all(isinstance(x, str) and x for x in rd["nr_text_tokens"]),
                f"{rd_path}.nr_text_tokens",
                "must be a list of non-empty strings",
            )
        if "rat_nr" in rd:
            _require(
                isinstance(rd["rat_nr"], str) and rd["rat_nr"],
                f"{rd_path}.rat_nr",
                "must be a non-empty string",
            )
        if "rat_lte" in rd:
            _require(
                isinstance(rd["rat_lte"], str) and rd["rat_lte"],
                f"{rd_path}.rat_lte",
                "must be a non-empty string",
            )
        compiled["rat_detection"] = rd
    if "emit_event" in parser:
        ee_path = f"{path}.emit_event"
        ee = parser["emit_event"]
        _require(isinstance(ee, dict), ee_path, "must be an object")
        if "summary_format" in ee:
            _require(
                isinstance(ee["summary_format"], str) and ee["summary_format"],
                f"{ee_path}.summary_format",
                "must be a non-empty string",
            )
        compiled["emit_event"] = ee

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
