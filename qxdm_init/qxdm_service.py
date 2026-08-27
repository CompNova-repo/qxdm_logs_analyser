"""
QXDM Controller Layer.

This module is the *only* place where Windows/QXDM specifics (send_keys,
``%f``/``%c`` accelerators, etc.) are kept.  Everything else in the
project speaks to the abstract :class:`QXDMController`.

Key design points
-----------------

* All Windows-only logic is **lazy-imported** so the Linux orchestrator
  never requires ``pywinauto`` or any ``win32`` API just to import.
* Remote execution uses an artifact-ID protocol, not Windows filesystem
  paths.  See ``device_agent/client.py``.
* ``COM port`` enumeration lives on the QXDM host only.  The Linux
  orchestrator does not enumerate ``/dev/tty`` for remote jobs.
* The QXDM menu accelerators remain explicitly tagged
  ``UNVERIFIED_QXDM_BUILD_SPECIFIC`` until they are validated against
  the actual TMO QXDM installation.
"""

from __future__ import annotations

import abc
import json
import logging
import os
import shutil
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, TYPE_CHECKING

try:
    from .settings import Settings, is_windows_host, new_job_id
    from .device_agent.client import (
        DownloadedArtifact,
        RemoteAgentClient,
    )
    from .device_agent.protocol import JobRequest, JobState
except ImportError:
    from settings import Settings, is_windows_host, new_job_id  # type: ignore
    from device_agent.client import (  # type: ignore
        DownloadedArtifact,
        RemoteAgentClient,
    )
    from device_agent.protocol import JobRequest, JobState  # type: ignore

if TYPE_CHECKING:
    from .manifest import JobManifest  # noqa: F401

log = logging.getLogger("QXDM_Service")


# ---------------------------------------------------------------------------
# Manifest helper -- the orchestrator's record of every job
# ---------------------------------------------------------------------------
@dataclass
class SessionArtifacts:
    """Files and metadata produced by a single QXDM logging session."""

    job_id: str
    scenario_name: str
    prefix: str
    raw_dir: Path
    binary_files: List[Path] = field(default_factory=list)
    downloaded_artifacts: List[DownloadedArtifact] = field(default_factory=list)
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    ended_at: Optional[str] = None
    duration_sec: int = 0
    duration_actual_sec: float = 0.0
    log_count: int = 0
    notes: List[str] = field(default_factory=list)
    remote_job_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "scenario_name": self.scenario_name,
            "prefix": self.prefix,
            "raw_dir": str(self.raw_dir),
            "binary_files": [str(p) for p in self.binary_files],
            "downloaded_artifacts": [a.to_dict() for a in self.downloaded_artifacts],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_sec": self.duration_sec,
            "duration_actual_sec": round(self.duration_actual_sec, 3),
            "log_count": self.log_count,
            "notes": list(self.notes),
            "remote_job_id": self.remote_job_id,
        }


# ---------------------------------------------------------------------------
# Path safety helpers
# ---------------------------------------------------------------------------
def _safe_join(directory: Path, *parts: str) -> Path:
    """Join ``parts`` onto ``directory`` while preventing path traversal."""
    target = directory.joinpath(*parts).resolve()
    directory_resolved = directory.resolve()
    try:
        target.relative_to(directory_resolved)
    except ValueError as exc:
        raise ValueError(f"unsafe path segment: {parts!r}") from exc
    return target


def _list_binary_files(directory: Path) -> List[Path]:
    patterns = ("*.dlf", "*.bin", "*.isf", "*.hdf", "*.qmdl")
    found: List[Path] = []
    for pattern in patterns:
        found.extend(sorted(directory.glob(pattern)))
    seen: set = set()
    unique: List[Path] = []
    for f in found:
        if f not in seen:
            unique.append(f)
            seen.add(f)
    return unique


def wait_for_new_log(
    watch_dir: Path,
    snapshot: Iterable[str],
    timeout_sec: float,
    poll_interval_sec: float = 0.5,
) -> List[Path]:
    """Block until at least one new binary file appears in ``watch_dir``."""
    snapshot = set(snapshot)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        current = _list_binary_files(watch_dir)
        new_paths = [p for p in current if str(p.resolve()) not in snapshot]
        if new_paths:
            return new_paths
        time.sleep(poll_interval_sec)
    raise TimeoutError(
        f"No new log file appeared in {watch_dir} within {timeout_sec:.1f}s"
    )


def wait_for_file_stability(
    paths: Iterable[Path],
    window_sec: float,
    poll_interval_sec: float = 0.5,
    timeout_sec: float = 120.0,
) -> None:
    """Block until the size of each ``path`` is unchanged for ``window_sec``."""
    paths = [Path(p) for p in paths]
    deadline = time.monotonic() + timeout_sec
    last_sizes = {p: -1 for p in paths}
    stable_since: dict = {}

    while time.monotonic() < deadline:
        now = time.monotonic()
        all_stable = True
        for p in paths:
            if not p.exists():
                all_stable = False
                break
            size = p.stat().st_size
            if size != last_sizes[p]:
                last_sizes[p] = size
                stable_since[p] = now
                all_stable = False
                continue
            if stable_since.get(p, 0) == 0:
                stable_since[p] = now
                all_stable = False
                continue
            elapsed = now - stable_since[p]
            if elapsed < window_sec:
                all_stable = False
        if all_stable and paths:
            return
        time.sleep(poll_interval_sec)
    raise TimeoutError(
        f"Files {paths} did not stabilise within {timeout_sec:.1f}s"
    )


# ---------------------------------------------------------------------------
# Controller interface
# ---------------------------------------------------------------------------
class QXDMController(abc.ABC):
    """Abstract logging driver.

    Implementations MUST return a :class:`SessionArtifacts` describing the
    exact set of binary files belonging to this session, even on the
    failure path.
    """

    @abc.abstractmethod
    def start_session(
        self,
        dmc_file: str,
        duration_sec: int,
        prefix: str,
        scenario_name: str,
        job_id: Optional[str] = None,
    ) -> SessionArtifacts: ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def make_controller(
    settings: Settings,
    job_id: Optional[str] = None,
    raw_dir: Optional[Path] = None,
    use_remote_agent: Optional[bool] = None,
    client: Optional[RemoteAgentClient] = None,
) -> QXDMController:
    """Select the right driver based on settings.

    Selection rules:
      * ``settings.mock_mode=True``           -> ``MockQXDMController``
      * ``settings.mock_mode=False`` and the orchestrator is on Linux and
        either ``use_remote_agent`` is true OR the platform is Linux ->
        ``RemoteAgentController`` (talks to a Windows Device Agent).
      * ``settings.mock_mode=False`` on a Windows host -> the legacy
        ``RealQXDMController`` is acceptable but only if
        ``force_windows_legacy`` is set.
    """
    if settings.mock_mode:
        return MockQXDMController(settings=settings, job_id=job_id, raw_dir=raw_dir)
    if is_windows_host():
        if settings.force_windows_legacy:
            return RealQXDMController(
                settings=settings, job_id=job_id, raw_dir=raw_dir
            )
        # Even on Windows, prefer the remote path so the same orchestrator
        # binary can be deployed on Linux CI hosts.
        return RemoteAgentController(
            settings=settings, job_id=job_id, raw_dir=raw_dir, client=client
        )
    # Linux orchestrator, production mode: must use the remote path.
    if use_remote_agent is False:
        raise RuntimeError(
            "QXDM_MOCK_MODE=False on Linux requires the remote agent "
            "path.  Set use_remote_agent=True or start a Device Agent."
        )
    return RemoteAgentController(
        settings=settings, job_id=job_id, raw_dir=raw_dir, client=client
    )


# ---------------------------------------------------------------------------
# Remote Agent controller
# ---------------------------------------------------------------------------
class RemoteAgentController(QXDMController):
    """Drive a remote Windows QXDM Device Agent.

    Submission is async: we POST and then poll until a terminal state is
    reached (or the per-job deadline expires).  Artifact transfer uses
    artifact IDs (not Windows paths); bytes are validated against size
    and SHA-256 before they are added to the session.
    """

    def __init__(
        self,
        settings: Settings,
        job_id: Optional[str] = None,
        raw_dir: Optional[Path] = None,
        client: Optional[RemoteAgentClient] = None,
    ):
        self.settings = settings
        self.job_id = job_id or new_job_id()
        self.raw_dir_override = Path(raw_dir) if raw_dir else None
        self.client = client or RemoteAgentClient(
            url=settings.device_agent_url,
            token=settings.device_agent_token,
            request_timeout_sec=settings.device_agent_timeout_sec,
            poll_initial_sec=settings.device_agent_poll_initial_sec,
            poll_max_sec=settings.device_agent_poll_max_sec,
            poll_deadline_sec=settings.device_agent_poll_deadline_sec,
        )

    def start_session(
        self,
        dmc_file: str,
        duration_sec: int,
        prefix: str,
        scenario_name: str,
        job_id: Optional[str] = None,
    ) -> SessionArtifacts:
        if job_id:
            self.job_id = job_id
        raw_dir = self.raw_dir_override or (
            self.settings.jobs_root / self.job_id / "raw"
        )
        raw_dir.mkdir(parents=True, exist_ok=True)

        artifacts = SessionArtifacts(
            job_id=self.job_id,
            scenario_name=scenario_name,
            prefix=prefix,
            raw_dir=raw_dir,
            duration_sec=duration_sec,
        )
        artifacts.notes.append(
            f"device_agent_url={self.settings.device_agent_url}"
        )

        request = JobRequest(
            dmc_file=dmc_file,
            duration_sec=int(duration_sec),
            prefix=prefix,
            scenario_name=scenario_name,
            job_id=self.job_id,
            max_log_size_mb=self.settings.max_log_size_mb,
        )
        log.info(
            "Submitting job %s to Device Agent %s (timeout=%.1fs, deadline=%.1fs)",
            self.job_id,
            self.settings.device_agent_url,
            self.settings.device_agent_timeout_sec,
            self.settings.device_agent_poll_deadline_sec,
        )

        submitted = self.client.submit(request)
        artifacts.remote_job_id = submitted.remote_job_id
        artifacts.notes.append(f"remote_job_id={submitted.remote_job_id}")

        terminal = self.client.wait_until_terminal(submitted.remote_job_id)

        if terminal.state in (JobState.FAILED,):
            raise RuntimeError(
                f"Device Agent reported failure: state={terminal.state.value} "
                f"message={terminal.message!r}"
            )

        downloaded: List[DownloadedArtifact] = []
        for meta in terminal.artifacts:
            try:
                dl = self.client.download_artifact(
                    remote_job_id=terminal.remote_job_id,
                    artifact=meta,
                    dest_dir=raw_dir,
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "Artifact download failed: %s artifact_id=%s",
                    exc,
                    meta.artifact_id,
                )
                raise
            downloaded.append(dl)

        artifacts.binary_files = [Path(d.local_path) for d in downloaded]
        artifacts.downloaded_artifacts = downloaded
        artifacts.duration_actual_sec = float(terminal.duration_actual_sec)
        artifacts.ended_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        artifacts.log_count = len(artifacts.binary_files)
        if terminal.state == JobState.PARTIAL:
            artifacts.notes.append(
                f"agent_state=PARTIAL message={terminal.message!r}"
            )
        return artifacts


# ---------------------------------------------------------------------------
# Legacy same-host Windows controller (pywinauto)
# ---------------------------------------------------------------------------
class RealQXDMController(QXDMController):
    """Same-host pywinauto driver.

    Only usable on Windows hosts with pywinauto installed.  All
    QXDM-specific GUI sequences are marked
    ``UNVERIFIED_QXDM_BUILD_SPECIFIC`` because they were derived from
    prototype documents that do not match a specific installed QXDM
    build; they require validation against the actual TMO QXDM
    installation before being used in production.
    """

    # The following sequence of ``send_keys`` calls is UNVERIFIED and
    # exists only as a starting point for integration with a real QXDM
    # installation.  Real accelerators/control IDs MUST be inspected via
    # ``pywinauto.controls.common_controls.print_control_identifiers()``
    # once the Windows host is available.
    UNVERIFIED_QXDM_BUILD_SPECIFIC = True

    def __init__(
        self,
        settings: Settings,
        job_id: Optional[str] = None,
        raw_dir: Optional[Path] = None,
    ):
        if not is_windows_host():
            raise RuntimeError(
                "RealQXDMController requires a Windows host with pywinauto. "
                "On Linux use the remote agent or mock controller."
            )
        try:
            import pywinauto  # noqa: F401  type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "pywinauto is required for RealQXDMController; install it "
                "with 'pip install pywinauto' on the Windows Device Agent."
            ) from exc
        self.settings = settings
        self.job_id = job_id or new_job_id()
        self.raw_dir = Path(raw_dir) if raw_dir else (
            self.settings.jobs_root / self.job_id / "raw"
        )
        self.app = None
        self.window = None
        self._logging_active = False

    # ------------------------------------------------------------------
    def _ensure_windows(self) -> None:
        # Already validated in __init__, kept for clarity.
        if not is_windows_host():
            raise RuntimeError("RealQXDMController only runs on Windows.")

    def start_session(
        self,
        dmc_file: str,
        duration_sec: int,
        prefix: str,
        scenario_name: str,
        job_id: Optional[str] = None,
    ) -> SessionArtifacts:
        self._ensure_windows()
        # Lazy imports -- never imported on Linux.
        from pywinauto import Application  # type: ignore
        from pywinauto.keyboard import send_keys  # type: ignore

        if job_id:
            self.job_id = job_id
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {str(p.resolve()) for p in _list_binary_files(self.raw_dir)}

        artifacts = SessionArtifacts(
            job_id=self.job_id,
            scenario_name=scenario_name,
            prefix=prefix,
            raw_dir=self.raw_dir,
            duration_sec=duration_sec,
        )
        artifacts.notes.append("UNVERIFIED_QXDM_BUILD_SPECIFIC menu accelerators")
        t_start = time.monotonic()
        try:
            log.info(
                "[REAL] Launching QXDM at %s (UNVERIFIED accelerators)",
                self.settings.qxdm_exe,
            )
            self.app = Application(backend="win32").start(self.settings.qxdm_exe)
            time.sleep(10)
            self.window = self.app.window(title_re=".*QXDM.*")
            self.window.wait("visible", timeout=30)
            self.window.set_focus()

            # UNVERIFIED_QXDM_BUILD_SPECIFIC menu sequence.  Replace with
            # print_control_identifiers()-derived control IDs after the
            # TMO QXDM build is available.
            send_keys("%f"); time.sleep(1); send_keys("l"); time.sleep(2)
            send_keys(dmc_file); send_keys("{ENTER}"); time.sleep(5)

            send_keys("%c"); time.sleep(1); send_keys("p"); time.sleep(2)
            send_keys(self.settings.com_port or ""); send_keys("{ENTER}"); time.sleep(5)

            send_keys("%f"); time.sleep(1); send_keys("g"); time.sleep(2)
            send_keys(str(self.raw_dir)); send_keys("{TAB}")
            send_keys(prefix); send_keys("{TAB}")
            send_keys(str(self.settings.max_log_size_mb)); send_keys("{TAB}{ENTER}")
            time.sleep(3)

            self._logging_active = True
            send_keys("%f"); time.sleep(1); send_keys("s")
            new_files = wait_for_new_log(
                self.raw_dir,
                snapshot,
                timeout_sec=self.settings.max_wait_for_log_sec,
            )
            time.sleep(max(0, duration_sec - 2))
            send_keys("%f"); time.sleep(1); send_keys("t")
            self._logging_active = False
            wait_for_file_stability(
                new_files,
                window_sec=self.settings.stability_window_sec,
                timeout_sec=self.settings.max_wait_for_stability_sec,
            )
        finally:
            try:
                self.ensure_logging_stopped()
            except Exception as exc:  # noqa: BLE001
                log.error("[REAL] Emergency stop failed: %s", exc)
            try:
                self.ensure_qxdm_closed()
            except Exception as exc:  # noqa: BLE001
                log.error("[REAL] Emergency close failed: %s", exc)

        artifacts.binary_files = _list_binary_files(self.raw_dir)
        artifacts.duration_actual_sec = time.monotonic() - t_start
        artifacts.ended_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        artifacts.log_count = len(artifacts.binary_files)
        return artifacts

    def ensure_logging_stopped(self) -> None:
        if not self._logging_active:
            return
        from pywinauto.keyboard import send_keys  # type: ignore
        log.warning("[REAL] Emergency stop logging...")
        send_keys("%f"); time.sleep(1); send_keys("t")
        self._logging_active = False

    def ensure_qxdm_closed(self) -> None:
        from pywinauto.keyboard import send_keys  # type: ignore
        log.warning("[REAL] Closing QXDM application...")
        send_keys("%{F4}"); time.sleep(2)
        if self.window is not None:
            try:
                self.window.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Mock controller
# ---------------------------------------------------------------------------
class MockQXDMController(QXDMController):
    """Synthetic QXDM driver for offline Linux tests."""

    def __init__(
        self,
        settings: Settings,
        job_id: Optional[str] = None,
        raw_dir: Optional[Path] = None,
    ):
        self.settings = settings
        self.job_id = job_id or new_job_id()
        self.raw_dir = Path(raw_dir) if raw_dir else (
            self.settings.jobs_root / self.job_id / "raw"
        )
        self.fail_mode = self.settings.mock_fail_mode
        self._stop_event = threading.Event()
        self._writer_thread: Optional[threading.Thread] = None

    def start_session(
        self,
        dmc_file: str,
        duration_sec: int,
        prefix: str,
        scenario_name: str,
        job_id: Optional[str] = None,
    ) -> SessionArtifacts:
        if job_id:
            self.job_id = job_id
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        for old in _list_binary_files(self.raw_dir):
            try:
                old.unlink()
            except OSError:
                pass

        artifacts = SessionArtifacts(
            job_id=self.job_id,
            scenario_name=scenario_name,
            prefix=prefix,
            raw_dir=self.raw_dir,
            duration_sec=duration_sec,
        )

        if self.fail_mode == "launch":
            time.sleep(0.1)
            raise RuntimeError("[MOCK] Simulated QXDM launch failure.")

        log.info("[MOCK] Loaded DMC config: %s", dmc_file)
        log.info(
            "[MOCK] Connected to DIAG port (mock); output=%s prefix=%s",
            self.raw_dir, prefix,
        )

        snapshot = {str(p.resolve()) for p in _list_binary_files(self.raw_dir)}

        prefix_path = _safe_join(self.raw_dir, prefix)
        rollover_bytes = max(
            64 * 1024,
            int(self.settings.mock_rollover_mb) * 1024 * 1024,
        )
        writer_ctx = _WriterContext(
            raw_dir=self.raw_dir,
            prefix=str(prefix_path.name),
            prefix_full=prefix_path,
            rollover_bytes=rollover_bytes,
            chunk_bytes=int(self.settings.mock_chunk_size_bytes),
            chunk_interval_ms=int(self.settings.mock_chunk_interval_ms),
            fail_mode=self.fail_mode,
        )
        self._stop_event.clear()
        self._writer_thread = threading.Thread(
            target=_writer_main,
            args=(writer_ctx, self._stop_event),
            name=f"qxdm-mock-writer-{self.job_id}",
            daemon=True,
        )
        self._writer_thread.start()

        try:
            new_files = wait_for_new_log(
                self.raw_dir,
                snapshot,
                timeout_sec=self.settings.max_wait_for_log_sec,
                poll_interval_sec=0.1,
            )
        except TimeoutError:
            if self.fail_mode == "no_log":
                raise RuntimeError(
                    "[MOCK] Simulated no-log timeout: no .qmdl produced."
                )
            raise

        log.info("[MOCK] Logging started; %d file(s) growing.", len(new_files))

        run_seconds = max(0.2, duration_sec)
        time.sleep(run_seconds)

        self._stop_event.set()
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=30)

        if writer_ctx.crashed:
            raise RuntimeError("[MOCK] Writer thread crashed mid-session.")

        flush_delay = float(self.settings.mock_simulate_flush_delay_sec)
        log.info("[MOCK] Waiting %.2fs for final flush...", flush_delay)
        time.sleep(flush_delay)

        try:
            wait_for_file_stability(
                writer_ctx.files_written,
                window_sec=self.settings.stability_window_sec,
                timeout_sec=self.settings.max_wait_for_stability_sec,
                poll_interval_sec=0.2,
            )
        except TimeoutError as exc:
            log.warning("[MOCK] Stability wait failed: %s", exc)

        artifacts.binary_files = writer_ctx.files_written
        artifacts.duration_actual_sec = float(duration_sec)
        artifacts.ended_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        artifacts.log_count = len(artifacts.binary_files)
        artifacts.notes.extend(writer_ctx.notes)
        log.info(
            "[MOCK] Session complete: %d binary file(s) in %s",
            artifacts.log_count,
            self.raw_dir,
        )
        return artifacts


# ---------------------------------------------------------------------------
# Mock writer internals
# ---------------------------------------------------------------------------
class _WriterContext:
    def __init__(
        self,
        raw_dir: Path,
        prefix: str,
        prefix_full: Path,
        rollover_bytes: int,
        chunk_bytes: int,
        chunk_interval_ms: int,
        fail_mode: str,
    ):
        self.raw_dir = raw_dir
        self.prefix = prefix
        self.prefix_full = prefix_full
        self.rollover_bytes = rollover_bytes
        self.chunk_bytes = chunk_bytes
        self.chunk_interval_ms = chunk_interval_ms
        self.fail_mode = fail_mode
        self.files_written: List[Path] = []
        self.notes: List[str] = []
        self.crashed: bool = False
        self._lock = threading.Lock()

    def add_file(self, p: Path) -> None:
        with self._lock:
            if p not in self.files_written:
                self.files_written.append(p)


def _writer_main(ctx: _WriterContext, stop_event: threading.Event) -> None:
    """Long-running writer thread that the controller starts."""
    try:
        seq = 0
        session_idx = 1
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        current = ctx.prefix_full.with_name(
            f"{ctx.prefix}_{timestamp}_session_{session_idx:03d}.qmdl"
        )
        f = open(current, "wb", buffering=0)
        ctx.add_file(current)
        log.info("[MOCK-WRITER] created %s", current.name)

        while not stop_event.is_set():
            if ctx.fail_mode == "crash" and seq >= 4 and seq < 6:
                f.close()
                log.warning("[MOCK-WRITER] simulated crash, aborting file.")
                return

            chunk = _synthetic_chunk(ctx.chunk_bytes, seq)
            f.write(chunk)
            f.flush()
            seq += 1

            if current.stat().st_size >= ctx.rollover_bytes:
                f.close()
                session_idx += 1
                current = ctx.prefix_full.with_name(
                    f"{ctx.prefix}_{timestamp}_session_{session_idx:03d}.qmdl"
                )
                f = open(current, "wb", buffering=0)
                ctx.add_file(current)
                log.info(
                    "[MOCK-WRITER] rollover -> %s (size threshold reached)",
                    current.name,
                )

            time.sleep(max(0.0, ctx.chunk_interval_ms / 1000.0))

        try:
            f.flush()
            f.close()
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        ctx.crashed = True
        ctx.notes.append(f"writer_exception: {exc}")
        log.error("[MOCK-WRITER] crashed: %s", exc)


def _synthetic_chunk(size: int, seq: int) -> bytes:
    header = b"\x7E\x00" + seq.to_bytes(4, "big") + os.urandom(2) + b"\x10"
    body = os.urandom(max(1, size - len(header) - 1))
    trailer = b"\x7E"
    return header + body + trailer


# ---------------------------------------------------------------------------
# Backwards-compatible shim for the old global-config entry point
# ---------------------------------------------------------------------------
def make_controller_legacy(*, job_id=None, raw_dir=None, use_remote_agent=False, device_agent_url=None):
    """Legacy entry point.  Builds a controller using the default Settings.

    Tests and CLI tools that haven't been migrated yet can still use this.
    """
    try:
        from .settings import from_env  # type: ignore
        from .device_agent.client import RemoteAgentClient  # type: ignore
    except ImportError:
        from settings import from_env  # type: ignore
        from device_agent.client import RemoteAgentClient  # type: ignore

    settings = from_env()
    client = None
    if device_agent_url:
        client = RemoteAgentClient(
            url=device_agent_url,
            token=settings.device_agent_token,
            request_timeout_sec=settings.device_agent_timeout_sec,
            poll_initial_sec=settings.device_agent_poll_initial_sec,
            poll_max_sec=settings.device_agent_poll_max_sec,
            poll_deadline_sec=settings.device_agent_poll_deadline_sec,
        )
    return make_controller(
        settings=settings,
        job_id=job_id,
        raw_dir=raw_dir,
        use_remote_agent=use_remote_agent,
        client=client,
    )
