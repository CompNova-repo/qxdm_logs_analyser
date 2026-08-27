"""
TMO QXDM Log Automation REST API (Linux-native).

Endpoints
---------
``GET  /``                          -- service info / health
``POST /api/v1/jobs``               -- create job (returns 202 + job_id)
``GET  /api/v1/jobs/{job_id}``      -- status + manifest
``POST /api/v1/jobs/{job_id}/cancel``-- request cancellation (best-effort)
``POST /api/v1/trigger-logging``    -- backwards-compat synchronous wrapper
``POST /api/v1/rotate-logs``        -- run a retention cycle

Authentication
--------------
Set ``QXDM_API_TOKEN`` to enable bearer-token auth.  When unset, the
service runs in dev mode and accepts all callers.  Tests use a custom
``Settings`` instance with a non-empty ``api_token`` to exercise the
protected paths.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security.utils import get_authorization_scheme_param
from pydantic import BaseModel, Field, field_validator

try:
    from .log_processor import LogProcessor, ProcessingResult, convert_and_archive
    from .log_rotator import run_log_rotation
    from .manifest import (
        DEFAULT_REGISTRY,
        JobManifest,
        JobRegistry,
        LifecycleState,
    )
    from .qxdm_service import (
        RemoteAgentController,
        SessionArtifacts,
        make_controller,
    )
    from .settings import Settings, from_env, new_job_id, safe_requester_id
except ImportError:
    from log_processor import LogProcessor, ProcessingResult, convert_and_archive
    from log_rotator import run_log_rotation
    from manifest import (
        DEFAULT_REGISTRY,
        JobManifest,
        JobRegistry,
        LifecycleState,
    )
    from qxdm_service import (
        RemoteAgentController,
        SessionArtifacts,
        make_controller,
    )
    from settings import Settings, from_env, new_job_id, safe_requester_id

log = logging.getLogger("QXDM_API")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(settings: Optional[Settings] = None, registry: Optional[JobRegistry] = None) -> FastAPI:
    settings = settings or from_env()
    registry = registry or DEFAULT_REGISTRY
    app = FastAPI(
        title="TMO QXDM Log Automation Service",
        version="1.2.0",
        description=(
            "Linux orchestrator for QXDM/QCAT diagnostic capture and "
            "decoding.  Runs in MOCK_MODE by default; flip QXDM_MOCK_MODE="
            "False to dispatch to a remote Windows Device Agent."
        ),
    )
    app.state.settings = settings
    app.state.registry = registry
    _wire_routes(app)
    return app


# ---------------------------------------------------------------------------
# Default app instance used by `python api_server.py`.
# Built lazily to ensure _wire_routes is defined first.
# ---------------------------------------------------------------------------
app = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Request models (with validation + sanitisation)
# ---------------------------------------------------------------------------
_FILENAME_SAFE_KEEP = (
    "-_.abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


def _sanitize(value: str) -> str:
    if value is None:
        return ""
    out = "".join(c if c in _FILENAME_SAFE_KEEP else "_" for c in value)
    return out.strip("._")


class LoggingRequest(BaseModel):
    """Request body shared by /jobs and the legacy /trigger-logging."""

    scenario_name: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="Logical scenario tag.",
    )
    duration_seconds: int = Field(10, ge=1, le=86400)
    dmc_config: Optional[str] = Field(None, max_length=260)
    prefix: Optional[str] = Field("QXDM_Test", min_length=1, max_length=60)
    request_id: Optional[str] = Field(
        None,
        max_length=64,
        description="External/traceable identifier (separate from internal job_id).",
    )
    use_remote_agent: Optional[bool] = Field(
        None,
        description="Override controller selection.",
    )
    wait: Optional[bool] = Field(
        False,
        description="If true, /api/v1/jobs blocks until the job is terminal.",
    )

    @field_validator("scenario_name", "prefix")
    @classmethod
    def _check_safe(cls, v: str) -> str:
        if v is None:
            raise ValueError("value is required")
        if v.strip() != v:
            raise ValueError("must not have leading/trailing whitespace")
        if ".." in v.split(os.sep):
            raise ValueError("path traversal not allowed")
        cleaned = _sanitize(v)
        if not cleaned:
            raise ValueError("value contains no safe characters")
        return cleaned

    @field_validator("request_id")
    @classmethod
    def _check_request_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if "/" in v or "\\" in v or v.startswith("..") or v == "..":
            raise ValueError("request_id must not contain path separators or traversal")
        cleaned = safe_requester_id(v)
        if not cleaned:
            raise ValueError("request_id contains no safe characters")
        return cleaned


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _check_auth(request: Request) -> None:
    settings: Settings = request.app.state.settings
    token = settings.api_token
    if not token:
        return
    auth = request.headers.get("Authorization") or ""
    scheme, value = get_authorization_scheme_param(auth)
    if scheme.lower() != "bearer" or not value:
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(value, token):
        raise HTTPException(status_code=403, detail="invalid bearer token")


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------
class _JobRunner:
    """Background executor that runs one job and updates the manifest."""

    def __init__(self, app: FastAPI):
        self.app = app
        self._cancel: Dict[str, threading.Event] = {}

    def cancel(self, job_id: str) -> bool:
        ev = self._cancel.get(job_id)
        if ev is None:
            return False
        ev.set()
        return True

    def run(self, manifest: JobManifest, payload: LoggingRequest) -> None:
        settings: Settings = self.app.state.settings
        registry: JobRegistry = self.app.state.registry
        cancel_event = threading.Event()
        self._cancel[manifest.job_id] = cancel_event

        def _persist() -> None:
            manifest.save(settings.jobs_root / manifest.job_id)

        threading.Thread(
            target=self._run, args=(manifest, payload, cancel_event, _persist),
            daemon=True, name=f"qxdm-job-{manifest.job_id}",
        ).start()

    # ------------------------------------------------------------------
    def _run(
        self,
        manifest: JobManifest,
        payload: LoggingRequest,
        cancel_event: threading.Event,
        persist,
    ) -> None:
        settings: Settings = self.app.state.settings
        registry: JobRegistry = self.app.state.registry

        try:
            dmc_file = payload.dmc_config or (
                str(settings.resolved_dmc_file)
                if settings.resolved_dmc_file
                else str(settings.configs_directory / "default_test.dmc")
            )
            if not Path(dmc_file).exists():
                raise FileNotFoundError(f"DMC config not found: {dmc_file}")

            prefix = f"{payload.prefix}_{payload.scenario_name}"

            manifest.controller = (
                "mock" if settings.mock_mode else
                ("remote" if sys.platform != "win32" else "windows-legacy")
            )
            manifest.state = LifecycleState.STARTING
            manifest.dmc_config = dmc_file
            manifest.prefix = prefix
            persist()
            registry.upsert(manifest)

            job_dir = settings.jobs_root / manifest.job_id
            raw_dir = job_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            manifest.raw_dir = str(raw_dir)

            controller = make_controller(
                settings=settings,
                job_id=manifest.job_id,
                raw_dir=raw_dir,
                use_remote_agent=payload.use_remote_agent,
            )

            manifest.state = LifecycleState.LOGGING
            manifest.capture_start_ts = time.time()
            persist()

            if cancel_event.is_set():
                manifest.state = LifecycleState.FAILED
                manifest.failure = "cancelled_before_start"
                persist()
                return

            artifacts: SessionArtifacts = controller.start_session(
                dmc_file=dmc_file,
                duration_sec=int(payload.duration_seconds),
                prefix=prefix,
                scenario_name=payload.scenario_name,
                job_id=manifest.job_id,
            )
            manifest.remote_job_id = artifacts.remote_job_id
            manifest.capture_end_ts = time.time()
            manifest.capture_duration_sec = artifacts.duration_actual_sec
            manifest.notes.extend(artifacts.notes)

            if cancel_event.is_set():
                manifest.state = LifecycleState.FAILED
                manifest.failure = "cancelled_during_capture"
                persist()
                return

            manifest.state = LifecycleState.CONVERTING
            persist()

            processor = LogProcessor(settings=settings)
            result = processor.convert_and_archive(
                scenario_name=payload.scenario_name,
                job_id=manifest.job_id,
                binary_files=list(artifacts.binary_files),
            )
            manifest.decoder_label = (
                result.artifacts[0].decoder if result.artifacts else "none"
            )
            for art in result.artifacts:
                manifest.artifacts.append(art.to_manifest())

            if result.failures:
                manifest.state = LifecycleState.PARTIAL
                manifest.failure = "; ".join(
                    f["reason"] for f in result.failures
                )
            else:
                manifest.state = LifecycleState.COMPLETE
            persist()
        except Exception as exc:  # noqa: BLE001
            log.exception("Job %s failed", manifest.job_id)
            manifest.state = LifecycleState.FAILED
            manifest.failure = f"{type(exc).__name__}: {exc}"
            persist()
        finally:
            self.app.state.registry.upsert(manifest)
            self._cancel.pop(manifest.job_id, None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _wire_routes(app: FastAPI) -> None:
    runner = _JobRunner(app)
    app.state.runner = runner

    # ------------------------------------------------------------------
    @app.get("/")
    async def root(_request: Request):
        settings = app.state.settings
        return {
            "service": "TMO QXDM Log Automation Service",
            "version": app.version,
            "mock_mode": bool(settings.mock_mode),
            "auth_enabled": bool(settings.api_token),
            "status": "ONLINE",
            "jobs_root": str(settings.jobs_root),
            "backup_dir": str(settings.backup_directory),
            "converted_dir": str(settings.converted_directory),
            "decoder": settings.decoder_kind,
            "device_agent_url": settings.device_agent_url,
        }

    # ------------------------------------------------------------------
    @app.post("/api/v1/jobs", status_code=202)
    async def create_job(request: Request, payload: LoggingRequest):
        _check_auth(request)
        settings = app.state.settings
        registry: JobRegistry = app.state.registry

        job_id = new_job_id()
        manifest = JobManifest(
            job_id=job_id,
            request_id=payload.request_id or "",
            scenario_name=payload.scenario_name,
            state=LifecycleState.QUEUED,
        )
        manifest.event("queued")
        registry.upsert(manifest)
        manifest.save(settings.jobs_root / job_id)

        if payload.wait:
            runner.run(manifest, payload)
            # Block until terminal state
            deadline = time.time() + float(
                settings.device_agent_poll_deadline_sec
            )
            while True:
                m = registry.get(job_id)
                if m and m.state.value in {
                    LifecycleState.COMPLETE.value,
                    LifecycleState.FAILED.value,
                    LifecycleState.PARTIAL.value,
                }:
                    return m.to_dict()
                if time.time() > deadline:
                    return m.to_dict() if m else {"job_id": job_id, "state": "TIMEOUT"}
                time.sleep(0.1)

        runner.run(manifest, payload)
        return {"job_id": job_id, "status": manifest.state.value}

    # ------------------------------------------------------------------
    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        _check_auth(request)
        if not _is_safe_id(job_id):
            raise HTTPException(status_code=400, detail="invalid job_id")
        manifest = app.state.registry.get(job_id)
        if manifest is None:
            on_disk = JobManifest.load(app.state.settings.jobs_root / job_id)
            if on_disk is None:
                raise HTTPException(status_code=404, detail="job not found")
            app.state.registry.upsert(on_disk)
            manifest = on_disk
        return manifest.to_dict()

    # ------------------------------------------------------------------
    @app.post("/api/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request):
        _check_auth(request)
        if not _is_safe_id(job_id):
            raise HTTPException(status_code=400, detail="invalid job_id")
        ok = app.state.runner.cancel(job_id)
        return {"job_id": job_id, "cancelled": ok}

    # ------------------------------------------------------------------
    @app.post("/api/v1/trigger-logging")
    async def trigger_logging(request: Request, payload: LoggingRequest):
        """Backwards-compatible synchronous wrapper.

        The internal architecture is job-based; this endpoint waits for
        the job to complete and returns the full result.  New callers
        should use ``POST /api/v1/jobs`` with ``wait=true``.
        """
        _check_auth(request)
        try:
            settings: Settings = app.state.settings
            registry: JobRegistry = app.state.registry

            job_id = new_job_id()
            manifest = JobManifest(
                job_id=job_id,
                request_id=payload.request_id or "",
                scenario_name=payload.scenario_name,
                state=LifecycleState.QUEUED,
            )
            registry.upsert(manifest)
            manifest.save(settings.jobs_root / job_id)

            dmc_file = payload.dmc_config or (
                str(settings.resolved_dmc_file)
                if settings.resolved_dmc_file
                else str(settings.configs_directory / "default_test.dmc")
            )
            if not Path(dmc_file).exists():
                raise HTTPException(status_code=400, detail=f"DMC config not found: {dmc_file}")

            prefix = f"{payload.prefix}_{payload.scenario_name}"
            job_dir = settings.jobs_root / job_id
            raw_dir = job_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)

            controller = make_controller(
                settings=settings,
                job_id=job_id,
                raw_dir=raw_dir,
                use_remote_agent=payload.use_remote_agent,
            )

            try:
                artifacts = controller.start_session(
                    dmc_file=dmc_file,
                    duration_sec=int(payload.duration_seconds),
                    prefix=prefix,
                    scenario_name=payload.scenario_name,
                    job_id=job_id,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Pipeline controller failed (job=%s)", job_id)
                manifest.state = LifecycleState.FAILED
                manifest.failure = str(exc)
                manifest.save(settings.jobs_root / job_id)
                raise HTTPException(status_code=500, detail=f"controller_error: {exc}") from exc

            processor = LogProcessor(settings=settings)
            processing = processor.convert_and_archive(
                scenario_name=payload.scenario_name,
                job_id=job_id,
                binary_files=list(artifacts.binary_files),
            )
            for art in processing.artifacts:
                manifest.artifacts.append(art.to_manifest())
            if processing.failures:
                manifest.state = LifecycleState.PARTIAL
                manifest.failure = "; ".join(f["reason"] for f in processing.failures)
            else:
                manifest.state = LifecycleState.COMPLETE
            manifest.save(settings.jobs_root / job_id)
            registry.upsert(manifest)

            return {
                "status": "SUCCESS" if processing.ok else "PARTIAL",
                "job_id": job_id,
                "artifacts": {
                    "session": artifacts.to_dict(),
                    "processing": processing.to_dict(),
                },
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("trigger-logging failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    @app.post("/api/v1/rotate-logs")
    async def trigger_rotation(request: Request):
        _check_auth(request)
        settings: Settings = app.state.settings
        return run_log_rotation(settings=settings).to_dict()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_safe_id(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    return all(c in "abcdefghijklmnopqrstuvwxyz0123456789-_." for c in value)


# ---------------------------------------------------------------------------
# Build the default app NOW that all helpers are defined.
# ---------------------------------------------------------------------------
# NOTE: ``create_app`` already wires routes, so the default instance is
# built the same way the tests build theirs.
app = create_app()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    settings = app.state.settings
    host = settings.api_host
    port = settings.api_port
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
