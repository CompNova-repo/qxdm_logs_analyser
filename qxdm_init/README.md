# QXDM Initialization & Automation Framework

Production-oriented Linux orchestration framework with **simulated
QXDM/Device-Agent integration**.  Physical Qualcomm integration
(QXDM/QCAT on a real Windows host) remains **hardware-validation
pending**.

The framework owns everything except the actual QXDM/QCAT execution:

* TMO-facing REST API (job-based)
* Job orchestration and lifecycle tracking
* Scenario/config resolution
* Mock capture lifecycle that imitates the real QXDM flow
* Artifact transfer from the remote Device Agent
* Per-job isolation, transactional decoding/archiving
* Retention (age + quota + active-file protection)

A single ``QXDMController`` interface keeps the QXDM-specific bits
isolated behind either a ``MockQXDMController`` (offline Linux tests) or
a ``RemoteAgentController`` (talks HTTP/JSON to a Windows Device Agent).

---

## Architecture

```text
[ TMO Automation Platform / REST Client ]
        │
        ▼
[ FastAPI / Uvicorn ]   Linux.  POST /api/v1/jobs -> 202 + job_id.
        │
        ▼
[ QXDMController abstraction ]
        ├── MockQXDMController      (synthetic file growth + rollover)
        └── RemoteAgentController   (HTTP/JSON to Device Agent)
                                       │
                                       ▼
                              [ logs/jobs/<uuid>/raw/ ]  -- never scanned globally
        │
        ▼
[ LogProcessor ]  decoder (mock | external) -> text + atomic ZIP archive
        │
        ▼
[ LogRotator ]    age + quota; .part / .hidden / symlinks protected
```

The orchestrator never imports ``pywinauto`` on Linux.  The Device
Agent is a separate small FastAPI service
(``qxdm_init/device_agent/``) that owns the Windows side.

See [DEVICE_AGENT_PROTOCOL.md](DEVICE_AGENT_PROTOCOL.md) for the full
HTTP/JSON contract.

---

## Directory Layout

```
qxdm_init/
├── __init__.py
├── settings.py             # Frozen Settings dataclass (DI), env loader
├── config.py               # Backwards-compat shim, re-exports Settings
├── decoders.py             # LogDecoder interface + Mock + External
├── manifest.py             # JobManifest persistence (atomic JSON)
├── qxdm_service.py         # QXDMController + Mock / Remote / Real impls
├── log_processor.py        # Per-job decode + transactional archive
├── log_rotator.py          # LogRotator (age+quota) + daemon
├── api_server.py           # FastAPI TMO-facing surface
├── device_agent/
│   ├── protocol.py         # Shared HTTP/JSON contract + dataclasses
│   ├── backend.py          # MockDeviceBackend + WindowsDeviceBackend stub
│   ├── server.py           # FastAPI app for the Device Agent
│   ├── client.py           # RemoteAgentClient (stdlib urllib)
│   └── __main__.py         # python -m device_agent
├── tests/                  # pytest suite (55 tests, all Linux)
│   ├── conftest.py
│   ├── test_remote_agent.py
│   ├── test_decoder.py
│   ├── test_api_auth.py
│   ├── test_linux_safety.py
│   ├── test_concurrency.py
│   └── test_retention.py
├── test_pipeline.py        # Legacy happy-path + CLI smoke
├── configs/default_test.dmc
└── DEVICE_AGENT_PROTOCOL.md
```

---

## Validated on Linux now

The following behaviours are exercised by the Linux test suite
(``pytest`` runs entirely without QXDM, QCAT, Windows, pywinauto, or
any Qualcomm hardware):

* TMO-facing API (sync + async endpoints)
* Job lifecycle states: ``QUEUED → STARTING → LOGGING → STOPPING →
  FLUSHING → TRANSFERRING → CONVERTING → ARCHIVING → COMPLETE/FAILED/
  PARTIAL``
* Scenario / DMC config resolution (path traversal rejected)
* Mock capture lifecycle: launch, load DMC, configure, connect, start,
  new-file detection, incremental growth, rollover, stop, delayed
  flush, stability detection
* Per-job isolation: ``logs/jobs/<uuid>/raw/`` -- never globally scanned
* Concurrent jobs (ThreadPoolExecutor with 4-6 simultaneous jobs)
* Cross-job isolation: no leftover binaries, no cross-boundary zips
* Remote Device Agent protocol (mock backend) end-to-end:
  - ``POST /api/v1/jobs`` → ``202 Accepted``
  - status polling with bounded exponential backoff
  - artifact enumeration by ``artifact_id``
  - byte download with size + SHA-256 verification, atomic write
  - local copy into the orchestrator's job dir
  - decoding + archiving from the downloaded bytes
* Decoder adapter:
  - Mock decoder produces clearly-labelled synthetic output
  - External decoder refuses to run without a configured command
    template (no silent fallback)
  - Failed decode / archive preserves the raw binary
* ZIP archive creation is transactional (atomic ``.part`` + rename,
  integrity test, expected entry check); raw is only deleted on success
* Retention:
  - age cutoff
  - quota enforcement (oldest first)
  - active ``.part`` / hidden files ignored
  - symlinks ignored (not followed, not deleted)
  - symlinked backup directory is refused entirely
  - per-file errors don't crash the daemon
* API authentication (optional bearer token)
* Path-safe job IDs and external request IDs

## NOT validated (requires TMO QXDM/QCAT environment)

* Actual QXDM executable behaviour on a Windows host
* Actual QXDM 5.x / 6.x menu accelerators and control IDs -- the
  ``send_keys`` sequences in ``qxdm_service.py`` carry the explicit tag
  ``UNVERIFIED_QXDM_BUILD_SPECIFIC``.  They must be replaced after
  running ``pywinauto``'s ``print_control_identifiers()`` against the
  installed QXDM build.
* Actual QXDM COM-port dialog behaviour
* Actual DIAG connection establishment
* Actual generated file extension(s) produced by a real QXDM build
* Actual QCAT version and CLI syntax (the production decoder accepts a
  configurable command template; no specific flags are hard-coded)
* Real Windows Device Agent ↔ QXDM interaction
* pywinauto / Win32 imports on Linux (intentionally not present)

---

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| ``QXDM_MOCK_MODE`` | ``True`` | Use ``MockQXDMController`` (default in CI/dev) |
| ``QXDM_DEVICE_AGENT_URL`` | ``http://127.0.0.1:8765`` | RemoteAgent target |
| ``QXDM_DEVICE_AGENT_TOKEN`` | _(unset)_ | Bearer token to the Device Agent |
| ``QXDM_DEVICE_AGENT_TIMEOUT_SEC`` | ``15`` | Per-request timeout |
| ``QXDM_DEVICE_AGENT_POLL_DEADLINE_SEC`` | ``900`` | Hard cap for any one capture |
| ``QXDM_API_TOKEN`` | _(unset)_ | Bearer token on the TMO-facing API |
| ``QXDM_API_HOST`` | ``0.0.0.0`` | FastAPI bind host |
| ``QXDM_API_PORT`` | ``8000`` | FastAPI bind port |
| ``QXDM_JOBS_ROOT`` | ``logs/jobs`` | Per-job root |
| ``QXDM_CONVERTED_DIRECTORY`` | ``logs/converted`` | Decoded text output |
| ``QXDM_BACKUP_DIRECTORY`` | ``logs/backup`` | ZIP archives |
| ``QXDM_CONFIGS_DIRECTORY`` | ``configs`` | DMC files |
| ``QXDM_MAX_LOG_SIZE_MB`` | ``250`` | Rollover threshold |
| ``QXDM_RETENTION_DAYS`` | ``7`` | Age cutoff |
| ``QXDM_BACKUP_QUOTA_MB`` | ``1024`` | Quota cap |
| ``QXDM_STABILITY_WINDOW_SEC`` | ``5`` | File-stable detection window |
| ``QXDM_MAX_WAIT_FOR_LOG_SEC`` | ``60`` | Wait for new file after start |
| ``QXDM_DECODER`` | ``mock`` | ``mock`` / ``external`` / ``none`` |
| ``QXDM_DECODER_TEMPLATE`` | _(unset)_ | External decoder command template |
| ``QCAT_EXE`` | ``/opt/.../QCAT.exe`` | Used by external decoder template |
| ``QXDM_MOCK_FAIL`` | ``none`` | ``none``/``launch``/``connect``/``no_log``/``crash``/``flush_timeout`` |
| ``QXDM_ROTATOR_FORCE_LOG`` | _(unset)_ | Force stdout logging for ``log_rotator`` |

For an exhaustive list see ``settings.py``.

---

## REST API

### ``POST /api/v1/jobs``  (asynchronous)

```json
{
  "scenario_name": "TMO_5G_SA_Handover_Chamber1",
  "duration_seconds": 15,
  "dmc_config": "configs/default_test.dmc",
  "prefix": "TMO_Lab",
  "request_id": "TMO-TEST-12345",
  "wait": false
}
```

* Response: ``202 Accepted`` with ``{"job_id": "<uuid>", "status": "QUEUED"}``.
* Poll with ``GET /api/v1/jobs/{job_id}`` until ``state`` is terminal.
* Set ``"wait": true`` for a blocking call that returns the final
  manifest.

### ``GET /api/v1/jobs/{job_id}``

Returns the full ``JobManifest`` (state, message, artifacts, timing,
failure details).  ``job_id`` must match the safe character set
(``[a-z0-9_.-]``); anything else is rejected.

### ``POST /api/v1/jobs/{job_id}/cancel``

Best-effort cancellation.  Returns ``{"cancelled": true/false}``.

### ``POST /api/v1/trigger-logging``  (legacy compat)

Synchronous wrapper.  Internally job-based; included so existing TMO
callers don't break.

### ``POST /api/v1/rotate-logs``

Runs a single retention cycle and returns the structured result.

### ``GET /``

Health check (mode, auth-enabled, decoder kind, key directories).

---

## Running

### Run the full Linux test suite

```bash
cd qxdm_init
python -m pytest -v
```

Current count: **55 tests, all passing**.  No QXDM, QCAT, pywinauto,
Windows, or Qualcomm hardware is required.

### Run the standalone smoke test

```bash
cd qxdm_init
python test_pipeline.py
```

### Launch the orchestrator API

```bash
cd qxdm_init
python api_server.py
# Swagger UI: http://127.0.0.1:8000/docs
```

### Launch the (mock) Device Agent

```bash
cd qxdm_init
python -m device_agent --host 127.0.0.1 --port 8765 --backend mock
```

Then point the orchestrator at it:

```bash
export QXDM_MOCK_MODE=False
export QXDM_DEVICE_AGENT_URL=http://127.0.0.1:8765
python api_server.py
```

### Schedule the log rotator

```ini
# /etc/systemd/system/qxdm-rotator.service
[Unit]
Description=QXDM log retention engine

[Service]
WorkingDirectory=/home/.../qxdm_init
ExecStart=/home/.../venv/bin/python -m log_rotator
Restart=on-failure
```

---

## Key design decisions

### Per-job isolation
Every job is given a UUID ``job_id``; raw binaries live in
``logs/jobs/<job_id>/raw/``.  The conversion pipeline only processes the
file list returned by the controller -- it never scans ``logs/raw/``
globally.  Concurrent jobs cannot overwrite or steal each other's logs.

### No silent production fallback
The decoder is selected via ``Settings.decoder_kind``:

* ``mock`` -- synthetic output (clearly labelled in every produced file).
* ``external`` -- invokes a configured command template.  If the
  template is missing, the pipeline raises immediately rather than
  silently substituting mock output.
* ``none`` -- the pipeline refuses to run; useful for hardening a
  deployment that is not yet wired up.

### Remote artifact transfer
The Device Agent never exposes Windows paths.  The orchestrator
downloads bytes by ``artifact_id``, validates ``Content-Length`` and
``sha256``, writes to a ``.part`` file, fsyncs, and renames atomically.
Only after a verified local copy exists does processing continue.

### Asynchronous TMO jobs
The TMO-facing API does not hold an HTTP request open across multi-minute
captures.  ``POST /api/v1/jobs`` returns ``202 Accepted`` immediately;
status is read via ``GET /api/v1/jobs/{id}``.  The legacy synchronous
endpoint is kept for compatibility.

### Internal IDs vs external request IDs
``job_id`` is always a server-generated UUID; ``request_id`` (if
provided) is sanitised and stored only as metadata.  Path-traversal
sequences in either field are rejected.

### Settings via dependency injection
Controllers/processors/rotators receive a ``Settings`` instance via
the constructor.  No module-level globals are mutated by request
execution; tests construct isolated ``Settings`` instances pointing at
``tmp_path``.
