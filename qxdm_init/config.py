"""
Legacy configuration shim.

The new code path uses :class:`qxdm_init.settings.Settings` for dependency
injection.  This module remains so existing CLI invocations
(``python api_server.py``, ``python -m log_rotator``) and smoke tests
continue to work without code changes.

It is **not** safe to mutate the attributes on this module after import;
production code should construct a ``Settings`` instance instead.  The
shim simply re-exports the values from a default :func:`from_env` call.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure package-relative imports work whether this module is imported as
# ``qxdm_init.config`` or the flat ``config``.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from settings import Settings, from_env  # type: ignore  # noqa: E402
else:
    from .settings import Settings, from_env  # noqa: E402

_DEFAULT = from_env()


# ---------------------------------------------------------------------------
# Backwards-compatible module-level constants
# ---------------------------------------------------------------------------
BASE_DIR = _DEFAULT.base_dir
JOBS_ROOT = _DEFAULT.jobs_root
LOG_DIRECTORY = _DEFAULT.legacy_raw_directory
CONVERTED_DIRECTORY = _DEFAULT.converted_directory
BACKUP_DIRECTORY = _DEFAULT.backup_directory
CONFIGS_DIRECTORY = _DEFAULT.configs_directory
MOCK_MODE = _DEFAULT.mock_mode

DEFAULT_DEVICE_AGENT_URL = _DEFAULT.device_agent_url

QXDM_EXE = _DEFAULT.qxdm_exe
CONVERTER_EXE = _DEFAULT.qcat_exe
DMC_FILE = str(_DEFAULT.resolved_dmc_file) if _DEFAULT.resolved_dmc_file else str(
    _DEFAULT.configs_directory / "default_test.dmc"
)

COM_PORT = _DEFAULT.com_port
MAX_LOG_SIZE_MB = _DEFAULT.max_log_size_mb
DEFAULT_LOG_DURATION_SEC = _DEFAULT.default_log_duration_sec

ROTATION_CONFIG = {
    "max_retention_days": _DEFAULT.retention_days,
    "max_backup_dir_mb": _DEFAULT.backup_quota_mb,
    "check_interval_sec": _DEFAULT.rotation_interval_sec,
    "stability_window_sec": _DEFAULT.stability_window_sec,
    "max_wait_for_log_sec": _DEFAULT.max_wait_for_log_sec,
    "max_wait_for_stability_sec": _DEFAULT.max_wait_for_stability_sec,
}

MOCK_CONFIG = {
    "chunk_interval_ms": _DEFAULT.mock_chunk_interval_ms,
    "chunk_size_bytes": _DEFAULT.mock_chunk_size_bytes,
    "rollover_mb": _DEFAULT.mock_rollover_mb,
    "rollover_chunk_budget": 256,
    "simulate_flush_delay_sec": _DEFAULT.mock_simulate_flush_delay_sec,
    "fail_mode": _DEFAULT.mock_fail_mode,
}


# ---------------------------------------------------------------------------
# Settings accessor (preferred)
# ---------------------------------------------------------------------------
def get_settings() -> Settings:
    """Return the current default :class:`Settings` instance."""
    return _DEFAULT


def reload_from_env() -> Settings:
    """Force-rebuild the default settings from the current environment."""
    global _DEFAULT, JOBS_ROOT, LOG_DIRECTORY, CONVERTED_DIRECTORY, BACKUP_DIRECTORY
    global CONFIGS_DIRECTORY, MOCK_MODE, DMC_FILE, COM_PORT, MAX_LOG_SIZE_MB
    global DEFAULT_LOG_DURATION_SEC, ROTATION_CONFIG, MOCK_CONFIG
    global DEFAULT_DEVICE_AGENT_URL, QXDM_EXE, CONVERTER_EXE

    _DEFAULT = from_env()
    JOBS_ROOT = _DEFAULT.jobs_root
    LOG_DIRECTORY = _DEFAULT.legacy_raw_directory
    CONVERTED_DIRECTORY = _DEFAULT.converted_directory
    BACKUP_DIRECTORY = _DEFAULT.backup_directory
    CONFIGS_DIRECTORY = _DEFAULT.configs_directory
    MOCK_MODE = _DEFAULT.mock_mode
    DEFAULT_DEVICE_AGENT_URL = _DEFAULT.device_agent_url
    QXDM_EXE = _DEFAULT.qxdm_exe
    CONVERTER_EXE = _DEFAULT.qcat_exe
    COM_PORT = _DEFAULT.com_port
    MAX_LOG_SIZE_MB = _DEFAULT.max_log_size_mb
    DEFAULT_LOG_DURATION_SEC = _DEFAULT.default_log_duration_sec
    DMC_FILE = str(_DEFAULT.resolved_dmc_file) if _DEFAULT.resolved_dmc_file else str(
        _DEFAULT.configs_directory / "default_test.dmc"
    )
    ROTATION_CONFIG.update({
        "max_retention_days": _DEFAULT.retention_days,
        "max_backup_dir_mb": _DEFAULT.backup_quota_mb,
        "check_interval_sec": _DEFAULT.rotation_interval_sec,
        "stability_window_sec": _DEFAULT.stability_window_sec,
        "max_wait_for_log_sec": _DEFAULT.max_wait_for_log_sec,
        "max_wait_for_stability_sec": _DEFAULT.max_wait_for_stability_sec,
    })
    MOCK_CONFIG.update({
        "chunk_interval_ms": _DEFAULT.mock_chunk_interval_ms,
        "chunk_size_bytes": _DEFAULT.mock_chunk_size_bytes,
        "rollover_mb": _DEFAULT.mock_rollover_mb,
        "rollover_chunk_budget": 256,
        "simulate_flush_delay_sec": _DEFAULT.mock_simulate_flush_delay_sec,
        "fail_mode": _DEFAULT.mock_fail_mode,
    })
    return _DEFAULT
