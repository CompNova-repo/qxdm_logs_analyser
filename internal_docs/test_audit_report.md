# Test Audit Report — `next_step_overview.md` Compliance

**Date:** 2026-08-28
**Reviewer:** tester perspective (read-only audit)
**Scope:** verify each item in `internal_docs/next_step_overview.md` is implemented and behaviour-matches the design.

---

## Coverage matrix

| # | Item from overview | Implemented? | Evidence |
|---|---|---|---|
| 1 | `parser_config.json` externalizes codes/markers/regex | ✅ | `parser_config.json` exists; `indexer.py` no longer contains hardcoded msg-code lists outside `_block_matches`. |
| 2 | Config schema/validator rejects malformed JSON, invalid regex, missing fields | ✅ | `parser_config_schema.py::validate_config` / `load_config`; verified live with bad inputs (invalid flag, unknown parser name, broken regex). |
| 3 | `indexer.py` refactored to use the JSON rules | ✅ | `indexer.py` imports `load_config` and dispatches via `_block_matches` / `_parse_*`. |
| 4 | Complex parsing kept in Python (timer decoding, NR tables, numeric validation, DB ops) | ✅ | `decode_gprs_timer2`, `_parse_nr_searcher`, `_range`, SQLite writes all remain in `indexer.py`. |
| 5 | Parser-health statistics (matched / parsed / failed / invalid / unknown) | ✅ | `ParserStats` + `unknown_msg_codes` tables populated. |
| 6 | Capture representative failure samples | ✅ | `parser_failures` + `unknown_samples` tables, capped via `failure_sampling` block. |
| 7 | `qxdm_tool.py` exposes `parser-health`, `parser-failures`, `unknown-types` | ⚠️ Partial — see Issue #1 below |
| 8 | Allow candidate configs (write to `parser_config.candidate.json`) | ✅ | `promote_config.py validate` / `promote` work end-to-end against a copied candidate. |
| 9 | Dry-run validation over the entire log | ✅ | `--dry-run` flag in `indexer.py` and the dry-run path inside `promote_config.py::cmd_validate`. |
| 10 | Automated sanity tests (RSRP / cause codes / timers / regressions) | ✅ | `test_parser_sanity.py` — 10/10 passing in venv. |
| 11 | Promotion only after tests pass → replace production → re-index DB | ⚠️ Partial — see Issue #2 below |

---

## Issues found

### � Issue #1 — `parser-failures` crashes when its table is missing (UX inconsistency)

**Severity:** Medium
**Location:** `qxdm_tool.py:220-255` (`parser_failures`)

The other two query commands handle the "table not present yet" case gracefully:

```text
$ python3 qxdm_tool.py --db /tmp/no_failures.db parser-health
{ "parsers": {}, "warning": "parser_health table is missing — re-run indexer.py" }
$ python3 qxdm_tool.py --db /tmp/no_failures.db unknown-types
{ "unknown_msg_codes": [], "warning": "unknown_msg_codes table is missing" }
```

But `parser-failures` does not:

```text
$ python3 qxdm_tool.py --db /tmp/no_failures.db parser-failures
error: no such table: parser_failures
$ echo $?
1
```

**Fix:** wrap the SELECT in `try/except sqlite3.OperationalError` and return `{"failures": [], "warning": "parser_failures table is missing — re-run indexer.py"}`, matching the other two commands.

---

### � Issue #2 — `--dry-run` against the production DB **destroys the entire database** (major footgun)

**Severity:** **High**
**Location:** `indexer.py:362-364` (`init_db`)

The `--dry-run` help text claims it only skips writes:

> `Skip events/nas/rf writes; still populate parser_health and parser_failures so the agent can diff against the production DB`

But `init_db` runs *before* the dry-run gate, and unconditionally deletes the DB file when `append=False`:

```python
def init_db(db_path="qxdm_indexed.db", append=False, keep_health=True):
    db_file = Path(db_path)
    if not append and db_file.exists():
        db_file.unlink()    # <-- nukes the production 30 MB file
```

Reproduced:

```text
Before: qxdm_indexed.db size = 30,146,560 bytes
$ python3 indexer.py --dry-run UE23_…txt qxdm_indexed.db
After:  qxdm_indexed.db size =    372,736 bytes   (events/nas/rf all gone)
```

The default `db_path` is `qxdm_indexed.db` — the production DB. An agent or human who reads the help and runs `indexer.py <log> --dry-run` to "test the new config" silently loses everything.

**Fix options (pick one):**

1. Default `db_path` to a scratch location (`qxdm_dryrun.db`) whenever `--dry-run` is set, and refuse to overwrite `qxdm_indexed.db` unless `--append` is also passed. Print a clear warning when the default would clobber an existing file.
2. Skip the `unlink()` when `dry_run=True` and the DB exists — only rewrite the parser-health tables.

---

### 🐞 Issue #3 — `--append` mode crashes with `OperationalError: table unknown_samples already exists`

**Severity:** High
**Location:** `indexer.py:399-430` (`init_db`)

The DROP block forgets one of the four parser-health tables:

```python
cur.execute("DROP TABLE IF EXISTS parser_health")
cur.execute("DROP TABLE IF EXISTS parser_failures")
cur.execute("DROP TABLE IF EXISTS unknown_msg_codes")
# <-- missing DROP for unknown_samples
…
cur.execute("""CREATE TABLE unknown_samples …""")    # raises on append run
```

Reproduced:

```text
$ python3 indexer.py --dry-run --append UE23_…txt qxdm_indexed.db
sqlite3.OperationalError: table unknown_samples already exists
```

The same is true of plain `indexer.py log.txt qxdm_indexed.db --append` — any append run that isn't a first-time create will fail.

**Fix:** add `cur.execute("DROP TABLE IF EXISTS unknown_samples")` right after the other three DROPs (line 401.5), or change all four CREATE statements to `CREATE TABLE IF NOT EXISTS …` and drop the manual DROPs.

---

### 🐞 Issue #4 — `text_token_regex` branch in `_block_matches` is partially dead code

**Severity:** Low
**Location:** `indexer.py:116-123`

```python
if match.get("text_token_regex"):
    pattern = match["text_token_regex"]
    if isinstance(pattern, str):
        return re.search(pattern, block_upper) is not None
    return pattern.search(block_upper) is not None
```

The schema (`parser_config_schema.py:172-182`) validates `text_token_regex` as a *string only*. The `else` branch never runs in production. Either:

- pre-compile `text_token_regex` in the validator (alongside `extract.*.regex`) and drop the `isinstance` check, **or**
- drop the dead `else` branch.

Pick one — leaving both is confusing.

---

### 🐞 Issue #5 — Dead code: `indexer.diff_health` and `keep_health` parameter

**Severity:** Low
**Location:** `indexer.py:361, 649`

- `diff_health()` is defined at line 649 but **never called** — `promote_config.py::cmd_validate` re-implements the diff inline. Either delete it or wire it into `cmd_validate`.
- `init_db(..., keep_health: bool = True)` declares `keep_health` but never references it. Either honour the flag (don't drop the health tables when `keep_health=True`) or remove it from the signature.

---

### 🐞 Issue #6 — `_parse_nas` always returns `True`, so `failure_reason = "cause regex missed"` is unreachable

**Severity:** Medium
**Location:** `indexer.py:202-233`

`_parse_nas` appends a `nas_events` row regardless of whether the cause regex matched (cause_code and cause_str simply default to `None` / `"UNKNOWN"`). It returns `True` unconditionally. As a result:

- the dispatch site never sets `failure_reason = "cause regex missed"`;
- the `parser_failures` table will **never** contain an NAS failure, no matter how many NAS blocks miss the cause regex.

If a malformed cause regex appears in a candidate, the agent will see only the parser_health deltas (matched ↑ but parsed stays flat → implicit regression), not a labelled failure. The dead branch in `flush_block`:

```python
if not parsed_ok:
    failure_reason = "cause regex missed"
```

…isn't doing anything today.

**Fix options:**

- Have `_parse_nas` return `False` (and skip the NAS append) when `cause_code is None`. This will surface the failure in `parser_failures` with the right reason string.
- Or, if "cause-less NAS messages are still interesting" is the intent, drop the dead `failure_reason` assignment and add a comment explaining why NAS is permissive.

---

### 🐞 Issue #7 — Sanity test `assert invalid_value <= parsed` is incorrect for `nr_searcher`

**Severity:** Medium
**Location:** `test_parser_sanity.py:120-135`, `indexer.py:251-275`

`_parse_nr_searcher` increments `invalid_value` per **row** that fails validation (and `continue`s past it), but a successful fallback row marks the whole block as `parsed_ok = True`. With many invalid rows in one block, `invalid_value` can exceed `parsed` (parsed counts blocks, not rows). The docstring claims "invalid_value should be a subset of parsed," but the implementation measures rows, not blocks.

For other parsers (`lte_rf`), `invalid_value` is incremented at most once per block, so the invariant holds — only `nr_searcher` is affected.

Current data happens to satisfy the assertion (`nr_searcher` invalid_value=20 ≤ parsed=128), so the test passes, but a future log could regress without warning.

**Fix:** change the assertion to one of:

- `assert invalid_value <= matched` (the only true upper bound), **or**
- compare `invalid_value` against `len(rows_seen)` rather than `parsed`, by adding a new counter to `ParserStats`, **or**
- document the difference and only assert `invalid_value <= matched` for `nr_searcher`.

---

### 🐞 Issue #8 — Validator doesn't type-check elements inside match-block lists

**Severity:** Low
**Location:** `parser_config_schema.py:153-189`

The validator confirms that `msg_codes`, `msg_codes_uppercase`, `text_tokens_any`, and `msg_type_contains` are lists, but never inspects their elements. A typo like `"msg_codes": ["0xB80A", 42]` or `"text_tokens_any": [null]` passes:

```text
$ python3 parser_config_schema.py /tmp/bad5.json
OK: /tmp/bad5.json -> 1 parsers (nas_5gmm)
```

At runtime `indexer.py` would crash inside `c.upper()` or `tok.upper()` on the bad element.

**Fix:** add an `all(isinstance(x, str) and x for x in lst)` check in each branch.

---

### 🐞 Issue #9 — `rat_detection` and `emit_event` blocks are passed through verbatim

**Severity:** Low
**Location:** `parser_config_schema.py:199-203`

```python
if "rat_detection" in parser:
    compiled["rat_detection"] = parser["rat_detection"]
if "emit_event" in parser:
    compiled["emit_event"] = parser["emit_event"]
```

No type/structure check, no `KeyError` protection for missing sub-keys (`rat_nr`, `rat_lte`, `summary_format`). If an agent deletes a key by accident, the failure shows up later as a `KeyError` deep in `_parse_nas`. Add at minimum:

- `rat_detection` must be a dict; `nr_codes_uppercase` and `nr_text_tokens` lists of strings; `rat_nr`/`rat_lte` strings.
- `emit_event` must be a dict containing a string `summary_format` (otherwise `_parse_nas` will throw on `.format()`).

---

### 🐞 Issue #10 — `promote_candidate` / `cmd_promote` don't guard against `src == dst`

**Severity:** Low
**Location:** `indexer.py:670-681`, `promote_config.py:124-138`

Running `python3 promote_config.py promote --candidate parser_config.json --target parser_config.json` (or `indexer.py <log> --config parser_config.json --promote`) will hit `shutil.SameFileError` because no copy is performed. Either detect same-path and no-op, or document that the flags are mutually exclusive.

---

### 🐞 Issue #11 — `indexer.parse_log` print output contaminates the JSON health report

**Severity:** Low
**Location:** `indexer.py:544, 641-646`

`print(...)` of progress and result lines goes to stdout, immediately followed by `print(json.dumps(health, indent=2))`. When an agent calls the CLI and parses stdout as JSON, the leading `[*] …` and `[✓] …` lines break the parse. Either redirect progress logs to `stderr`, or wrap them behind a `--quiet` flag and default to quiet.

---

### 🐞 Issue #12 — `promote_config.py validate` doesn't pre-check the log path

**Severity:** Low
**Location:** `promote_config.py:56-74`

If `args.log` doesn't exist, the failure surfaces as a raw `FileNotFoundError` traceback from `open()` inside `parse_log`. Add a `Path(args.log).is_file()` check at the top of `cmd_validate` and print a clean error.

---

## Observations (not bugs)

- The `rrc_state` parser's `match` block has `msg_type_contains: ["CM State Info"]` AND `block_text_contains: ["RRC"]`. Combined with the universal matchers (no msg_code filter), it could in theory claim blocks that mention "CM State Info" + "RRC" anywhere — fine for this dataset, worth keeping in mind.
- The `parser-health` output keys come from `parser_health` rows but are sorted by parser_name alphabetically. With many parsers this gets noisy — consider a `--top N` option (parallels `unknown-types --limit`).
- The `lte_rf` parser matches by `text_tokens_any` only (no `msg_codes_uppercase`). Many `0x1544` blocks are classified as unknown — likely because QXDM emits different variants for the same msg code. Consider adding explicit msg-code allowlists for `lte_rf` once the agent has surveyed the unknown samples.
- `_parse_lte_rf` calls `extract["rssi"]["pattern"].search(block_text)` **twice** when RSSI is present (once in the ternary, once to grab the value). Minor wasted work, not a correctness bug.

---

## Suggested fix order

1. **#3** (append crashes) — one-line fix, blocks a whole workflow.
2. **#2** (`--dry-run` footgun) — guards against data loss; default DB protection.
3. **#7** (test invariant) — small test change, prevents future false green.
4. **#6** (NAS always-true) — clarifies a confusing dead branch.
5. **#1** (missing-table UX) — parity between subcommands.
6. The rest are cleanup / hardening.
