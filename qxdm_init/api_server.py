"""
TMO QXDM Log Automation REST API (Linux-native).

Endpoints
---------
``GET  /``                       - service info / health
``POST /api/v1/trigger-logging`` - run a complete logging pipeline.
``POST /api/v1/rotate-logs``     - trigger a manual rotation cycle.

Every invocation is given a unique ``job_id`` so the resulting raw
binaries, decoded text files, and ZIP archives can be traced back to the
exact REST request.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

try:
    import config  # type: ignore
    from qxdm_service import (  # type: ignore
        RemoteAgentController,
        SessionArtifacts,
        make_controller,
    )
    from log_processor import ProcessingResult, convert_and_archive  # type: ignore
    from log_rotator import run_log_rotation  # type: ignore
except (ImportError, ModuleNotFoundError):
    from . import config  # type: ignore
    from .qxdm_service import (  # type: ignore
        RemoteAgentController,
        SessionArtifacts,
        make_controller,
    )
    from .log_processor import ProcessingResult, convert_and_archive  # type: ignore
    from .log_rotator import run_log_rotation  # type: ignore


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("QXDM_API")


app = FastAPI(
    title="TMO QXDM Log Automation Service",
    version="1.1.0",
    description=(
        "Linux orchestrator for QXDM/QCAT diagnostic capture and decoding. "
        "Runs in MOCK_MODE by default; flip QXDM_MOCK_MODE=False to dispatch "
        "to a remote Windows Device Agent."
    ),
)


# ---------------------------------------------------------------------------
# Request models (with validation + sanitisation)
# ---------------------------------------------------------------------------
_FILENAME_SAFE_KEEP = (
    "-_.abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


def _sanitize(value: str) -> str:
    """Allow only filename-friendly characters; fall back to ``field``."""
    if value is None:
        return ""
    out = "".join(c if c in _FILENAME_SAFE_KEEP else "_" for c in value)
    out = out.strip("._")
    return out


class LoggingRequest(BaseModel):
    scenario_name: str = Field(
        ...,
        min_length=1,
        max_length=80,
        examples=["TMO_5G_SA_Handover_Chamber1"],
        description="Logical scenario tag. Filename sanitisation applied.",
    )
    duration_seconds: int = Field(
        10,
        ge=1,
        le=86400,
        description="Capture duration in seconds (1 day max).",
    )
    dmc_config: Optional[str] = Field(
        None,
        max_length=260,
        description="Path to a .dmc filter file; defaults to config.DMC_FILE.",
    )
    prefix: Optional[str] = Field(
        "QXDM_Test",
        min_length=1,
        max_length=60,
        description="Filename prefix for generated binaries.",
    )
    job_id: Optional[str] = Field(
        None,
        max_length=64,
        description="Optional explicit job ID for traceability.",
    )
    use_remote_agent: Optional[bool] = Field(
        None,
        description="Override controller selection. Defaults to a remote "
        "agent when MOCK_MODE is off, except on Windows hosts.",
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


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def execute_logging_pipeline(req: LoggingRequest) -> Dict[str, Any]:
    """Full pipeline: capture -> convert -> archive -> rotate."""
    job_id = req.job_id or uuid.uuid4().hex[:12]
    job_dir = config.JOBS_ROOT / job_id
    raw_dir = job_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    dmc_file = req.dmc_config or config.DMC_FILE
    if not Path(dmc_file).exists():
        raise HTTPException(status_code=400, detail=f"DMC config not found: {dmc_file}")

    prefix = f"{req.prefix}_{req.scenario_name}"

    use_remote = req.use_remote_agent
    if use_remote is None:
        use_remote = (not config.MOCK_MODE) and (sys.platform != "win32")

    log.info(
        "[job=%s] starting pipeline (mock_mode=%s, remote_agent=%s)",
        job_id,
        config.MOCK_MODE,
        use_remote,
    )

    controller = make_controller(
        job_id=job_id,
        raw_dir=raw_dir,
        use_remote_agent=use_remote,
    )

    try:
        artifacts: SessionArtifacts = controller.start_session(
            dmc_file=dmc_file,
            duration_sec=int(req.duration_seconds),
            prefix=prefix,
            scenario_name=req.scenario_name,
            job_id=job_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Pipeline controller failed (job=%s)", job_id)
        raise HTTPException(status_code=500, detail=f"controller_error: {exc}") from exc

    processing: ProcessingResult = convert_and_archive(
        scenario_name=req.scenario_name,
        job_id=job_id,
        binary_files=list(artifacts.binary_files),
    )

    rotation = run_log_rotation()

    return {
        "status": "SUCCESS" if processing.ok else "PARTIAL",
        "job_id": job_id,
        "artifacts": {
            "session": artifacts.to_dict(),
            "processing": processing.to_dict(),
            "rotation": rotation.to_dict(),
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "service": "TMO QXDM Log Automation Service",
        "version": app.version,
        "mock_mode": bool(config.MOCK_MODE),
        "status": "ONLINE",
        "jobs_root": str(config.JOBS_ROOT),
        "backup_dir": str(config.BACKUP_DIRECTORY),
    }


@app.post("/api/v1/trigger-logging")
def trigger_logging(request: LoggingRequest) -> Dict[str, Any]:
    """Synchronously run a complete capture/decode/archive pipeline.

    This endpoint blocks for the full ``duration_seconds`` plus
    conversion/archiving overhead.  Wrap calls in a queue if you need
    genuine asynchrony.
    """
    try:
        return execute_logging_pipeline(request)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("trigger-logging failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/v1/rotate-logs")
def trigger_rotation() -> Dict[str, Any]:
    return run_log_rotation().to_dict()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    host = os.getenv("QXDM_API_HOST", "0.0.0.0")
    port = int(os.getenv("QXDM_API_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
