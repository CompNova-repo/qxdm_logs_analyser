"""
Device Agent backend interface and implementations.

The HTTP server (see ``server.py``) talks to a :class:`DeviceBackend`.
Two implementations are shipped:

* :class:`MockDeviceBackend` -- simulates a full QXDM logging session on
  any OS.  Used for Linux integration testing.
* :class:`WindowsDeviceBackend` -- actually launches QXDM via
  ``pywinauto`` on Windows.  Lazy-imported so Linux never requires
  pywinauto.
"""

from __future__ import annotations

import abc
import hashlib
import logging
import os
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .protocol import ArtifactMetadata, JobRequest, JobState, JobStatus

log = logging.getLogger("DeviceAgentBackend")


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------
class DeviceBackend(abc.ABC):
    """Abstract QXDM-capturing backend."""

    @abc.abstractmethod
    def start_job(self, request: JobRequest) -> str:
        """Kick off a logging job asynchronously; return a remote_job_id."""

    @abc.abstractmethod
    def get_status(self, remote_job_id: str) -> JobStatus:
        ...

    @abc.abstractmethod
    def list_artifacts(self, remote_job_id: str) -> List[ArtifactMetadata]:
        ...

    @abc.abstractmethod
    def read_artifact(self, remote_job_id: str, artifact_id: str) -> bytes:
        ...

    @abc.abstractmethod
    def delete_job(self, remote_job_id: str) -> bool:
        ...


# ---------------------------------------------------------------------------
# Mock backend (Linux-friendly)
# ---------------------------------------------------------------------------
class _MockJob:
    def __init__(self, request: JobRequest, store_dir: Path, fail_mode: str):
        self.request = request
        self.store_dir = store_dir
        self.fail_mode = fail_mode
        self.remote_job_id = uuid.uuid4().hex[:16]
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.status = JobStatus(
            remote_job_id=self.remote_job_id,
            state=JobState.QUEUED,
            message="queued",
        )
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._files: List[Path] = []
        self._started_at: Optional[float] = None
        self._ended_at: Optional[float] = None

    def launch(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"mock-backend-{self.remote_job_id}",
            daemon=True,
        )
        self._thread.start()

    def _set_state(self, state: JobState, message: str = "") -> None:
        with self._lock:
            self.status.state = state
            self.status.message = message

    def _run(self) -> None:
        try:
            self._started_at = time.time()
            self.status.started_at = self._started_at
            self._set_state(JobState.STARTING, "launching qxdm")
            time.sleep(0.05)
            if self.fail_mode == "launch":
                self._set_state(JobState.FAILED, "launch failure (mock)")
                self._ended_at = time.time()
                self.status.ended_at = self._ended_at
                self.status.duration_actual_sec = (
                    self._ended_at - self._started_at
                )
                return
            self._set_state(JobState.STARTING, "loading dmc")
            time.sleep(0.05)
            self._set_state(JobState.LOGGING, "writing")
            # simulate file growth
            duration = max(0.2, float(self.request.duration_sec))
            prefix = self.request.prefix or "QXDM"
            ext = ".qmdl"
            file_count = max(1, int(self.request.max_log_size_mb) // 250) or 1
            bytes_per_file = 4096
            total_chunks = max(2, int(duration * 4))  # ~4 chunks per second
            for session_idx in range(1, file_count + 1):
                if self.fail_mode == "crash" and session_idx > 1:
                    self._set_state(JobState.FAILED, "writer crash (mock)")
                    self._ended_at = time.time()
                    self.status.ended_at = self._ended_at
                    self.status.duration_actual_sec = (
                        self._ended_at - self._started_at
                    )
                    return
                path = self.store_dir / (
                    f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_session_"
                    f"{session_idx:03d}{ext}"
                )
                with path.open("wb") as fh:
                    for chunk_idx in range(total_chunks // file_count):
                        fh.write(_synthetic_qmdl_chunk(bytes_per_file, chunk_idx))
                        fh.flush()
                        time.sleep(duration / max(1, total_chunks))
                self._files.append(path)
            self._set_state(JobState.STOPPING, "stopping")
            time.sleep(0.1)
            self._set_state(JobState.FLUSHING, "waiting for file flush")
            time.sleep(min(0.3, duration * 0.1))
            self._set_state(JobState.TRANSFERRING, "ready for transfer")
            time.sleep(0.05)
            # register artifacts
            self.status.artifacts = [_artifact_meta(p) for p in self._files]
            self._ended_at = time.time()
            self.status.ended_at = self._ended_at
            self.status.duration_actual_sec = self._ended_at - self._started_at
            if self.fail_mode == "partial":
                self._set_state(JobState.PARTIAL, "partial: only some files")
            else:
                self._set_state(JobState.COMPLETE, "ok")
        except Exception as exc:  # noqa: BLE001
            self._set_state(JobState.FAILED, f"backend exception: {exc}")
            if self._ended_at is None:
                self._ended_at = time.time()
                self.status.ended_at = self._ended_at
                self.status.duration_actual_sec = self._ended_at - (
                    self._started_at or self._ended_at
                )

    def join(self, timeout: float = 5.0) -> bool:
        if self._thread is None:
            return True
        return self._thread.join(timeout)


def _synthetic_qmdl_chunk(size: int, seq: int) -> bytes:
    header = b"\x7E\x00" + seq.to_bytes(4, "big") + os.urandom(2) + b"\x10"
    body = os.urandom(max(1, size - len(header) - 1))
    trailer = b"\x7E"
    return header + body + trailer


def _artifact_meta(path: Path) -> ArtifactMetadata:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            buf = fh.read(64 * 1024)
            if not buf:
                break
            h.update(buf)
            size += len(buf)
    return ArtifactMetadata(
        artifact_id=uuid.uuid4().hex[:16],
        filename=path.name,
        size_bytes=size,
        sha256=h.hexdigest(),
    )


class MockDeviceBackend(DeviceBackend):
    """In-process mock backend used for Linux tests."""

    def __init__(self, store_dir: Path, fail_mode: str = "none"):
        self.store_dir = Path(store_dir).resolve()
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.fail_mode = fail_mode
        self._jobs: Dict[str, _MockJob] = {}
        self._lock = threading.Lock()

    def start_job(self, request: JobRequest) -> str:
        job = _MockJob(request, self.store_dir, self.fail_mode)
        with self._lock:
            self._jobs[job.remote_job_id] = job
        job.launch()
        return job.remote_job_id

    def get_status(self, remote_job_id: str) -> JobStatus:
        with self._lock:
            job = self._jobs.get(remote_job_id)
        if job is None:
            raise KeyError(remote_job_id)
        return job.status

    def list_artifacts(self, remote_job_id: str) -> List[ArtifactMetadata]:
        return list(self.get_status(remote_job_id).artifacts)

    def read_artifact(self, remote_job_id: str, artifact_id: str) -> bytes:
        status = self.get_status(remote_job_id)
        target = None
        for meta in status.artifacts:
            if meta.artifact_id == artifact_id:
                target = meta
                break
        if target is None:
            raise KeyError(artifact_id)
        with self._lock:
            job = self._jobs.get(remote_job_id)
        assert job is not None
        path = next((p for p in job._files if p.name == target.filename), None)
        if path is None or not path.exists():
            raise FileNotFoundError(target.filename)
        with path.open("rb") as fh:
            return fh.read()

    def delete_job(self, remote_job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(remote_job_id, None)
        if job is None:
            return False
        job.join(timeout=5.0)
        for p in job._files:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            job.store_dir.rmdir()
        except OSError:
            pass
        return True


# ---------------------------------------------------------------------------
# Windows backend (lazy imports)
# ---------------------------------------------------------------------------
class WindowsDeviceBackend(DeviceBackend):
    """Real Windows QXDM backend using pywinauto.

    NOT implemented here.  This class exists only so the server can be
    started with the right backend selection; the actual pywinauto/QXDM
    sequences are intentionally deferred to the real TMO QXDM installation
    where they can be validated.  Until then the orchestrator should be
    pointed at ``MockDeviceBackend`` (or any future real backend).
    """

    def __init__(self, qxdm_exe: str, qcat_exe: str, store_dir: Path):
        if sys.platform != "win32":
            raise RuntimeError(
                "WindowsDeviceBackend can only run on Windows hosts."
            )
        # Lazy import -- raises ImportError on Linux.
        try:
            from pywinauto import Application  # type: ignore  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "pywinauto is required for WindowsDeviceBackend; install it "
                "with 'pip install pywinauto' on the Windows Device Agent."
            ) from exc
        self.qxdm_exe = qxdm_exe
        self.qcat_exe = qcat_exe
        self.store_dir = Path(store_dir)
        raise NotImplementedError(
            "WindowsDeviceBackend is a placeholder until real QXDM/QCAT "
            "control is validated against the TMO installation.  Use "
            "MockDeviceBackend for Linux CI."
        )

    def start_job(self, request: JobRequest) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_status(self, remote_job_id: str) -> JobStatus:  # pragma: no cover
        raise NotImplementedError

    def list_artifacts(self, remote_job_id: str) -> List[ArtifactMetadata]:  # pragma: no cover
        raise NotImplementedError

    def read_artifact(self, remote_job_id: str, artifact_id: str) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def delete_job(self, remote_job_id: str) -> bool:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_backend(
    kind: str,
    store_dir: Path,
    qxdm_exe: str = "",
    qcat_exe: str = "",
) -> DeviceBackend:
    kind = (kind or "mock").lower()
    if kind == "mock":
        return MockDeviceBackend(store_dir=store_dir)
    if kind == "windows":
        return WindowsDeviceBackend(
            qxdm_exe=qxdm_exe, qcat_exe=qcat_exe, store_dir=store_dir
        )
    raise ValueError(f"Unknown backend kind: {kind!r}")
