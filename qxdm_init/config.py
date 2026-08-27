"""
QXDM Automation Configuration (Linux-native).

All paths are derived from this file's location so the framework works on
any POSIX system without hard-coded Windows drive letters.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Base paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
JOBS_ROOT = BASE_DIR / "logs" / "jobs"
LOG_DIRECTORY = BASE_DIR / "logs" / "raw"          # legacy / fallback raw dir
CONVERTED_DIRECTORY = BASE_DIR / "logs" / "converted"
BACKUP_DIRECTORY = BASE_DIR / "logs" / "backup"
CONFIGS_DIRECTORY = BASE_DIR / "configs"

# Ensure all canonical directories exist.
for _p in [
    JOBS_ROOT,
    LOG_DIRECTORY,
    CONVERTED_DIRECTORY,
    BACKUP_DIRECTORY,
    CONFIGS_DIRECTORY,
]:
    _p.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Mode toggle (mock vs. real QXDM hardware)
# ---------------------------------------------------------------------------
# True  -> Synthetic .qmdl generation, no QXDM/QCAT required.
# False -> Real QXDM hardware on a (remote) Windows Device Agent.
MOCK_MODE = os.getenv("QXDM_MOCK_MODE", "True").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Hardware / production configuration
# ---------------------------------------------------------------------------
# In production the QXDM GUI runs on a *remote* Windows machine; the Linux
# controller speaks to it through a Device Agent HTTP/JSON-RPC interface.
# The Device Agent endpoint and CLI tool overrides come from environment
# variables; sensible defaults are provided for local Linux development.
DEFAULT_DEVICE_AGENT_URL = os.getenv("QXDM_DEVICE_AGENT_URL", "http://127.0.0.1:8765")

# Local tool paths used only on Windows hosts running pywinauto / QCAT.
# Kept here so an operator can override via environment if needed.
QXDM_EXE = os.getenv("QXDM_EXE", "/opt/qualcomm/QXDM/QXDM.exe")
CONVERTER_EXE = os.getenv("QCAT_EXE", "/opt/qualcomm/QCAT/QCAT.exe")
DMC_FILE = str(CONFIGS_DIRECTORY / "default_test.dmc")

COM_PORT = os.getenv("QXDM_COM_PORT", "")  # empty string -> auto-detect
MAX_LOG_SIZE_MB = int(os.getenv("QXDM_MAX_LOG_SIZE_MB", "250"))
DEFAULT_LOG_DURATION_SEC = int(os.getenv("QXDM_DEFAULT_LOG_DURATION_SEC", "10"))

# ---------------------------------------------------------------------------
# Log rotator / retention policy
# ---------------------------------------------------------------------------
ROTATION_CONFIG = {
    "max_retention_days": int(os.getenv("QXDM_RETENTION_DAYS", "7")),
    "max_backup_dir_mb": int(os.getenv("QXDM_BACKUP_QUOTA_MB", "1024")),
    "check_interval_sec": int(os.getenv("QXDM_ROTATION_INTERVAL_SEC", "3600")),
    "stability_window_sec": int(os.getenv("QXDM_STABILITY_WINDOW_SEC", "5")),
    "max_wait_for_log_sec": int(os.getenv("QXDM_MAX_WAIT_FOR_LOG_SEC", "60")),
}

# ---------------------------------------------------------------------------
# Mock controller tunables (Linux-only, used by MockQXDMController)
# ---------------------------------------------------------------------------
MOCK_CONFIG = {
    "chunk_interval_ms": 200,          # how often to append data while logging
    "chunk_size_bytes": 4096,          # payload per tick
    "rollover_mb": 1,                   # rollover threshold (override MAX_LOG_SIZE_MB
                                        # to keep tests fast)
    "rollover_chunk_budget": 256,       # max chunks per file in mocked rollover
    "simulate_flush_delay_sec": 1.5,   # sleep between "stop" and final flush
    "fail_mode": os.getenv("QXDM_MOCK_FAIL", "none"),  # none|launch|connect|no_log|crash
}
