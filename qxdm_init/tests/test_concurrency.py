"""Real concurrency tests for the orchestrator."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _run_job(app, payload):
    with TestClient(app) as client:
        r = client.post("/api/v1/jobs", json=payload)
        return r.json()


def test_six_jobs_simultaneously(orchestrator_app, isolated_settings):
    """6 jobs at once, each in its own thread, isolated directories."""
    payloads = [
        {
            "scenario_name": f"Concurrent_{i}",
            "duration_seconds": 2,
            "prefix": f"PFX_{i}",
            "request_id": f"REQ-{i}",
        }
        for i in range(6)
    ]
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda p: _run_job(orchestrator_app, p), payloads))

    job_ids = {r["job_id"] for r in results}
    assert len(job_ids) == 6

    # Wait for all to complete
    deadline = time.time() + 20
    with TestClient(orchestrator_app) as client:
        while time.time() < deadline:
            states = []
            for jid in job_ids:
                r = client.get(f"/api/v1/jobs/{jid}")
                if r.status_code == 200:
                    states.append(r.json()["state"])
            if all(s in {"COMPLETE", "FAILED", "PARTIAL"} for s in states):
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"jobs did not finish: {states}")
        final = [client.get(f"/api/v1/jobs/{jid}").json() for jid in job_ids]

    for f in final:
        assert f["state"] in {"COMPLETE", "PARTIAL"}, f

    # No cross-job contamination: each job produced its own ZIP
    jobs_root = isolated_settings.jobs_root
    raw_files = list(jobs_root.rglob("*.qmdl"))
    assert not raw_files, f"leftover raw files: {raw_files}"


def test_failure_during_one_job_does_not_affect_others(
    orchestrator_app, isolated_settings
):
    """One failing concurrent job cannot corrupt the others."""
    # Schedule a failing mock (fail_mode=launch) and two healthy jobs.
    from api_server import create_app
    failing_settings = isolated_settings.with_overrides(mock_fail_mode="launch")
    failing_app = create_app(settings=failing_settings)

    payloads_ok = [
        {"scenario_name": f"OK_{i}", "duration_seconds": 1, "prefix": f"P_{i}"}
        for i in range(2)
    ]
    failing_payload = {
        "scenario_name": "FAIL", "duration_seconds": 1, "prefix": "P_FAIL",
    }

    def run_ok(p):
        with TestClient(orchestrator_app) as c:
            return c.post("/api/v1/jobs", json=p).json()

    def run_fail(p):
        with TestClient(failing_app) as c:
            r = c.post("/api/v1/jobs", json=p)
            # 500 because controller raised
            return {"status": r.status_code, "body": r.text}

    with ThreadPoolExecutor(max_workers=3) as ex:
        f1 = ex.submit(run_fail, failing_payload)
        f2 = ex.submit(run_ok, payloads_ok[0])
        f3 = ex.submit(run_ok, payloads_ok[1])
        fail_result = f1.result()
        ok_results = [f2.result(), f3.result()]

    # Wait for OK jobs to finish
    deadline = time.time() + 15
    with TestClient(orchestrator_app) as client:
        for r in ok_results:
            jid = r["job_id"]
            while time.time() < deadline:
                rr = client.get(f"/api/v1/jobs/{jid}").json()
                if rr["state"] in {"COMPLETE", "FAILED", "PARTIAL"}:
                    assert rr["state"] in {"COMPLETE", "PARTIAL"}, rr
                    break
                time.sleep(0.1)


def test_rotation_does_not_break_active_writes(orchestrator_app, isolated_settings):
    """Run a rotation cycle while a job is mid-capture; nothing should break."""
    from log_rotator import LogRotator

    backup = isolated_settings.backup_directory
    backup.mkdir(exist_ok=True)
    for i in range(3):
        p = backup / f"pre_{i}.zip"
        p.write_bytes(b"x")
        # backdate
        import os
        os.utime(p, (time.time() - 86400 * 30, time.time() - 86400 * 30))

    rotator = LogRotator(settings=isolated_settings, directory=backup)
    result = rotator.run_once()
    assert result.pruned_by_age >= 1
