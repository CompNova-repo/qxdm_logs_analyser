"""
Immutable, dependency-injectable settings for the QXDM automation service.

Every controller/processor/retention engine receives a :class:`Settings`
instance instead of mutating module-level globals.  Tests construct their
own ``Settings`` pointing at ``tmp_path``; production loads from environment
variables via :func:`from_env`.

The legacy module-level constants in ``config.py`` continue to exist as a
thin convenience layer for the CLI / smoke tests, but every code path that
runs in production or in pytest must accept a ``Settings`` argument.
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional


def _resolve_jobs_root(base: Path) -> Path:
    return (base / "logs" / "jobs").resolve()


def _default_jobs_root() -> Path:
    # base = parent of settings.py = qxdm_init/
    return _resolve_jobs_root(Path(__file__).resolve().parent)


@dataclass(frozen=True)
class Settings:
    """All tunables for the orchestrator, decoupled from module globals."""

    # --- core directories --------------------------------------------------
    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent)
    jobs_root: Path = field(default_factory=_default_jobs_root)
    converted_directory: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "logs" / "converted")
    backup_directory: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "logs" / "backup")
    configs_directory: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "configs")
    legacy_raw_directory: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "logs" / "raw")
    device_agent_artifact_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "logs" / "device_agent")
    manifests_directory: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "logs" / "manifests")

    # --- controller selection ---------------------------------------------
    mock_mode: bool = True
    force_windows_legacy: bool = False  # opt-in legacy same-host Windows path

    # --- hardware / production paths (Windows-only) ------------------------
    qxdm_exe: str = "/opt/qualcomm/QXDM/QXDM.exe"
    qcat_exe: str = "/opt/qualcomm/QCAT/QCAT.exe"
    qcat_command_template: Optional[str] = None  # e.g. "{qcat_exe} -i {input} -o {output}"
    dmc_file: Optional[str] = None  # resolved at boot from configs_directory

    # --- capture tunables --------------------------------------------------
    max_log_size_mb: int = 250
    default_log_duration_sec: int = 10
    com_port: str = ""  # empty -> auto-detect on the QXDM host

    # --- device agent ------------------------------------------------------
    device_agent_url: str = "http://127.0.0.1:8765"
    device_agent_token: Optional[str] = None   # bearer token sent to agent
    device_agent_timeout_sec: float = 15.0     # network request timeout
    device_agent_poll_initial_sec: float = 0.5
    device_agent_poll_max_sec: float = 5.0
    device_agent_poll_deadline_sec: float = 900.0  # 15 min upper bound for any single capture

    # --- log rotator / retention ------------------------------------------
    retention_days: int = 7
    backup_quota_mb: int = 1024
    rotation_interval_sec: int = 3600
    stability_window_sec: float = 5.0
    max_wait_for_log_sec: float = 60.0
    max_wait_for_stability_sec: float = 120.0

    # --- mock controller tunables -----------------------------------------
    mock_chunk_interval_ms: int = 200
    mock_chunk_size_bytes: int = 4096
    mock_rollover_mb: int = 1
    mock_simulate_flush_delay_sec: float = 1.5
    mock_fail_mode: str = "none"  # none|launch|connect|no_log|crash|flush_timeout

    # --- API server --------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_token: Optional[str] = None  # optional bearer auth on the TMO-facing API

    # --- decoder selection ------------------------------------------------
    # 'mock'   = synthetic decoded text (development)
    # 'external' = call a configured external converter (QCAT)
    # 'none'   = refuse to run the pipeline at all (safest production default)
    decoder_kind: str = "mock"
    decoder_template: Optional[str] = None  # used when decoder_kind='external'

    # -------------------------------------------------------------------
    def with_overrides(self, **kwargs) -> "Settings":
        """Return a copy of these settings with the given fields replaced."""
        return replace(self, **kwargs)

    # -------------------------------------------------------------------
    def ensure_directories(self) -> None:
        for d in (
            self.jobs_root,
            self.converted_directory,
            self.backup_directory,
            self.configs_directory,
            self.legacy_raw_directory,
            self.device_agent_artifact_dir,
            self.manifests_directory,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------
    @property
    def resolved_dmc_file(self) -> Optional[Path]:
        if self.dmc_file:
            p = Path(self.dmc_file)
            return p if p.exists() else None
        candidate = self.configs_directory / "default_test.dmc"
        return candidate if candidate.exists() else None


# ---------------------------------------------------------------------------
# Factory: environment -> Settings
# ---------------------------------------------------------------------------
def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def from_env(env: Optional[dict] = None) -> Settings:
    """Build a :class:`Settings` populated from ``os.environ`` (or ``env``)."""
    e = env if env is not None else os.environ
    base = Path(__file__).resolve().parent

    jobs_root = Path(e.get("QXDM_JOBS_ROOT", base / "logs" / "jobs")).resolve()
    converted = Path(e.get("QXDM_CONVERTED_DIRECTORY", base / "logs" / "converted")).resolve()
    backup = Path(e.get("QXDM_BACKUP_DIRECTORY", base / "logs" / "backup")).resolve()
    configs = Path(e.get("QXDM_CONFIGS_DIRECTORY", base / "configs")).resolve()

    settings = Settings(
        base_dir=base,
        jobs_root=jobs_root,
        converted_directory=converted,
        backup_directory=backup,
        configs_directory=configs,
        legacy_raw_directory=Path(e.get("QXDM_RAW_DIRECTORY", base / "logs" / "raw")).resolve(),
        device_agent_artifact_dir=Path(
            e.get("QXDM_DEVICE_AGENT_ARTIFACT_DIR", base / "logs" / "device_agent")
        ).resolve(),
        manifests_directory=Path(
            e.get("QXDM_MANIFESTS_DIRECTORY", base / "logs" / "manifests")
        ).resolve(),
        mock_mode=_env_bool("QXDM_MOCK_MODE", True),
        force_windows_legacy=_env_bool("QXDM_FORCE_WINDOWS_LEGACY", False),
        qxdm_exe=e.get("QXDM_EXE", "/opt/qualcomm/QXDM/QXDM.exe"),
        qcat_exe=e.get("QCAT_EXE", "/opt/qualcomm/QCAT/QCAT.exe"),
        qcat_command_template=e.get("QXDM_QCAT_TEMPLATE") or None,
        dmc_file=e.get("QXDM_DMC_FILE") or None,
        max_log_size_mb=_env_int("QXDM_MAX_LOG_SIZE_MB", 250),
        default_log_duration_sec=_env_int("QXDM_DEFAULT_LOG_DURATION_SEC", 10),
        com_port=e.get("QXDM_COM_PORT", ""),
        device_agent_url=e.get("QXDM_DEVICE_AGENT_URL", "http://127.0.0.1:8765"),
        device_agent_token=e.get("QXDM_DEVICE_AGENT_TOKEN") or None,
        device_agent_timeout_sec=_env_float("QXDM_DEVICE_AGENT_TIMEOUT_SEC", 15.0),
        device_agent_poll_initial_sec=_env_float("QXDM_DEVICE_AGENT_POLL_INITIAL_SEC", 0.5),
        device_agent_poll_max_sec=_env_float("QXDM_DEVICE_AGENT_POLL_MAX_SEC", 5.0),
        device_agent_poll_deadline_sec=_env_float(
            "QXDM_DEVICE_AGENT_POLL_DEADLINE_SEC", 900.0
        ),
        retention_days=_env_int("QXDM_RETENTION_DAYS", 7),
        backup_quota_mb=_env_int("QXDM_BACKUP_QUOTA_MB", 1024),
        rotation_interval_sec=_env_int("QXDM_ROTATION_INTERVAL_SEC", 3600),
        stability_window_sec=_env_float("QXDM_STABILITY_WINDOW_SEC", 5.0),
        max_wait_for_log_sec=_env_float("QXDM_MAX_WAIT_FOR_LOG_SEC", 60.0),
        max_wait_for_stability_sec=_env_float("QXDM_MAX_WAIT_FOR_STABILITY_SEC", 120.0),
        mock_chunk_interval_ms=_env_int("QXDM_MOCK_CHUNK_MS", 200),
        mock_chunk_size_bytes=_env_int("QXDM_MOCK_CHUNK_BYTES", 4096),
        mock_rollover_mb=_env_int("QXDM_MOCK_ROLLOVER_MB", 1),
        mock_simulate_flush_delay_sec=_env_float("QXDM_MOCK_FLUSH_DELAY_SEC", 1.5),
        mock_fail_mode=e.get("QXDM_MOCK_FAIL", "none"),
        api_host=e.get("QXDM_API_HOST", "0.0.0.0"),
        api_port=_env_int("QXDM_API_PORT", 8000),
        api_token=e.get("QXDM_API_TOKEN") or None,
        decoder_kind=e.get("QXDM_DECODER", "mock"),
        decoder_template=e.get("QXDM_DECODER_TEMPLATE") or None,
    )
    settings.ensure_directories()
    return settings


# ---------------------------------------------------------------------------
# Helpers used by tests and other modules
# ---------------------------------------------------------------------------
def new_job_id() -> str:
    """Generate an internal job_id that is filesystem-safe."""
    return uuid.uuid4().hex


def safe_requester_id(value: str) -> str:
    """Coerce an externally-supplied ``request_id`` into a filesystem-safe string."""
    if value is None:
        return ""
    keep = "-_.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    out = "".join(c if c in keep else "_" for c in value)
    return out.strip("._")[:64] or "external"


def is_windows_host() -> bool:
    return sys.platform == "win32"
