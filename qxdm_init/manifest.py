"""
Job manifest: a JSON record of everything the orchestrator did for one
job.

Persisting this lets a separate process/agent determine what happened
without re-reading log files or running the controller again.

Lives next to each ``logs/jobs/<job_id>/`` directory as ``manifest.json``.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("QXDM_Manifest")


class LifecycleState(str, enum.Enum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    LOGGING = "LOGGING"
    STOPPING = "STOPPING"
    FLUSHING = "FLUSHING"
    TRANSFERRING = "TRANSFERRING"
    CONVERTING = "CONVERTING"
    ARCHIVING = "ARCHIVING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


TERMINAL_LIFECYCLE = {
    LifecycleState.COMPLETE,
    LifecycleState.PARTIAL,
    LifecycleState.FAILED,
}


@dataclass
class ManifestArtifact:
    filename: str
    size_bytes: int = 0
    sha256: str = ""
    archive_path: Optional[str] = None
    text_log_path: Optional[str] = None
    decoder: str = ""
    decoded_lines: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class JobManifest:
    job_id: str
    request_id: str = ""
    scenario_name: str = ""
    prefix: str = ""
    dmc_config: str = ""
    controller: str = ""
    state: LifecycleState = LifecycleState.QUEUED
    state_message: str = ""
    failure: Optional[str] = None
    capture_start_ts: Optional[float] = None
    capture_end_ts: Optional[float] = None
    capture_duration_sec: float = 0.0
    raw_dir: str = ""
    artifacts: List[ManifestArtifact] = field(default_factory=list)
    remote_job_id: Optional[str] = None
    decoder_label: str = ""
    notes: List[str] = field(default_factory=list)
    events: List[Dict] = field(default_factory=list)

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, payload: Dict) -> "JobManifest":
        return cls(
            job_id=str(payload["job_id"]),
            request_id=str(payload.get("request_id") or ""),
            scenario_name=str(payload.get("scenario_name") or ""),
            prefix=str(payload.get("prefix") or ""),
            dmc_config=str(payload.get("dmc_config") or ""),
            controller=str(payload.get("controller") or ""),
            state=LifecycleState(payload.get("state", LifecycleState.QUEUED.value)),
            state_message=str(payload.get("state_message") or ""),
            failure=payload.get("failure"),
            capture_start_ts=payload.get("capture_start_ts"),
            capture_end_ts=payload.get("capture_end_ts"),
            capture_duration_sec=float(payload.get("capture_duration_sec") or 0.0),
            raw_dir=str(payload.get("raw_dir") or ""),
            artifacts=[ManifestArtifact(**a) for a in payload.get("artifacts") or []],
            remote_job_id=payload.get("remote_job_id"),
            decoder_label=str(payload.get("decoder_label") or ""),
            notes=list(payload.get("notes") or []),
            events=list(payload.get("events") or []),
        )

    # ------------------------------------------------------------------
    def event(self, label: str, **fields) -> None:
        self.events.append(
            {"ts": time.time(), "label": label, **fields}
        )

    # ------------------------------------------------------------------
    def save(self, directory: Path) -> Path:
        """Atomic JSON write so partial manifests never confuse consumers."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "manifest.json"
        fd, tmp_name = tempfile.mkstemp(
            dir=str(directory), prefix=".manifest.", suffix=".part"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2, sort_keys=True)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return target

    @classmethod
    def load(cls, directory: Path) -> Optional["JobManifest"]:
        path = Path(directory) / "manifest.json"
        if not path.exists():
            return None
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            log.warning("Could not load manifest %s: %s", path, exc)
            return None


# ---------------------------------------------------------------------------
# In-memory registry (per-process job index)
# ---------------------------------------------------------------------------
class JobRegistry:
    """Process-wide in-memory job index for the REST API."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[str, JobManifest] = {}

    def upsert(self, manifest: JobManifest) -> None:
        with self._lock:
            self._jobs[manifest.job_id] = manifest

    def get(self, job_id: str) -> Optional[JobManifest]:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> List[JobManifest]:
        with self._lock:
            return list(self._jobs.values())

    def remove(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)


# Module-level singleton for the API process.
DEFAULT_REGISTRY = JobRegistry()
