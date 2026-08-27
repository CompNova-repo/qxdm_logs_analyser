"""
HTTP/JSON contract for the QXDM Windows Device Agent.

The contract is intentionally narrow:

* Jobs are created asynchronously; the agent returns immediately with a
  ``remote_job_id`` and a ``QUEUED`` status.
* The Linux orchestrator polls ``GET /jobs/{remote_job_id}`` until the
  job reaches a terminal state (``COMPLETE`` / ``FAILED`` / ``PARTIAL``).
* Artifact download is by **ID**, never by Windows filesystem path.  The
  Linux orchestrator pulls the bytes into its own ``logs/jobs/<id>/raw/``
  and verifies size + SHA-256 before deleting the raw file again.

This is the only file the client and the server need to agree on.  Any
endpoint naming change goes here.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Lifecycle states
# ---------------------------------------------------------------------------
class JobState(str, enum.Enum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    LOGGING = "LOGGING"
    STOPPING = "STOPPING"
    FLUSHING = "FLUSHING"
    TRANSFERRING = "TRANSFERRING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


TERMINAL_STATES = {JobState.COMPLETE, JobState.FAILED, JobState.PARTIAL}


class AgentError(Exception):
    """Raised on non-2xx responses from the Device Agent."""

    def __init__(self, status_code: int, code: str, message: str, details: Optional[dict] = None):
        super().__init__(f"[{status_code}] {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# Request / response dataclasses (lightweight, no extra dependencies)
# ---------------------------------------------------------------------------
@dataclass
class JobRequest:
    dmc_file: str
    duration_sec: int
    prefix: str
    scenario_name: str
    job_id: str
    max_log_size_mb: int = 250
    extra: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "dmc_file": self.dmc_file,
            "duration_sec": self.duration_sec,
            "prefix": self.prefix,
            "scenario_name": self.scenario_name,
            "job_id": self.job_id,
            "max_log_size_mb": self.max_log_size_mb,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "JobRequest":
        return cls(
            dmc_file=str(payload.get("dmc_file") or ""),
            duration_sec=int(payload.get("duration_sec") or 0),
            prefix=str(payload.get("prefix") or "QXDM"),
            scenario_name=str(payload.get("scenario_name") or "default"),
            job_id=str(payload.get("job_id") or ""),
            max_log_size_mb=int(payload.get("max_log_size_mb") or 250),
            extra=dict(payload.get("extra") or {}),
        )


@dataclass
class ArtifactMetadata:
    artifact_id: str
    filename: str
    size_bytes: int
    sha256: str
    created_at: float = field(default_factory=time.time)
    content_type: str = "application/octet-stream"

    def to_dict(self) -> Dict:
        return {
            "artifact_id": self.artifact_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "content_type": self.content_type,
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "ArtifactMetadata":
        return cls(
            artifact_id=str(payload["artifact_id"]),
            filename=str(payload.get("filename") or ""),
            size_bytes=int(payload.get("size_bytes") or 0),
            sha256=str(payload.get("sha256") or ""),
            created_at=float(payload.get("created_at") or time.time()),
            content_type=str(payload.get("content_type") or "application/octet-stream"),
        )


@dataclass
class JobStatus:
    remote_job_id: str
    state: JobState
    message: str = ""
    artifacts: List[ArtifactMetadata] = field(default_factory=list)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    duration_actual_sec: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "remote_job_id": self.remote_job_id,
            "state": self.state.value,
            "message": self.message,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_actual_sec": self.duration_actual_sec,
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "JobStatus":
        return cls(
            remote_job_id=str(payload["remote_job_id"]),
            state=JobState(payload.get("state", "QUEUED")),
            message=str(payload.get("message") or ""),
            artifacts=[
                ArtifactMetadata.from_dict(a) for a in (payload.get("artifacts") or [])
            ],
            started_at=payload.get("started_at"),
            ended_at=payload.get("ended_at"),
            duration_actual_sec=float(payload.get("duration_actual_sec") or 0.0),
        )


# ---------------------------------------------------------------------------
# API route paths (single source of truth)
# ---------------------------------------------------------------------------
ROUTE_CREATE_JOB = "/api/v1/jobs"
ROUTE_GET_JOB = "/api/v1/jobs/{remote_job_id}"
ROUTE_LIST_ARTIFACTS = "/api/v1/jobs/{remote_job_id}/artifacts"
ROUTE_DOWNLOAD_ARTIFACT = "/api/v1/jobs/{remote_job_id}/artifacts/{artifact_id}"
ROUTE_DELETE_JOB = "/api/v1/jobs/{remote_job_id}"
