# QA Audit Report — Post-Commit `03f1d80`

This report is the live ledger of issues in the QXDM automation code.
Items already resolved are kept at the bottom in a clearly-marked
**✅ Resolved** section so the audit trail is preserved. Anything
remaining is sorted by descending severity — 🔴 critical issues first,
then 🟠 medium, then � minor.

Verified against the current code on branch `fix/pr#2`. **71 tests pass
(1 warning)** — `pytest -q`.

---

## 🔴 Critical issues (still open)

_None._ All critical correctness bugs from the original audit have been
resolved by commits `6d1a820` and `03f1d80` (see ✅ Resolved section
below).

If the next round of work surfaces a 🔴 issue, it will go here.

---

## 🟠 Medium issues (still open)

### 1. `decode_gprs_timer2` under-reports back-off timers by up to 30×

**File:** `indexer.py:96-100`.

```python
multipliers = {0: 2, 1: 60, 2: 360}
...
return value * multipliers.get(unit, 2)
```

3GPP TS 24.008 10.5.7.4 defines *seven* unit values, not three:

| unit | multiplier |
|------|------------|
| 0    | 2 seconds  |
| 1    | 60 seconds |
| 2    | 360 seconds|
| 3    | 60 seconds |
| 4    | 60 seconds |
| 5    | 60 seconds |
| 6    | 60 seconds |
| 7    | deactivated|

Units 3-6 currently fall back to 2 seconds — that under-reports
back-off timers by 30× for real network output. The
`test_nas_timers_within_backoff_range` check (`<= 7200 s`) does not
catch under-reporting. Directly affects the NAS-reject diagnosis flow
documented in `ARTIFACT1_EXEC_SUMMARY.md` / `ARTIFACT2_TECHNICAL_RCA.md`.

**Fix:** use the documented table:

```python
multipliers = {0: 2, 1: 60, 2: 360, 3: 60, 4: 60, 5: 60, 6: 60}
```

### 2. `lte_rf` has no `msg_codes` filter — risks false-positive `rf_kpis` rows

**File:** `parser_config.json:104-129`.

The only match criterion is `text_tokens_any: ["nas_sig_info",
"lte_sig_info"]`. Any block whose body happens to mention those tokens
will be claimed regardless of its `msg_code`. Live evidence:
`lte_rf: matched=276 parsed=276` looks healthy but relies entirely on
the regex matching `rsrp=…`/`rsrq=…` only in genuine signal-info blocks.
A debug log block containing the literal string `nas_sig_info` inside a
printf-style message but belonging to a different msg_code would be
incorrectly parsed as an LTE RF measurement.

**Fix:** add `msg_codes_uppercase: ["0X1544"]` so the parser only
claims 0x1544 blocks.

### 3. `rrc_event` uses overly broad `msg_type_contains: ["Event"]`

**File:** `parser_config.json:131-144`.

"Event" appears in nearly every QXDM message type. The parser also
declares `msg_codes_uppercase: ["0X1FFB", "0XB821", "0XB0C0"]` which
constrains the actual match — but if a future config author drops the
msg_codes safety net, the parser would silently grab every Event block
and the events table would fill with duplicates. The parser_overlaps
table (`03f1d80`) records 7 468 overlaps on this log, mostly
`rrc_event` + `rrc_state` collisions on 0x1FFB.

**Fix:** drop `msg_type_contains` from `rrc_event` (the three
msg_codes are sufficient) or replace with a more specific token like
`"EVENT_LTE_RRC"` or `"EVENT_NR5G_RRC"`.

### 4. Schema doesn't validate `msg_codes` are hex literals

**File:** `parser_config_schema.py:_validate_str_list` (line 159-170).

PR #2's `_validate_str_list` only checks "non-empty string". An agent
can write `{"msg_codes": ["foo", "bar"]}` and the validator accepts it.
`_block_matches` then silently never matches any real QXDM msg code,
and `parser_health` shows `matched: 0` for the affected parser — looks
healthy in shape but is dead in practice.

**Fix:** add a hex-literal check:

```python
_require(
    all(re.fullmatch(r"0x[0-9A-Fa-f]+", item) for item in match[key]),
    spec_path,
    "items must be 0x-prefixed hex literals",
)
```

### 5. Schema doesn't validate timer names

**File:** `parser_config_schema.py:_validate_extract` `timers` branch
(line 86-93).

`_validate_extract`'s `timers` branch only type-checks the list. An
agent can write `{"timers": ["tABCD"]}` and the validator accepts it;
the indexer calls `decode_gprs_timer2(block, "tABCD")` which returns
`None` silently. The parser looks successful but a typo has silently
dropped a column.

**Fix:** add a small allowlist of known GPRS timer identifiers
(`t3346`, `t3502`, `t3324`, `t3412`, `t3402`, `t3411`) and reject
anything outside.

### 6. Inconsistent warning-payload envelope across CLI commands

**File:** `qxdm_tool.py:192-296`.

| command          | warning payload                                              |
|------------------|--------------------------------------------------------------|
| `parser-health`  | `{"parsers": {}, "warning": "..."}`                          |
| `parser-failures`| `{"failures": [], "warning": "..."}`                         |
| `unknown-types`  | `{"unknown_msg_codes": [], "warning": "..."}`                |
| `parser-overlaps`| `{"overlaps": [], "warning": "..."}`                         |

Four different envelope names (`parsers`, `failures`, `unknown_msg_codes`,
`overlaps`). Downstream tooling / agents that read all four responses
have to special-case each shape.

**Fix:** standardise on one envelope, e.g. always
`{"<resource_name>": <data>, "warning": "..."}`, or always
`{"data": ..., "warning": ...}`.

### 7. `_parse_lte_rf` calls `rssi.pattern.search(block_text)` twice

**File:** `indexer.py:320-321`.

```python
rssi_val = _safe_float(extract["rssi"]["pattern"].search(block_text).group(1)) \
    if extract["rssi"]["pattern"].search(block_text) else None
```

The `search` runs once for the truthiness check and again to extract
`group(1)`. Doubles the regex work for every LTE RF block.

**Fix:**

```python
rssi_match = extract["rssi"]["pattern"].search(block_text)
rssi_val = _safe_float(rssi_match.group(1)) if rssi_match else None
```

### 8. `_parse_nr_searcher` recompiles `table_row.regex` per block

**File:** `indexer.py:249-253`.

```python
table_spec = extract["table_row"]["spec"]
row_re = re.compile(
    table_spec["regex"],
    sum(getattr(re, f) for f in table_spec.get("flags", [])),
)
```

The validator already pre-compiles `extract.*` regexes and stashes them
at `extract[field]["pattern"]`; `table_row` is the lone exception. With
~thousands of `0xB97F` blocks in a 50 MB log this is measurable wasted
CPU.

**Fix:** move the compile into `_validate_extract`'s `table_row` branch:

```python
compiled[field] = {
    "pattern": _compile_pattern(spec["regex"], flags, spec_path),
    "spec": spec,
}
```

and have the indexer read `table_spec["pattern"]` directly.

### 9. `parser_failures` `LIMIT ?` returns first-N globally, not N per parser

**File:** `qxdm_tool.py:220-268`.

The query is `SELECT … FROM parser_failures ORDER BY id ASC LIMIT ?`,
which returns the first N failures inserted across all parsers. With
`--limit 10`, the agent sees 10 rows from whichever parser had the
lowest `parser_failures.id` values (typically `rrc_event`), and later-
inserted failures are invisible without raising the limit.

The function's docstring implies per-parser sampling.

**Fix:** use a window function (`ROW_NUMBER() OVER (PARTITION BY
parser_name ORDER BY id)`) or two queries (one for the parser name
list, one per parser).

### 10. Overlap noise floods stderr (usability regression from `03f1d80`)

**File:** `indexer.py:509-513`.

`03f1d80` added parser_overlaps tracking with a per-block stderr log
line:

```
[!] Parser overlap at 2023 Jan 11  11:17:29.011 0x1FFB: rrc_event, rrc_state; selected rrc_event
```

For the bundled log that produces **7 468 lines** of stderr noise
(rrc_event + rrc_state overlap on every 0x1FFB block). The data is
correctly persisted to `parser_overlaps` (and queryable via
`qxdm_tool.py parser-overlaps`), so the value is in the table, not the
per-line log.

**Fix:** add a `--quiet-overlaps` flag (default off) that suppresses
the per-line log; keep the persistence behaviour unchanged. Or
truncate to first-N overlaps with a summary count.

### 11. `msg_codes` and `msg_codes_uppercase` are redundant

**File:** `parser_config.json`, `indexer.py:108-114`.

Both fields exist and the indexer normalises both to uppercase. The
config author has to wonder which to populate. Confusing and error-
prone.

**Fix:** remove one field everywhere, document the canonical form
(e.g. always `msg_codes_uppercase`, lowercase the input on read), and
let the validator enforce it.

### 12. `anomalies` LIKE clauses overlap and don't surface `releaseCause`

**File:** `qxdm_tool.py:30-55`.

The query has nine `LIKE` clauses, several of which overlap
(`%RRCConnectionRelease%` vs `%RRC Release` vs
`%DL_RRCConnectionRelease%`). The agent tuning this list has no signal
about which clause catches which event.

The docstring in `CLAUDE.md` tells the agent to use
`qxdm_tool.py window` to inspect `releaseCause`, but `anomalies` does
not surface it — every diagnosis needs a second query.

**Fix:** factor the LIKE patterns into a named list at module level
with `# matches X` comments; consider joining on `events.raw_block`
with a regex extraction, or expose a `--cause` flag on `anomalies`.

---

## 🟡 Minor issues (still open)

### 13. `indexer.py` accepts the log file path positionally without validating it exists

**File:** `indexer.py` `__main__` block (around line 770-790).

If the path doesn't exist, `open()` raises `FileNotFoundError` deep in
the call stack with no helpful context. `promote_config.py` already
does this check (line 58-60); `indexer.py` does not.

**Fix:** add `Path(args.log_file).is_file()` before `parse_log`, with a
clear error message.

### 14. `parser_config_schema.py` CLI reports only the first validation error

**File:** `parser_config_schema.py:267-275` (CLI entrypoint).

When `python3 parser_config_schema.py candidate.json` fails, only the
first `ConfigValidationError` is printed. The agent has to fix and
re-run to discover subsequent errors.

**Fix:** aggregate all errors into a list, raise a single combined
`ConfigValidationError` at the end (or return a non-zero exit with a
JSON error array).

### 15. `decode_gprs_timer2` regex could over-match `t3502_ext`

**File:** `indexer.py:84-88`.

```python
sub = re.search(
    rf"^\s*{name}\s*$(.{{0,400}}?)timer_2_value\s*=\s*(\d+)",
    block_text,
    re.MULTILINE | re.DOTALL,
)
```

If a block has `t3502_incl = 1` and a separate `t3502_ext = 0` line
below, the regex could capture the wrong `timer_2_value` because
`t3502` matches both the line itself and the prefix of `t3502_ext`.

**Fix:** add a negative lookahead `(?<!_ext)` or anchor more strictly.

### 16. `parse_log` doesn't wrap body in `try/finally` to close DB on exception

**File:** `indexer.py` `parse_log` body (~line 460-660).

If `init_db` or any `cur.execute` raises mid-run, `conn.close()` is
never called and the SQLite file may stay locked until process exit.
With `--dry-run` and a large log this can leave `qxdm_dryrun.db` (or
any scratch DB) in a half-written state.

**Fix:** wrap the body in `try/finally: conn.close()` or use a context
manager (`with sqlite3.connect(...) as conn:`).

### 17. `_record_unknown` / `_record_failure` lack defensive guard for `current_ts=None`

**File:** `indexer.py:172-191, 472-543`.

`flush_block` returns early when `not current_block or not current_ts`,
so the function never actually sees `None` today. But there's no
defensive assertion; a future refactor that moves the header-match
logic could regress this. Low-risk but worth a one-line guard.

**Fix:** add an `assert` or `if current_ts is None: return` guard at the
top of `flush_block`.

---

## ✅ Resolved (audit-trail preservation)

These items were raised in earlier audits and have been resolved by
commits on this branch. Listed here for the historical record so the
diff between audits is obvious.

### Closed by `6d1a820` ("fixing #1")

- `_parse_nas` returns `False` when `cause_code is None` (was issue
  #1's silent-garbage-row behaviour).
- Schema validator pre-compiles `text_token_regex` into
  `text_token_regex_pattern`.
- Schema validator type-checks list elements via new
  `_validate_str_list` helper.
- Schema validator validates `rat_detection` and `emit_event`
  structure.
- Schema validator rejects non-dict `extract`.
- `init_db` drops `unknown_samples` (was missing from the rebuild path).
- `keep_health` dead parameter removed.
- `diff_health` signature flattened to `{parser: {…}}`.
- `promote_candidate` and `cmd_promote` handle `src.resolve() ==
  dst.resolve()`.
- `cmd_validate` checks the log file exists.
- `parser_failures` catches `OperationalError` on missing table.
- `--dry-run` defaults to `qxdm_dryrun.db` (production-DB protection).
- `test_parser_health_consistent` checks `invalid_value <= matched`.
- `print` statements that polluted JSON output moved to `stderr`.

### Closed by `03f1d80` ("close NAS over-matching, restore default rebuild, harden unknown_types")

- **R1** — Default rebuild workflow was blocked: the
  `db_path_was_default` safety check fired for every non-`--append`
  invocation. Now removed; default rebuilds proceed via `init_db()`'s
  existing unlink+rebuild path.
- **R2** — Second `--dry-run` invocation refused because the
  scratch path itself existed. Now `init_db()`'s unlink+rebuild makes
  repeated dry-runs safe without `--append`.
- **R3** — `unknown_types` half-fix: the inner per-code SELECT on
  `unknown_samples` was unprotected. Now wrapped symmetrically with
  the outer SELECT; produces
  `{"unknown_msg_codes": [...], "samples": [], "warning": "..."}` when
  the inner table is missing.
- **#1 (nas_lte over-matching)** — `text_token_regex` was
  `(?:^|[^A-Z])(EMM)(?:[^A-Z]|$)` and OR-gated with `msg_codes`. Now
  tightened to `(?:^|[^A-Z])EMM[ _]CAUSE\s*=(?:[^A-Z]|$)` AND-gated via
  `mode: "all"`. Live effect: `nas_lte` correctly claims zero
  non-reject blocks (78 B0EC/B0ED blocks in the bundled log, none with
  `emm_cause`).
- **#2 (nas_5gmm over-matching)** — Same fix shape on the 5GMM regex.
  Live effect: `nas_5gmm` went from `parsed 20/80, failed 60` →
  `parsed 20/20, failed 0` (100% parse rate).
- **#6 (parser_failures type annotation)** — Was
  `-> list[dict[str, Any]]` but returned a dict on the warning
  branch. Now `-> list[dict[str, Any]] | dict[str, Any]`.
- **#10 (overlap invisibility)** — Blocks claimed by multiple parsers
  were silently first-wins. Now persisted to a new `parser_overlaps`
  table and exposed via `qxdm_tool.py parser-overlaps`. First-parser-
  wins semantics retained for the actual event/nas/rf emission; only
  the audit trail changes.
- **#13 (redundant re.IGNORECASE)** — Was applied to a regex that
  already operated on upper-cased input. Now dropped (with an inline
  comment explaining why).

### Closed by repository conventions (not code)

- **`qxdm_indexed.db` is no longer tracked** — added `*.db` to
  `.gitignore` and untracked the existing file. Reproducible from
  source via `python3 indexer.py <log>`.

---

## Verification commands

Re-run these to confirm each remaining item is still live:

```bash
# Confirm decode_gprs_timer2 under-reporting (issue 1)
grep -n "multipliers" indexer.py    # should still show {0: 2, 1: 60, 2: 360}

# Confirm _parse_lte_rf double RSSI search (issue 7)
grep -n "rssi.pattern.search" indexer.py    # should still show 2 calls

# Confirm _parse_nr_searcher per-block recompile (issue 8)
grep -n "row_re = re.compile" indexer.py    # should still be inside the function body

# Confirm CLI envelope-key drift (issue 6)
python3 qxdm_tool.py --db qxdm_indexed.db parser-health | head -3
python3 qxdm_tool.py --db qxdm_indexed.db parser-failures | head -3
python3 qxdm_tool.py --db qxdm_indexed.db unknown-types | head -3
python3 qxdm_tool.py --db qxdm_indexed.db parser-overlaps | head -3

# Confirm overlap-noise regression (issue 10)
python3 indexer.py UE23_*.txt --dry-run 2>&1 | grep -c "Parser overlap"

# Full test suite (should be 71 passed, 1 warning)
source venv/bin/activate && python3 -m pytest -q
```

## How I verified

- `git log --oneline -5` — confirmed `03f1d80` is HEAD on
  `fix/pr#2`.
- `git show 03f1d80 --stat` — read every file changed to determine
  what landed.
- Live re-index: `python3 indexer.py UE23_TCP_UDP_UL_75_01-11.06-36-49-875.txt qxdm_test.db`
  + `python3 qxdm_tool.py --db qxdm_test.db parser-health` — confirmed
  `nas_5gmm: 20/20/0`, `nas_lte` not in output (correct: 78
  0xB0EC/0xB0ED blocks in the log, none contain `emm_cause`).
- `python3 test_indexer_matching.py` — 6 passed (the new tests for
  `mode=all`, AND-gating, and overlap persistence).
- `python3 test_parser_sanity.py qxdm_test.db` — 10 passed.
- `python3 -m pytest -q` — **71 passed, 1 warning** (no regressions).
- `python3 indexer.py UE23_*.txt --dry-run` — confirmed the redirect
  to `qxdm_dryrun.db` works repeatedly without self-tripping.
- `python3 indexer.py UE23_*.txt qxdm_indexed.db` — confirmed default
  rebuild works without `--append`.
- Read current `indexer.py:96-100, 310-330, 250-255`, `qxdm_tool.py:220-340`,
  `parser_config_schema.py:159-180` to confirm each remaining item is
  still present in the code.
