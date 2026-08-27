"""
FastAPI app exposing the QXDM Device Agent surface.

The Linux orchestrator talks to this app over HTTP/JSON.  The app is
backend-agnostic; production deployments wire :class:`WindowsDeviceBackend`
while Linux CI uses :class:`MockDeviceBackend`.

Endpoint summary (see ``protocol.py`` for paths):

* ``POST /api/v1/jobs``              -- create logging job (202 + remote_job_id)
* ``GET  /api/v1/jobs/{id}``         -- status (state, message, artifacts)
* ``GET  /api/v1/jobs/{id}/artifacts``      -- list artifacts
* ``GET  /api/v1/jobs/{id}/artifacts/{aid}``-- download artifact bytes
* ``DELETE /api/v1/jobs/{id}``       -- delete job + artifacts

Optional bearer auth: set ``QXDM_DEVICE_AGENT_TOKEN`` on both sides.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.security.utils import get_authorization_scheme_param

from .backend import DeviceBackend, MockDeviceBackend, build_backend
from .protocol import (
    JobRequest,
    JobState,
    JobStatus,
    ROUTE_CREATE_JOB,
    ROUTE_DELETE_JOB,
    ROUTE_DOWNLOAD_ARTIFACT,
    ROUTE_GET_JOB,
    ROUTE_LIST_ARTIFACTS,
)


log = logging.getLogger("DeviceAgentServer")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(
    backend: Optional[DeviceBackend] = None,
    store_dir: Optional[Path] = None,
    backend_kind: str = "mock",
    qxdm_exe: str = "",
    qcat_exe: str = "",
    bearer_token: Optional[str] = None,
) -> FastAPI:
    app = FastAPI(
        title="QXDM Device Agent",
        version="1.0.0",
        description=(
            "Windows-side agent that controls QXDM/QCAT on behalf of the "
            "Linux orchestrator.  Linux CI runs this with MockDeviceBackend."
        ),
    )

    if backend is None:
        if store_dir is None:
            store_dir = Path(os.getenv("QXDM_DEVICE_AGENT_ARTIFACT_DIR", "./device_agent_store")).resolve()
        backend = build_backend(
            kind=backend_kind, store_dir=store_dir, qxdm_exe=qxdm_exe, qcat_exe=qcat_exe
        )
    app.state.backend = backend
    app.state.bearer_token = bearer_token or os.getenv("QXDM_DEVICE_AGENT_TOKEN") or None

    _wire_routes(app)
    return app


def _wire_routes(app: FastAPI) -> None:
    @app.get("/")
    def root():
        return {"service": "qxdm-device-agent", "version": app.version, "status": "ONLINE"}

    @app.post(ROUTE_CREATE_JOB, status_code=202)
    async def create_job(request: Request):
        _check_auth(request, app.state.bearer_token)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json body")
        try:
            job_request = JobRequest.from_dict(payload)
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad request: {exc}")
        if not job_request.job_id:
            raise HTTPException(status_code=400, detail="job_id required")
        remote_id = app.state.backend.start_job(job_request)
        status = JobStatus(remote_job_id=remote_id, state=JobState.QUEUED, message="queued")
        return status.to_dict()

    @app.get(ROUTE_GET_JOB)
    def get_job(remote_job_id: str, request: Request):
        _check_auth(request, app.state.bearer_token)
        try:
            return app.state.backend.get_status(remote_job_id).to_dict()
        except KeyError:
            raise HTTPException(status_code=404, detail="remote_job_id not found")

    @app.get(ROUTE_LIST_ARTIFACTS)
    def list_arts(remote_job_id: str, request: Request):
        _check_auth(request, app.state.bearer_token)
        try:
            return {
                "remote_job_id": remote_job_id,
                "artifacts": [
                    a.to_dict() for a in app.state.backend.list_artifacts(remote_job_id)
                ],
            }
        except KeyError:
            raise HTTPException(status_code=404, detail="remote_job_id not found")

    @app.get(ROUTE_DOWNLOAD_ARTIFACT)
    def download_artifact(remote_job_id: str, artifact_id: str, request: Request):
        _check_auth(request, app.state.bearer_token)
        try:
            data = app.state.backend.read_artifact(remote_job_id, artifact_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="artifact not found")
        except FileNotFoundError:
            raise HTTPException(status_code=410, detail="artifact gone")
        try:
            meta = next(
                a for a in app.state.backend.list_artifacts(remote_job_id)
                if a.artifact_id == artifact_id
            )
        except StopIteration:
            raise HTTPException(status_code=404, detail="artifact not found")
        return Response(
            content=data,
            media_type=meta.content_type,
            headers={
                "Content-Length": str(meta.size_bytes),
                "X-SHA256": meta.sha256,
                "Content-Disposition": f'attachment; filename="{meta.filename}"',
            },
        )

    @app.delete(ROUTE_DELETE_JOB)
    def delete_job(remote_job_id: str, request: Request):
        _check_auth(request, app.state.bearer_token)
        deleted = app.state.backend.delete_job(remote_job_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="remote_job_id not found")
        return {"deleted": True, "remote_job_id": remote_job_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _check_auth(request: Request, expected_token: Optional[str]) -> None:
    if not expected_token:
        return  # auth disabled
    auth = request.headers.get("Authorization") or ""
    scheme, token = get_authorization_scheme_param(auth)
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(token, expected_token):
        raise HTTPException(status_code=403, detail="invalid bearer token")


# ---------------------------------------------------------------------------
# Convenience: build & run uvicorn
# ---------------------------------------------------------------------------
def main() -> int:  # pragma: no cover - manual run
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the QXDM Device Agent.")
    parser.add_argument("--host", default=os.getenv("QXDM_DEVICE_AGENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("QXDM_DEVICE_AGENT_PORT", "8765")))
    parser.add_argument(
        "--backend",
        default=os.getenv("QXDM_DEVICE_AGENT_BACKEND", "mock"),
        choices=["mock", "windows"],
    )
    parser.add_argument(
        "--store-dir",
        default=os.getenv("QXDM_DEVICE_AGENT_ARTIFACT_DIR", "./device_agent_store"),
    )
    args = parser.parse_args()

    store_dir = Path(args.store_dir).resolve()
    app = create_app(
        store_dir=store_dir,
        backend_kind=args.backend,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
