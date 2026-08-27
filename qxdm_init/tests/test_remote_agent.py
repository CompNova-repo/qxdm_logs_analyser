"""End-to-end tests for the Linux orchestrator talking to a mock Device Agent."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from device_agent.client import RemoteAgentClient
from device_agent.protocol import JobRequest, JobState
from qxdm_service import RemoteAgentController


def test_client_round_trip(device_agent_server: str):
    client = RemoteAgentClient(
        url=device_agent_server,
        request_timeout_sec=2.0,
        poll_initial_sec=0.05,
        poll_max_sec=0.2,
        poll_deadline_sec=10.0,
    )
    req = JobRequest(
        dmc_file="/tmp/x.dmc",
        duration_sec=1,
        prefix="T",
        scenario_name="s",
        job_id="j-1",
        max_log_size_mb=1,
    )
    submitted = client.submit(req)
    assert submitted.state == JobState.QUEUED
    assert submitted.remote_job_id
    terminal = client.wait_until_terminal(submitted.remote_job_id)
    assert terminal.state in (JobState.COMPLETE, JobState.PARTIAL)
    assert terminal.artifacts, "agent should produce at least one artifact"


def test_client_downloads_artifact(device_agent_server: str, tmp_path: Path):
    client = RemoteAgentClient(
        url=device_agent_server,
        request_timeout_sec=2.0,
        poll_initial_sec=0.05,
        poll_max_sec=0.2,
        poll_deadline_sec=10.0,
    )
    submitted = client.submit(
        JobRequest(
            dmc_file="x.dmc", duration_sec=1, prefix="T",
            scenario_name="s", job_id="j-2", max_log_size_mb=1,
        )
    )
    terminal = client.wait_until_terminal(submitted.remote_job_id)
    assert terminal.artifacts
    dest = tmp_path / "downloads"
    dl = client.download_artifact(
        terminal.remote_job_id, terminal.artifacts[0], dest
    )
    assert dl.local_path.exists()
    assert dl.size_bytes == dl.local_path.stat().st_size
    assert len(dl.sha256) == 64


def test_remote_controller_writes_into_job_dir(
    isolated_settings, device_agent_server: str, tmp_path: Path
):
    """RemoteAgentController must download artifacts into logs/jobs/<id>/raw."""
    isolated_settings = isolated_settings.with_overrides(
        mock_mode=False,
        device_agent_url=device_agent_server,
    )
    controller = RemoteAgentController(settings=isolated_settings, job_id="rc-test")
    artifacts = controller.start_session(
        dmc_file="x.dmc", duration_sec=1, prefix="REMOTE",
        scenario_name="s", job_id="rc-test",
    )
    assert artifacts.binary_files
    raw_dir = tmp_path / "jobs" / "rc-test" / "raw"
    for f in artifacts.binary_files:
        assert str(f).startswith(str(raw_dir))
        assert f.exists()


def test_remote_controller_failure_reports_clean_error(
    isolated_settings, device_agent_server: str
):
    isolated_settings = isolated_settings.with_overrides(
        mock_mode=False,
        device_agent_url="http://127.0.0.1:1",  # nothing listens here
    )
    controller = RemoteAgentController(settings=isolated_settings, job_id="x")
    with pytest.raises(Exception) as excinfo:
        controller.start_session(
            dmc_file="x.dmc", duration_sec=1, prefix="x",
            scenario_name="x", job_id="x",
        )
    assert "Device Agent" in str(excinfo.value) or "connection" in str(excinfo.value).lower()


def test_orchestrator_uses_remote_agent_end_to_end(
    isolated_settings, device_agent_server: str, tmp_path: Path
):
    """The orchestrator can submit a job, download artifacts, decode + archive."""
    from fastapi.testclient import TestClient

    isolated_settings = isolated_settings.with_overrides(
        mock_mode=False, device_agent_url=device_agent_server
    )
    from api_server import create_app

    app = create_app(settings=isolated_settings)
    client = TestClient(app)
    response = client.post(
        "/api/v1/trigger-logging",
        json={
            "scenario_name": "REMOTE_SCEN",
            "duration_seconds": 1,
            "dmc_config": str(isolated_settings.dmc_file),
            "prefix": "AGENT",
            "request_id": "TMO-REMOTE-1",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["artifacts"]["processing"]["ok"], body

    job_id = body["job_id"]
    # Job artifacts should be inside the orchestrator's jobs_root
    leftovers = list((tmp_path / "jobs" / job_id / "raw").glob("*"))
    assert not leftovers, leftovers
    # ZIPs should be in backup directory
    zips = list((tmp_path / "backup").glob(f"*_{job_id}_*.zip"))
    assert zips
    # Manifest exists
    manifest_path = tmp_path / "jobs" / job_id / "manifest.json"
    assert manifest_path.exists()


def test_orchestrator_async_lifecycle(
    isolated_settings, device_agent_server: str, tmp_path: Path
):
    """POST /api/v1/jobs returns 202, status endpoint reports progression."""
    from fastapi.testclient import TestClient

    isolated_settings = isolated_settings.with_overrides(
        mock_mode=False, device_agent_url=device_agent_server
    )
    from api_server import create_app

    app = create_app(settings=isolated_settings)
    client = TestClient(app)
    response = client.post(
        "/api/v1/jobs",
        json={
            "scenario_name": "ASYNC_SCEN",
            "duration_seconds": 1,
            "dmc_config": str(isolated_settings.dmc_file),
            "prefix": "ASYNC",
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    deadline = time.time() + 15
    final_state = None
    while time.time() < deadline:
        r = client.get(f"/api/v1/jobs/{job_id}")
        if r.status_code == 200:
            body = r.json()
            final_state = body["state"]
            if final_state in {"COMPLETE", "FAILED", "PARTIAL"}:
                break
        time.sleep(0.1)
    assert final_state in {"COMPLETE", "PARTIAL"}, final_state
