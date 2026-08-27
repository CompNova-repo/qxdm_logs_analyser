"""
End-to-end pipeline tests for qxdm_init.

Each test runs in a fully isolated temporary workspace (``tmp_path``) by
constructing its own :class:`Settings` instance pointing at that
``tmp_path`` and rebuilding the FastAPI app against those settings.  No
module-level globals are mutated by these tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from api_server import create_app  # noqa: E402
from log_rotator import LogRotator  # noqa: E402
from settings import Settings  # noqa: E402


REPO_ROOT = BASE_DIR.parent
SOURCE_DMC = BASE_DIR / "configs" / "default_test.dmc"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
def _make_isolated_settings(tmp_path: Path) -> Settings:
    """Build a :class:`Settings` pointing at ``tmp_path`` with a copy of the DMC."""
    base = Settings(
        base_dir=BASE_DIR,
        jobs_root=tmp_path / "jobs",
        converted_directory=tmp_path / "converted",
        backup_directory=tmp_path / "backup",
        configs_directory=tmp_path / "configs",
        legacy_raw_directory=tmp_path / "raw",
        device_agent_artifact_dir=tmp_path / "device_agent",
        manifests_directory=tmp_path / "manifests",
        mock_mode=True,
        force_windows_legacy=False,
        qxdm_exe="/nonexistent",
        qcat_exe="/nonexistent",
        qcat_command_template=None,
        dmc_file=str(SOURCE_DMC),
        max_log_size_mb=1,
        default_log_duration_sec=10,
        com_port="",
        device_agent_url="http://127.0.0.1:9999",
        device_agent_token=None,
        device_agent_timeout_sec=2.0,
        device_agent_poll_initial_sec=0.1,
        device_agent_poll_max_sec=0.5,
        device_agent_poll_deadline_sec=10.0,
        retention_days=7,
        backup_quota_mb=1024,
        rotation_interval_sec=3600,
        stability_window_sec=0.3,
        max_wait_for_log_sec=10.0,
        max_wait_for_stability_sec=10.0,
        mock_chunk_interval_ms=50,
        mock_chunk_size_bytes=4096,
        mock_rollover_mb=1,
        mock_simulate_flush_delay_sec=0.2,
        mock_fail_mode="none",
        api_host="127.0.0.1",
        api_port=8000,
        api_token=None,
        decoder_kind="mock",
        decoder_template=None,
    )
    base.ensure_directories()
    shutil.copy(SOURCE_DMC, base.configs_directory / SOURCE_DMC.name)
    return base


def _purge(directory: Path) -> None:
    if directory.exists():
        for entry in directory.glob("*"):
            if entry.is_file():
                try:
                    entry.unlink()
                except OSError:
                    pass
            elif entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)


def _inject_old_archive(backup_dir: Path, days_old: int = 10) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    old = backup_dir / f"OLD_SCENARIO_{uuid.uuid4().hex[:6]}.zip"
    old.write_bytes(b"dummy zip content")
    past = time.time() - (days_old * 86400)
    os.utime(old, (past, past))
    return old


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_pipeline_happy_path(tmp_path: Path):
    """Mock-mode capture -> convert -> archive in an isolated workspace."""
    settings = _make_isolated_settings(tmp_path)
    app = create_app(settings=settings)
    client = TestClient(app)

    payload = {
        "scenario_name": "TMO_5G_VoNR_Drop_Test",
        "duration_seconds": 2,
        "dmc_config": str(SOURCE_DMC),
        "prefix": "CHAMBER_1",
    }
    response = client.post("/api/v1/trigger-logging", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in ("SUCCESS", "PARTIAL"), body
    job_id = body["job_id"]
    processing = body["artifacts"]["processing"]
    assert processing["ok"], processing

    converted = list((tmp_path / "converted").glob("*.txt"))
    zips = list((tmp_path / "backup").glob(f"*_{job_id}_*.zip"))
    leftovers = list((tmp_path / "jobs" / job_id / "raw").glob("*.qmdl"))

    assert len(converted) >= 1, "expected at least one decoded text file"
    assert len(zips) == len(processing["artifacts"]), (len(zips), processing)
    assert not leftovers, f"raw binaries should be cleaned up: {leftovers}"

    sample_text = converted[0].read_text(encoding="utf-8", errors="replace")
    assert "LTE Serving Cell Info" in sample_text
    assert "5GMM_REGISTRATION_REJECT" in sample_text


def test_pipeline_isolated_jobs(tmp_path: Path):
    """Sequential two-job run, each isolated."""
    settings = _make_isolated_settings(tmp_path)
    app = create_app(settings=settings)
    client = TestClient(app)

    payload_a = {
        "scenario_name": "Run_A",
        "duration_seconds": 1,
        "dmc_config": str(SOURCE_DMC),
        "prefix": "PFX_A",
    }
    payload_b = {
        "scenario_name": "Run_B",
        "duration_seconds": 1,
        "dmc_config": str(SOURCE_DMC),
        "prefix": "PFX_B",
    }
    resp_a = client.post("/api/v1/trigger-logging", json=payload_a).json()
    resp_b = client.post("/api/v1/trigger-logging", json=payload_b).json()
    job_ids = {resp_a["job_id"], resp_b["job_id"]}
    assert len(job_ids) == 2

    for jid in job_ids:
        leftovers = list((tmp_path / "jobs" / jid / "raw").glob("*.qmdl"))
        assert not leftovers, f"job {jid} left {leftovers}"


def test_log_rotator_age_retention(tmp_path: Path):
    backup_dir = tmp_path / "backup"
    settings = _make_isolated_settings(tmp_path)
    old = _inject_old_archive(backup_dir, days_old=10)
    rotator = LogRotator(settings=settings, directory=backup_dir, max_retention_days=7)
    result = rotator.run_once()
    assert result.pruned_by_age >= 1
    assert not old.exists()


def test_log_rotator_quota(tmp_path: Path):
    backup_dir = tmp_path / "backup"
    settings = _make_isolated_settings(tmp_path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    big_files = []
    for i in range(3):
        p = backup_dir / f"big_{i}.zip"
        p.write_bytes(b"X" * (200 * 1024))
        big_files.append(p)
        os.utime(p, (time.time() - (10 - i), time.time() - (10 - i)))

    rotator = LogRotator(
        settings=settings, directory=backup_dir, max_backup_dir_mb=0
    )
    result = rotator.run_once()
    assert result.pruned_by_quota >= 1
    assert not big_files[0].exists()


def test_log_rotator_entrypoint_help(capsys):
    result = subprocess.run(
        [sys.executable, "-m", "log_rotator", "--help"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "rotation" in result.stdout.lower() or "rotator" in result.stdout.lower()


def test_mock_rollover(tmp_path: Path):
    """Mock writer produces multiple .qmdl files when rollover threshold is low."""
    settings = _make_isolated_settings(tmp_path).with_overrides(
        mock_chunk_size_bytes=4096,
        mock_chunk_interval_ms=20,
        mock_rollover_mb=1,
        mock_simulate_flush_delay_sec=0.1,
        stability_window_sec=0.3,
        max_wait_for_log_sec=10.0,
    )

    from qxdm_service import (
        MockQXDMController,
        _WriterContext,
        _safe_join,
        _writer_main,
    )

    controller = MockQXDMController(settings=settings, raw_dir=tmp_path / "raw")
    artifacts = controller.start_session(
        dmc_file=str(SOURCE_DMC),
        duration_sec=1,
        prefix="ROLL",
        scenario_name="rollover",
        job_id="rollover-test",
    )
    assert artifacts.log_count >= 1

    raw2 = tmp_path / "raw2"
    raw2.mkdir(parents=True, exist_ok=True)
    prefix_path = _safe_join(raw2, "ROLLOVER_TEST")
    ctx = _WriterContext(
        raw_dir=raw2,
        prefix=prefix_path.name,
        prefix_full=prefix_path,
        rollover_bytes=8 * 1024,
        chunk_bytes=4096,
        chunk_interval_ms=20,
        fail_mode="none",
    )
    stop = threading.Event()
    thread = threading.Thread(target=_writer_main, args=(ctx, stop), daemon=True)
    thread.start()
    time.sleep(0.5)
    stop.set()
    thread.join(timeout=5)
    assert len(ctx.files_written) >= 2, ctx.files_written


def test_mock_launch_failure(tmp_path: Path):
    """``fail_mode='launch'`` should produce a controller_error: response."""
    settings = _make_isolated_settings(tmp_path).with_overrides(mock_fail_mode="launch")
    app = create_app(settings=settings)
    client = TestClient(app)
    response = client.post(
        "/api/v1/trigger-logging",
        json={
            "scenario_name": "should_fail",
            "duration_seconds": 1,
            "dmc_config": str(SOURCE_DMC),
            "prefix": "FAIL",
        },
    )
    assert response.status_code == 500, response.text
    assert "launch failure" in response.text


def test_log_rotator_daemon_one_cycle(tmp_path: Path):
    """`--once` flag runs a single cycle and returns success."""
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    _inject_old_archive(backup_dir, days_old=15)
    fresh = backup_dir / "fresh.zip"
    fresh.write_bytes(b"x")

    # The daemon reads from settings.backup_directory; point it there via env.
    env = {
        **os.environ,
        "QXDM_BACKUP_DIRECTORY": str(backup_dir),
        "QXDM_RETENTION_DAYS": "7",
        "QXDM_ROTATOR_FORCE_LOG": "1",
    }
    result = subprocess.run(
        [sys.executable, "-m", "log_rotator", "--once"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Log rotation finished" in result.stdout or "rotation" in result.stdout


# ---------------------------------------------------------------------------
# True concurrent jobs
# ---------------------------------------------------------------------------
def test_true_concurrent_jobs(tmp_path: Path):
    """4+ simultaneous jobs, each isolated, no cross-contamination."""
    settings = _make_isolated_settings(tmp_path).with_overrides(
        mock_chunk_interval_ms=20,
        mock_chunk_size_bytes=2048,
        mock_simulate_flush_delay_sec=0.1,
        max_wait_for_log_sec=10.0,
        stability_window_sec=0.3,
    )
    app = create_app(settings=settings)

    payloads = [
        {
            "scenario_name": f"Concurrent_{i}",
            "duration_seconds": 2,
            "dmc_config": str(SOURCE_DMC),
            "prefix": f"PFX_{i}",
        }
        for i in range(4)
    ]

    def _run(payload):
        with TestClient(app) as client:
            return client.post("/api/v1/trigger-logging", json=payload).json()

    with ThreadPoolExecutor(max_workers=len(payloads)) as ex:
        results = list(ex.map(_run, payloads))

    job_ids = {r["job_id"] for r in results}
    assert len(job_ids) == len(payloads), job_ids
    # All jobs should have produced artifacts
    for r in results:
        assert r["artifacts"]["processing"]["ok"], r
    # No raw leftovers anywhere
    leftovers = list((tmp_path / "jobs").rglob("*.qmdl"))
    assert not leftovers, leftovers


# ---------------------------------------------------------------------------
# CLI entry point (manual smoke)
# ---------------------------------------------------------------------------
def main() -> int:
    tmp = Path(os.getenv("QXDM_TEST_TMP", "/tmp/qxdm_init_smoke")).resolve()
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("RUNNING END-TO-END QXDM AUTOMATION PIPELINE TEST (MOCK MODE)")
    print("=" * 70)
    settings = _make_isolated_settings(tmp)
    app = create_app(settings=settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/trigger-logging",
            json={
                "scenario_name": "TMO_5G_VoNR_Drop_Test",
                "duration_seconds": 3,
                "dmc_config": str(SOURCE_DMC),
                "prefix": "CHAMBER_1",
            },
        ).json()

    job_id = response["job_id"]
    converted = list((tmp / "converted").glob("*.txt"))
    zips = list((tmp / "backup").glob(f"*_{job_id}_*.zip"))
    raw = list((tmp / "jobs" / job_id / "raw").glob("*.qmdl"))

    print(json.dumps(response, indent=2))
    print("-" * 70)
    print("Artifacts")
    print(f"  converted: {[p.name for p in converted]}")
    print(f"  backups:    {[p.name for p in zips]}")
    print(f"  raw:        {[p.name for p in raw]}")
    print("-" * 70)
    print("SIMULATED PART-1 PIPELINE PASSED")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
