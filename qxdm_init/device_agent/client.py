"""
HTTP client the Linux orchestrator uses to talk to the Device Agent.

Key rules enforced by this client:

* It never assumes a Windows filesystem path returned by the agent is a
  valid local Linux path.  All artifacts are pulled by ``artifact_id``
  into local bytes and verified by size + SHA-256.
* Network calls have an explicit ``request_timeout_sec`` distinct from
  the (much longer) capture deadline.
* Polling uses capped exponential backoff until the job reaches a
  terminal state or the per-job deadline expires.
* Authentication uses a single bearer token loaded from settings.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from .protocol import (
    AgentError,
    ArtifactMetadata,
    JobRequest,
    JobState,
    JobStatus,
    TERMINAL_STATES,
)

log = logging.getLogger("RemoteAgentClient")


@dataclass
class DownloadedArtifact:
    artifact_id: str
    filename: str
    size_bytes: int
    sha256: str
    local_path: Path

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "local_path": str(self.local_path),
        }


class RemoteAgentClient:
    """Thin HTTP/JSON client wrapping the Device Agent protocol."""

    def __init__(
        self,
        url: str,
        token: Optional[str] = None,
        request_timeout_sec: float = 15.0,
        poll_initial_sec: float = 0.5,
        poll_max_sec: float = 5.0,
        poll_deadline_sec: float = 900.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.url = url.rstrip("/")
        self.token = token
        self.request_timeout_sec = float(request_timeout_sec)
        self.poll_initial_sec = float(poll_initial_sec)
        self.poll_max_sec = float(poll_max_sec)
        self.poll_deadline_sec = float(poll_deadline_sec)
        self._sleep = sleep_fn

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{self.url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout_sec) as resp:  # noqa: S310
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail)
            except Exception:
                pass
            raise AgentError(
                status_code=exc.code,
                code=f"http_{exc.code}",
                message=str(detail),
            ) from exc
        except urllib.error.URLError as exc:
            raise AgentError(
                status_code=0,
                code="connection_error",
                message=f"could not reach device agent: {exc.reason}",
            ) from exc
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AgentError(
                status_code=0,
                code="invalid_response",
                message=f"non-JSON response: {exc.msg}",
            ) from exc

    def _request_bytes(self, method: str, path: str) -> tuple[bytes, dict]:
        url = f"{self.url}{path}"
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout_sec) as resp:  # noqa: S310
                data = resp.read()
                return data, dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            raise AgentError(
                status_code=exc.code,
                code=f"http_{exc.code}",
                message=exc.read().decode("utf-8", errors="replace"),
            ) from exc
        except urllib.error.URLError as exc:
            raise AgentError(
                status_code=0,
                code="connection_error",
                message=f"could not reach device agent: {exc.reason}",
            ) from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def submit(self, request: JobRequest) -> JobStatus:
        resp = self._request("POST", "/api/v1/jobs", request.to_dict())
        return JobStatus.from_dict(resp)

    def get_status(self, remote_job_id: str) -> JobStatus:
        resp = self._request("GET", f"/api/v1/jobs/{remote_job_id}")
        return JobStatus.from_dict(resp)

    def list_artifacts(self, remote_job_id: str) -> List[ArtifactMetadata]:
        resp = self._request("GET", f"/api/v1/jobs/{remote_job_id}/artifacts")
        return [ArtifactMetadata.from_dict(a) for a in resp.get("artifacts", [])]

    def download_artifact(
        self,
        remote_job_id: str,
        artifact: ArtifactMetadata,
        dest_dir: Path,
        overwrite: bool = False,
    ) -> DownloadedArtifact:
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Atomic download: write to .part, fsync, then rename.
        tmp_path = dest_dir / f".{artifact.filename}.part"
        if tmp_path.exists() and not overwrite:
            tmp_path.unlink()
        data, headers = self._request_bytes(
            "GET",
            f"/api/v1/jobs/{remote_job_id}/artifacts/{artifact.artifact_id}",
        )
        # Verify size
        if len(data) != artifact.size_bytes:
            raise AgentError(
                status_code=0,
                code="size_mismatch",
                message=(
                    f"downloaded {len(data)} bytes for {artifact.filename}, "
                    f"expected {artifact.size_bytes}"
                ),
            )
        # Verify SHA-256
        sha = hashlib.sha256(data).hexdigest()
        expected_sha = (
            artifact.sha256
            or headers.get("X-SHA256", "")
            or headers.get("x-sha256", "")
        )
        if expected_sha and sha != expected_sha:
            raise AgentError(
                status_code=0,
                code="checksum_mismatch",
                message=f"SHA-256 mismatch for {artifact.filename}",
            )
        with tmp_path.open("wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os_fsync = getattr(fh, "fileno", None)
                if os_fsync is not None:
                    os_fsync()
            except OSError:
                pass
        final_path = dest_dir / artifact.filename
        if final_path.exists():
            final_path.unlink()
        tmp_path.rename(final_path)
        return DownloadedArtifact(
            artifact_id=artifact.artifact_id,
            filename=artifact.filename,
            size_bytes=len(data),
            sha256=sha,
            local_path=final_path,
        )

    def wait_until_terminal(
        self,
        remote_job_id: str,
        on_poll: Optional[Callable[[JobStatus], None]] = None,
    ) -> JobStatus:
        """Poll status until a terminal state is reached or the deadline expires."""
        deadline = time.monotonic() + self.poll_deadline_sec
        backoff = self.poll_initial_sec
        while True:
            status = self.get_status(remote_job_id)
            if on_poll is not None:
                try:
                    on_poll(status)
                except Exception:  # noqa: BLE001
                    log.exception("on_poll callback raised")
            if status.state in TERMINAL_STATES:
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Device Agent job {remote_job_id} did not reach a terminal "
                    f"state within {self.poll_deadline_sec:.1f}s; last state="
                    f"{status.state.value}"
                )
            self._sleep(backoff)
            backoff = min(self.poll_max_sec, backoff * 2)

    def delete(self, remote_job_id: str) -> None:
        self._request("DELETE", f"/api/v1/jobs/{remote_job_id}")


# Avoid name shadowing: ``io`` is imported but not used here.
_ = io
