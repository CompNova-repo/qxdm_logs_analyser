# QXDM Device Agent Protocol

## Purpose

The Device Agent is a small HTTP/JSON service that runs on the Windows
machine which hosts the real QXDM/QCAT installation.  The Linux
orchestrator never imports ``pywinauto`` or speaks QXDM directly -- it
talks to this service.

This document describes:

1. The HTTP/JSON contract between the orchestrator and the agent.
2. The trust boundary and the artifact-transfer flow.
3. What is currently validated on Linux and what is not.

## Architecture overview

```text
                     LINUX ORCHESTRATOR                         WINDOWS DEVICE AGENT
   ┌──────────────────────────────────────┐                ┌──────────────────────────────────┐
   │  FastAPI / TMO-facing API            │                │  FastAPI / device_agent.server    │
   │        │                             │                │        │                          │
   │  Job runner (background)             │   POST /jobs   │  DeviceBackend (Mock or Windows)  │
   │        │                             │ ─────────────► │        │                          │
   │  RemoteAgentController               │                │  pywinauto / QXDM (Windows only) │
   │        │                             │                │  QCAT / binary->text              │
   │  RemoteAgentClient (stdlib HTTP)     │  GET /jobs     │        │                          │
   │        │                             │ ◄───────────── │  artifact storage (store_dir)    │
   │  download bytes -> logs/jobs/<id>/raw│                │                                  │
   │        │                             │  GET artifacts │  /artifacts/{aid} -> bytes       │
   │  LogProcessor: decode + archive      │ ◄───────────── │                                  │
   └──────────────────────────────────────┘                └──────────────────────────────────┘
```

## Endpoints

All paths are prefixed with ``/api/v1``.

| Method | Path                                      | Body                              | Returns                                   |
|--------|-------------------------------------------|-----------------------------------|------------------------------------------|
| POST   | ``/jobs``                                 | ``JobRequest`` JSON               | ``202`` + ``{remote_job_id, state}``     |
| GET    | ``/jobs/{remote_job_id}``                 | --                                | ``JobStatus`` JSON                        |
| GET    | ``/jobs/{remote_job_id}/artifacts``       | --                                | ``{remote_job_id, artifacts:[]}``         |
| GET    | ``/jobs/{remote_job_id}/artifacts/{aid}`` | --                                | bytes; ``X-SHA256`` header carries digest |
| DELETE | ``/jobs/{remote_job_id}``                 | --                                | ``{deleted: true}``                       |

### Authentication

Optional bearer token via ``QXDM_DEVICE_AGENT_TOKEN``.  When unset the
service is open (development).  When set, the orchestrator must send
``Authorization: Bearer <token>`` on every request.

### Job lifecycle

```
QUEUED -> STARTING -> LOGGING -> STOPPING -> FLUSHING -> TRANSFERRING -> COMPLETE
                                                       \-> PARTIAL / FAILED
```

The orchestrator polls ``GET /jobs/<id>`` with capped exponential
backoff until a terminal state is reached.

## Artifact transfer contract

**Critical rule:** the Device Agent never exposes Windows filesystem
paths.  The Linux orchestrator pulls each artifact by ``artifact_id``,
verifies:

* HTTP status == 200
* ``Content-Length`` matches ``ArtifactMetadata.size_bytes``
* ``sha256`` of downloaded bytes matches ``ArtifactMetadata.sha256`` (and
  the agent's ``X-SHA256`` response header as defence in depth)

Downloaded bytes are written to ``<dest>/<filename>.part``, fsynced,
then renamed atomically to ``<dest>/<filename>``.  Only after the local
copy is verified is the source considered "transferred".

The Device Agent should NOT delete its local copy until the orchestrator
confirms receipt (out of band -- the orchestrator calls ``DELETE`` after
its pipeline finishes processing the job).

## Running the Device Agent

The Linux-runnable mock backend can be started directly:

```bash
cd qxdm_init
python -m device_agent --host 127.0.0.1 --port 8765 --backend mock
```

In production:

```bash
set QXDM_DEVICE_AGENT_TOKEN=...
set QXDM_QXDM_EXE=C:\QXDM\QXDM.exe
set QCAT_EXE=C:\QCAT\QCAT.exe
python -m device_agent --host 0.0.0.0 --port 8765 --backend windows
```

(Note: the ``windows`` backend is a stub pending validation against the
real TMO QXDM installation -- see *What is NOT validated* below.)

## What is validated on Linux now

The entire HTTP/JSON protocol, the mock backend, the Linux-side
orchestrator download flow, checksum + size verification, atomic write
+ rename, polling/backoff, and lifecycle state machine are exercised by
``tests/test_remote_agent.py`` and ``tests/test_concurrency.py``.

## What is NOT validated

* The actual pywinauto/QCAT integration on Windows.
* The exact QXDM menu/control IDs (currently ``UNVERIFIED_QXDM_BUILD_SPECIFIC``).
* Real COM-port discovery on Windows (the Linux orchestrator does NOT
  enumerate ``/sys/class/tty`` for remote jobs -- that responsibility
  belongs to the Device Agent host).
* Real QCAT CLI syntax (the production decoder accepts a configurable
  command template and never silently falls back to the mock decoder).
