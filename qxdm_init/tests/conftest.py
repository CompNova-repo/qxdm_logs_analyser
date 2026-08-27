"""Shared fixtures for the qxdm_init tests."""

from __future__ import annotations

import shutil
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from api_server import create_app
from settings import Settings

BASE_DIR = Path(__file__).resolve().parent.parent
SOURCE_DMC = BASE_DIR / "configs" / "default_test.dmc"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def isolated_settings(tmp_path: Path) -> Settings:
    """A ``Settings`` instance pointing at ``tmp_path``."""
    settings = Settings(
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
        device_agent_url="http://127.0.0.1:9",
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
        mock_chunk_interval_ms=20,
        mock_chunk_size_bytes=4096,
        mock_rollover_mb=1,
        mock_simulate_flush_delay_sec=0.1,
        mock_fail_mode="none",
        api_host="127.0.0.1",
        api_port=8000,
        api_token=None,
        decoder_kind="mock",
        decoder_template=None,
    )
    settings.ensure_directories()
    shutil.copy(SOURCE_DMC, settings.configs_directory / SOURCE_DMC.name)
    return settings


@pytest.fixture()
def device_agent_server(tmp_path: Path):
    """Spin up an in-process mock Device Agent and return its base URL."""
    from device_agent.backend import MockDeviceBackend
    from device_agent.server import create_app as create_agent_app

    port = _free_port()
    store_dir = tmp_path / "agent_store"
    backend = MockDeviceBackend(store_dir=store_dir)
    app = create_agent_app(backend=backend, store_dir=store_dir)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5.0
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Device Agent test server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture()
def orchestrator_app(isolated_settings: Settings):
    """A FastAPI orchestrator app wired to isolated settings."""
    return create_app(settings=isolated_settings)
