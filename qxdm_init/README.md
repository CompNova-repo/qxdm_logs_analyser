# QXDM Initialization & Automation Framework

A production-ready framework for Qualcomm eXtensible Diagnostic Monitor (QXDM)
automation. Provides a Linux REST API, automated raw binary capture
(`.qmdl`/`.dlf`), QCAT decoding (or a high-fidelity mock), ZIP archiving, and
age/quota log retention — without requiring a Windows desktop on the host that
runs the orchestrator.

Two controller tiers are supported behind a single `QXDMController` interface:

| Mode | When to use | Driver |
|---|---|---|
| **Mock** (`QXDM_MOCK_MODE=True`) | Linux dev / CI, no QXDM hardware | `MockQXDMController` — synthesises a full lifecycle (file creation, incremental growth, rollover, delayed flush, stability detection) |
| **Remote Agent** (`QXDM_MOCK_MODE=False`, not on Windows) | Linux orchestrator + remote Windows QXDM host | `RemoteAgentController` — calls the **QXDM Device Agent** which owns `pywinauto` + `QCAT.exe` |
| **Legacy Same-Host** (`QXDM_MOCK_MODE=False` on Windows) | Single-box Windows test-bench | `RealQXDMController` — `pywinauto` over QXDM GUI |

All three share the same `start_session(dmc_file, duration_sec, prefix,
scenario_name, job_id) -> SessionArtifacts` contract.

---

## Architecture

```
[ TMO Automation Platform / REST Client ]
                    │
                    ▼
       [ FastAPI / Uvicorn Endpoint ]   (Linux, async-IO friendly, sync body)
                    │
                    ▼
       [ QXDM Automation Controller ]
       ├── Mock Controller    (no hardware)
       ├── Remote Agent Ctrl  (HTTP/JSON over network)
       └── Real Controller    (Windows-only pywinauto)
                    │
                    ▼
       [ logs/jobs/<job_id>/raw ]   ← isolated per request, never global
                    │
                    ▼
       [ Log Processing Engine ]
       ├── Synthetic decoder (mock) or QCAT (production; never falls back silently)
       └── ZIP into logs/backup/ + decoded text into logs/converted/
                    │
                    ▼
       [ Log Retention Engine ]    (CLI: `python -m log_rotator`)
```

---

## Directory Layout

```
qxdm_init/
├── __init__.py
├── config.py            # Linux-native paths, hardware config, retention, mock tunables
├── qxdm_service.py      # Abstract controller + Mock/Remote/Real implementations,
│                        # COM-port discovery, file stability helpers
├── log_processor.py     # Per-job isolation, mock vs QCAT decoder, ZIP archive
├── log_rotator.py       # Single-cycle + `python -m log_rotator` daemon
├── api_server.py        # REST API with validated/sanitised request model
├── test_pipeline.py     # Pytest + standalone runner; tmp_path isolated
├── requirements.txt
├── configs/
│   └── default_test.dmc
└── logs/
    ├── jobs/<job_id>/raw    # per-job isolated binaries
    ├── converted/           # global decoded text
    └── backup/              # global ZIP archive (retention enforced here)
```

---

## Key Design Decisions

### Per-job isolation
Every request is assigned a unique `job_id` (auto-generated UUID hex).  The
raw binaries are written to `logs/jobs/<job_id>/raw/`.  The conversion
pipeline **only ever** processes the file list returned by the controller —
it never scans `logs/raw/` globally.  Two simultaneous requests cannot
overwrite or steal each other's logs.

### No silent conversion fallback
In production (`QXDM_MOCK_MODE=False`) if the configured `QCAT.exe` is
missing, `log_processor` raises an explicit `RuntimeError` instead of
silently manufacturing a fake decoded log.  This is intentional — a fake
decode in production would be a debugging nightmare.

### Realistic mock lifecycle
The mock writer is asynchronous: it creates a `_session_001.qmdl`, appends
chunks (configurable `chunk_size_bytes` × `chunk_interval_ms`), rolls over
to `_session_002.qmdl` at the configured size threshold, sleeps for the
configured duration, then flushes.  Tests verify that the new file is
detected *before* the snapshot is overwritten, that rollover really
produces multiple files, and that the file size is stable before
archiving proceeds.

### Linux-first, Windows-optional
No Windows-specific imports (`pywinauto`, `win32com`, PowerShell COM
enumeration) live in `api_server.py` or `qxdm_service.py`.  The remote
agent tier speaks plain HTTP/JSON to whatever Windows host is running the
Device Agent.  The same-host `RealQXDMController` is retained for
single-box Windows testbenches but refuses to run on Linux (it raises a
clear `RuntimeError`).

### Periodic rotation as a daemon
`log_rotator` exposes both a one-shot `run_log_rotation()` function *and*
a long-lived `serve()` loop, callable as `python -m log_rotator` so it can
be scheduled by `systemd` timers or `cron`.

---

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `QXDM_MOCK_MODE` | `True` | Force mock even on hardware hosts |
| `QXDM_DEVICE_AGENT_URL` | `http://127.0.0.1:8765` | RemoteAgent target |
| `QXDM_EXE` | `/opt/qualcomm/QXDM/QXDM.exe` | Local QXDM (Windows) |
| `QCAT_EXE` | `/opt/qualcomm/QCAT/QCAT.exe` | Local QCAT (Windows) |
| `QXDM_COM_PORT` | _(empty)_ | Force a specific DIAG port |
| `QXDM_MAX_LOG_SIZE_MB` | `250` | Log-file rollover threshold |
| `QXDM_DEFAULT_LOG_DURATION_SEC` | `10` | Default logging duration |
| `QXDM_RETENTION_DAYS` | `7` | Age cutoff for archived ZIPs |
| `QXDM_BACKUP_QUOTA_MB` | `1024` | Backup-dir quota |
| `QXDM_ROTATION_INTERVAL_SEC` | `3600` | Daemon tick interval |
| `QXDM_STABILITY_WINDOW_SEC` | `5` | File-stable detection window |
| `QXDM_MAX_WAIT_FOR_LOG_SEC` | `60` | How long to wait for new file |
| `QXDM_API_HOST` | `0.0.0.0` | FastAPI bind host |
| `QXDM_API_PORT` | `8000` | FastAPI bind port |
| `QXDM_MOCK_FAIL` | `none` | `none` / `launch` / `connect` / `no_log` / `crash` |

---

## Running

### 1. Run the integration test (uses `/tmp/qxdm_init_smoke`)

```bash
cd qxdm_init
./../venv/bin/python test_pipeline.py
```

Expected final block:

```
SIMULATED PART-1 PIPELINE PASSED

Validated offline on Linux:
  - REST trigger
  - mock QXDM session orchestration (load/connect/configure)
  - new-file detection after logging start
  - incremental file growth
  - rollover to a second .qmdl when size threshold reached
  - logging stop with delayed flush
  - file stability detection
  - per-job isolation (raw/converted/backup)
  - synthetic binary -> decoded text conversion
  - ZIP archive creation per session
  - raw cleanup after successful archive
  - age-based retention policy
  - quota-based retention policy

NOT validated (requires hardware / Windows host):
  - pywinauto / Win32 QXDM GUI automation
  - Qualcomm DIAG COM port enumeration on Windows
  - real QCAT decoding of live captures
  - remote Device Agent round-trip
  - Windows path handling
```

### 2. Run the full pytest suite

```bash
cd qxdm_init
./../venv/bin/python -m pytest test_pipeline.py -v
```

Coverage:
* `test_pipeline_happy_path`              — capture → decode → archive
* `test_pipeline_isolated_jobs`           — two jobs don't collide
* `test_log_rotator_age_retention`        — age policy prunes old ZIPs
* `test_log_rotator_quota`                — quota policy prunes oldest
* `test_log_rotator_entrypoint_help`      — `python -m log_rotator --help`
* `test_mock_rollover`                    — writer produces multiple files
* `test_mock_launch_failure`              — `QXDM_MOCK_FAIL=launch` raises
* `test_log_rotator_daemon_one_cycle`     — `python -m log_rotator --once`

### 3. Launch the API server

```bash
cd qxdm_init
./../venv/bin/python api_server.py
# Swagger UI: http://127.0.0.1:8000/docs
```

### 4. Schedule the log rotator

```bash
# systemd timer
cat > /etc/systemd/system/qxdm-rotator.service <<'EOF'
[Unit]
Description=QXDM log retention engine

[Service]
WorkingDirectory=/home/pulkit3010/CompNova/qxdm_automation/qxdm_init
ExecStart=/home/pulkit3010/CompNova/qxdm_automation/venv/bin/python -m log_rotator
Restart=on-failure
EOF
```

Or run a one-shot cycle in CI:
```bash
cd qxdm_init && python -m log_rotator --once
```

---

## REST API

### POST `/api/v1/trigger-logging`

```json
{
  "scenario_name": "TMO_5G_SA_Handover_Chamber1",
  "duration_seconds": 15,
  "dmc_config": "configs/default_test.dmc",
  "prefix": "TMO_Lab"
}
```

Returns:
```json
{
  "status": "SUCCESS",
  "job_id": "ab12...",
  "artifacts": {
    "session": { "binary_files": [...], "duration_actual_sec": 15.04, ... },
    "processing": { "artifacts": [...], "failures": [] },
    "rotation": { "pruned_by_age": 0, "pruned_by_quota": 0, ... }
  }
}
```

`scenario_name` and `prefix` are sanitised (path-traversal characters
stripped).  `duration_seconds` is constrained to `[1, 86400]`.  Failed
controller runs surface as `HTTP 500` with a `controller_error:` prefix on
the detail field — never as a fabricated success.

### POST `/api/v1/rotate-logs`

Runs a single retention cycle and returns the same `RotationResult` shape
as the daemon emits internally.

### GET `/`

Health/mode check.

---

## Hardware Production Deployment

When deploying against a real QXDM/QCAT bench:

1. Provision a Windows host (Device Agent) running `pywinauto` and QCAT.
2. Expose its REST surface on `QXDM_DEVICE_AGENT_URL` (the orchestrator
   points at it).
3. On the Linux orchestrator: `export QXDM_MOCK_MODE=False`.  The
   `RemoteAgentController` is selected automatically (it cannot import
   `pywinauto`, so even if the deployment host were a Windows machine,
   Linux deployment still uses the remote agent — and that's the
   intended path).
4. Same-host Windows benches can still use `RealQXDMController` by
   setting `QXDM_MOCK_MODE=False` *and* running `pywinauto`-capable code
   on Windows.
