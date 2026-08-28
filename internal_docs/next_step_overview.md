[[Next Steps for External Regex JSON Details]]
- **Create `parser_config.json`** — move message codes, text markers, field aliases, and regex patterns out of `indexer.py`.
    
- **Create a config schema/validator** — reject malformed JSON, invalid regexes, missing fields, etc.
    
- **Refactor `indexer.py`** — load the JSON and use its rules instead of hardcoded regex/message-code conditions.
    
- **Keep complex parsing in Python** — timer decoding, NR tables, numeric validation, DB operations, etc. stay in code.
    
- **Add parser-health statistics** — track `matched`, `parsed`, `failed`, invalid values, unknown message types, etc.
    
- **Capture representative failure samples** — save a small number of failed/unrecognized QXDM blocks for agent inspection.
    
- **Extend `qxdm_tool.py`** — expose commands such as `parser-health`, `parser-failures`, and `unknown-types`.
    
- **Allow candidate configs** — agent writes changes to something like `parser_config.candidate.json`, not production directly.
    
- **Add dry-run validation** — run the candidate config over the **entire log** and compare extraction statistics against the current config.
    
- **Add automated sanity tests** — ensure extracted RSRP/cause codes/timers/etc. remain valid and existing parsing doesn't regress.
    
- **Promote only validated configs** — candidate → tests pass → replace production config → re-index DB.
    

So ultimately:

**Indexer scans everything → reports failures → agent examines small samples → modifies JSON → indexer validates against entire log → config promoted → re-index.**