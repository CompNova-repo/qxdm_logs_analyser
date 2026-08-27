"""
QXDM Controller Layer.

Architecture
------------
``QXDMController`` is an abstract driver; the framework ships two
implementations:

* ``RealQXDMController``  - delegates to a remote Windows Device Agent
  (HTTP/JSON-RPC).  The Linux orchestrator never imports ``pywinauto``;
  the actual GUI automation lives on the QXDM/QCAT host.
* ``MockQXDMController`` - reproduces the full logging lifecycle
  (file creation, incremental growth, rollover, flush delay) for offline
  Linux testing.
"""

from __future__ import annotations

import abc
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

try:
    import config  # type: ignore
except (ImportError, ModuleNotFoundError):
    from . import config  # type: ignore


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("QXDM_Service")


# ---------------------------------------------------------------------------
# Shared data structures
# ---------------------------------------------------------------------------
@dataclass
class SessionArtifacts:
    """Files and metadata produced by a single QXDM logging session."""

    job_id: str
    scenario_name: str
    prefix: str
    raw_dir: Path
    binary_files: List[Path] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    ended_at: Optional[str] = None
    duration_sec: int = 0
    duration_actual_sec: float = 0.0
    log_count: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "scenario_name": self.scenario_name,
            "prefix": self.prefix,
            "raw_dir": str(self.raw_dir),
            "binary_files": [str(p) for p in self.binary_files],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_sec": self.duration_sec,
            "duration_actual_sec": round(self.duration_actual_sec, 3),
            "log_count": self.log_count,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Helpers
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
    # de-dupe (in case of unusual extension overlaps) but keep sort order
    seen: set = set()
    unique: List[Path] = []
    for f in found:
        if f not in seen:
            unique.append(f)
            seen.add(f)
    return unique


def wait_for_new_log(
    watch_dir: Path,
    snapshot: set,
    timeout_sec: float,
    poll_interval_sec: float = 0.5,
) -> List[Path]:
    """Block until at least one new binary file appears in ``watch_dir``."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        current = _list_binary_files(watch_dir)
        new = [p for p in current if p not in snapshot and p not in snapshot]
        # match by absolute path
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
    """Block until size of each ``path`` is unchanged for ``window_sec`` seconds."""
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


def discover_com_ports() -> List[str]:
    """Discover Qualcomm DIAG serial ports (portable, Linux-first)."""
    if config.MOCK_MODE:
        return ["COM99"]  # simulated only

    ports: List[str] = []
    # Linux: enumerate /sys/class/tty/* for Qualcomm-style interfaces
    tty_dir = Path("/sys/class/tty")
    if tty_dir.exists():
        for entry in tty_dir.iterdir():
            driver_path = entry / "device" / "driver"
            try:
                driver_link = (
                    driver_path.resolve().name if driver_path.exists() else ""
                )
            except OSError:
                driver_link = ""
            if any(
                token in driver_link.lower()
                for token in ("qcserial", "qcdm", "diag", "qdss")
            ):
                ports.append(f"/dev/{entry.name}")
    if ports:
        log.info("Detected Qualcomm ports (Linux): %s", ports)
    if not ports:
        log.warning(
            "No Qualcomm DIAG ports auto-detected. Set QXDM_COM_PORT explicitly."
        )
    return ports


def get_com_port() -> str:
    if config.COM_PORT:
        return config.COM_PORT
    ports = discover_com_ports()
    if not ports:
        raise RuntimeError(
            "No Qualcomm DIAG port detected. Set QXDM_COM_PORT=COMx "
            "or /dev/ttyUSB0 to override."
        )
    return ports[0]


# ---------------------------------------------------------------------------
# Controller interface
# ---------------------------------------------------------------------------
class QXDMController(abc.ABC):
    """Abstract logging driver."""

    @abc.abstractmethod
    def start_session(
        self,
        dmc_file: str,
        duration_sec: int,
        prefix: str,
        scenario_name: str,
        job_id: Optional[str] = None,
    ) -> SessionArtifacts:
        """Run a complete logging session.

        Implementations MUST return a SessionArtifacts object describing
        exactly which binary files belong to this session (for downstream
        processing), even on the failure path.
        """


def make_controller(
    job_id: Optional[str] = None,
    raw_dir: Optional[Path] = None,
    use_remote_agent: bool = False,
    device_agent_url: Optional[str] = None,
) -> QXDMController:
    """Factory selecting the right driver based on config & env."""
    if config.MOCK_MODE:
        return MockQXDMController(
            job_id=job_id,
            raw_dir=raw_dir,
        )
    if use_remote_agent:
        return RemoteAgentController(
            url=device_agent_url or config.DEFAULT_DEVICE_AGENT_URL,
            raw_dir=raw_dir,
        )
    return RealQXDMController(
        job_id=job_id,
        raw_dir=raw_dir,
    )


# ---------------------------------------------------------------------------
# Real QXDM controller (remote Windows Device Agent)
# ---------------------------------------------------------------------------
class RemoteAgentController(QXDMController):
    """Talks to a separate Windows QXDM Device Agent over HTTP/JSON-RPC.

    The Device Agent owns the pywinauto/QCAT integration on the Windows
    host; the Linux orchestrator only sends commands and receives file
    paths.  This keeps the orchestrator free of Windows-only imports.
    """

    def __init__(self, url: str, raw_dir: Optional[Path] = None):
        self.url = url.rstrip("/")
        self.raw_dir_override = Path(raw_dir) if raw_dir else None

    def start_session(
        self,
        dmc_file: str,
        duration_sec: int,
        prefix: str,
        scenario_name: str,
        job_id: Optional[str] = None,
    ) -> SessionArtifacts:
        job_id = job_id or uuid.uuid4().hex[:12]
        raw_dir = self.raw_dir_override or (config.JOBS_ROOT / job_id / "raw")
        raw_dir.mkdir(parents=True, exist_ok=True)

        artifacts = SessionArtifacts(
            job_id=job_id,
            scenario_name=scenario_name,
            prefix=prefix,
            raw_dir=raw_dir,
            duration_sec=duration_sec,
        )
        artifacts.notes.append(f"remote_agent_url={self.url}")

        payload = {
            "dmc_file": dmc_file,
            "duration_sec": duration_sec,
            "prefix": prefix,
            "scenario_name": scenario_name,
            "job_id": job_id,
            "raw_dir": str(raw_dir),
            "max_log_size_mb": config.MAX_LOG_SIZE_MB,
        }
        log.info("Dispatching logging job %s to Device Agent %s", job_id, self.url)
        try:
            response = self._post_json("/api/v1/start_session", payload)
        except Exception as exc:  # noqa: BLE001
            artifacts.notes.append(f"agent_error: {exc}")
            raise RuntimeError(f"Device Agent call failed: {exc}") from exc

        artifacts.binary_files = [Path(p) for p in response.get("binary_files", [])]
        artifacts.duration_actual_sec = float(response.get("duration_actual_sec", 0.0))
        artifacts.ended_at = response.get(
            "ended_at", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        artifacts.log_count = len(artifacts.binary_files)
        return artifacts

    # -- helpers ---------------------------------------------------------
    def _post_json(self, path: str, payload: dict) -> dict:
        # Use stdlib only so the orchestrator never breaks offline.
        import urllib.request

        req = urllib.request.Request(
            f"{self.url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Real QXDM controller (legacy: same-host pywinauto / Windows path)
# ---------------------------------------------------------------------------
class RealQXDMController(QXDMController):
    """Legacy same-host controller (kept for Windows test-benches).

    Combines: launch QXDM, load DMC, connect DIAG, configure, start,
    monitor growth, stop, flush, emergency cleanup.  This is the path
    that runs *only* on a Windows machine; on Linux you should use
    ``RemoteAgentController`` instead.
    """

    def __init__(
        self,
        job_id: Optional[str] = None,
        raw_dir: Optional[Path] = None,
    ):
        self.job_id = job_id or uuid.uuid4().hex[:12]
        self.raw_dir = Path(raw_dir) if raw_dir else (
            config.JOBS_ROOT / self.job_id / "raw"
        )
        self.app = None
        self.window = None
        self._logging_active = False

    def start_session(
        self,
        dmc_file: str,
        duration_sec: int,
        prefix: str,
        scenario_name: str,
        job_id: Optional[str] = None,
    ) -> SessionArtifacts:
        if not shutil.which("pywinauto"):
            raise RuntimeError(
                "RealQXDMController requires pywinauto / Windows host. "
                "On Linux use RemoteAgentController or set QXDM_MOCK_MODE=True."
            )
        from pywinauto import Application  # type: ignore
        from pywinauto.keyboard import send_keys  # type: ignore

        artifacts = SessionArtifacts(
            job_id=self.job_id,
            scenario_name=scenario_name,
            prefix=prefix,
            raw_dir=self.raw_dir,
            duration_sec=duration_sec,
        )
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {str(p.resolve()) for p in _list_binary_files(self.raw_dir)}

        t_start = time.monotonic()
        try:
            port = get_com_port()
            log.info("[REAL] Launching QXDM at %s", config.QXDM_EXE)
            self.app = Application(backend="win32").start(config.QXDM_EXE)
            time.sleep(10)
            self.window = self.app.window(title_re=".*QXDM.*")
            self.window.wait("visible", timeout=30)

            self.window.set_focus()

            # Load DMC
            send_keys("%f"); time.sleep(1); send_keys("l"); time.sleep(2)
            send_keys(dmc_file); send_keys("{ENTER}"); time.sleep(5)

            # Connect COM port
            send_keys("%c"); time.sleep(1); send_keys("p"); time.sleep(2)
            send_keys(port); send_keys("{ENTER}"); time.sleep(5)

            # Configure output dir + max size + prefix
            send_keys("%f"); time.sleep(1); send_keys("g"); time.sleep(2)
            send_keys(str(self.raw_dir)); send_keys("{TAB}")
            send_keys(prefix); send_keys("{TAB}")
            send_keys(str(config.MAX_LOG_SIZE_MB)); send_keys("{TAB}{ENTER}")
            time.sleep(3)

            # Start logging
            self._logging_active = True
            send_keys("%f"); time.sleep(1); send_keys("s")
            log.info("[REAL] Logging triggered; waiting up to %ds for new file...",
                     config.ROTATION_CONFIG["max_wait_for_log_sec"])
            new_files = wait_for_new_log(
                self.raw_dir,
                snapshot,
                timeout_sec=float(config.ROTATION_CONFIG["max_wait_for_log_sec"]),
            )
            log.info("[REAL] Logging confirmed; %d file(s) growing.", len(new_files))

            # Let it run
            time.sleep(max(0, duration_sec - 2))

            # Stop logging
            send_keys("%f"); time.sleep(1); send_keys("t")
            self._logging_active = False
            wait_for_file_stability(
                new_files,
                window_sec=float(config.ROTATION_CONFIG["stability_window_sec"]),
                timeout_sec=120.0,
            )
            log.info("[REAL] Logs flushed and stable.")

        finally:
            # Emergency cleanup
            try:
                self.ensure_logging_stopped(send_keys)
            except Exception as exc:  # noqa: BLE001
                log.error("[REAL] Emergency stop failed: %s", exc)
            try:
                self.ensure_qxdm_closed(send_keys)
            except Exception as exc:  # noqa: BLE001
                log.error("[REAL] Emergency close failed: %s", exc)

        artifacts.binary_files = _list_binary_files(self.raw_dir)
        artifacts.duration_actual_sec = time.monotonic() - t_start
        artifacts.ended_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        artifacts.log_count = len(artifacts.binary_files)
        return artifacts

    # ------------------------------------------------------------------
    def ensure_logging_stopped(self, send_keys) -> None:
        if not self._logging_active:
            return
        log.warning("[REAL] Emergency stop logging...")
        send_keys("%f"); time.sleep(1); send_keys("t")
        self._logging_active = False

    def ensure_qxdm_closed(self, send_keys) -> None:
        log.warning("[REAL] Closing QXDM application...")
        send_keys("%{F4}"); time.sleep(2)
        if self.window is not None:
            try:
                self.window.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Mock controller (Linux-friendly, realistic lifecycle)
# ---------------------------------------------------------------------------
class MockQXDMController(QXDMController):
    """Synthetic QXDM driver for offline Linux tests.

    Lifecycle imitates the real flow:

      start -> create session file -> append chunks -> rollover
              -> stop -> delayed flush -> files stabilize
    """

    def __init__(
        self,
        job_id: Optional[str] = None,
        raw_dir: Optional[Path] = None,
    ):
        self.job_id = job_id or uuid.uuid4().hex[:12]
        self.raw_dir = Path(raw_dir) if raw_dir else (
            config.JOBS_ROOT / self.job_id / "raw"
        )
        self.fail_mode = config.MOCK_CONFIG.get("fail_mode", "none")
        self._stop_event = threading.Event()
        self._writer_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
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
        self.raw_dir = self.raw_dir  # keep
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        # purge any leftover binaries in this job's dir
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

        # --- failure injection --------------------------------------------
        if self.fail_mode == "launch":
            time.sleep(0.1)
            raise RuntimeError("[MOCK] Simulated QXDM launch failure.")

        port = get_com_port()
        log.info("[MOCK] Attached to simulated QXDM window.")
        log.info("[MOCK] Loaded DMC config: %s", dmc_file)
        log.info("[MOCK] Connected to DIAG port %s", port)
        log.info("[MOCK] Output path=%s prefix=%s", self.raw_dir, prefix)
        log.info("[MOCK] Triggering logging for %d seconds...", duration_sec)

        # --- verify a new file appeared ----------------------------------
        # Snapshot BEFORE starting the writer so we can detect the new file
        # the writer will create.
        snapshot = {str(p.resolve()) for p in _list_binary_files(self.raw_dir)}

        # --- start writer thread -----------------------------------------
        prefix_path = _safe_join(self.raw_dir, prefix)
        rollover_bytes = max(
            64 * 1024,
            int(config.MOCK_CONFIG.get("rollover_mb", config.MAX_LOG_SIZE_MB))
            * 1024
            * 1024,
        )
        writer_ctx = _WriterContext(
            raw_dir=self.raw_dir,
            prefix=str(prefix_path.name),
            prefix_full=prefix_path,
            rollover_bytes=rollover_bytes,
            chunk_bytes=int(config.MOCK_CONFIG.get("chunk_size_bytes", 4096)),
            chunk_interval_ms=int(
                config.MOCK_CONFIG.get("chunk_interval_ms", 200)
            ),
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
                timeout_sec=float(
                    config.ROTATION_CONFIG["max_wait_for_log_sec"]
                ),
                poll_interval_sec=0.1,
            )
        except TimeoutError:
            if self.fail_mode == "no_log":
                raise RuntimeError(
                    "[MOCK] Simulated no-log timeout: no .qmdl produced."
                )
            raise

        log.info("[MOCK] Logging started; %d file(s) growing.", len(new_files))

        # --- let it run for the requested duration (compressed for tests)-
        run_seconds = max(0.2, duration_sec)
        time.sleep(run_seconds)

        # --- request stop -------------------------------------------------
        log.info("[MOCK] Stop requested; signalling writer thread.")
        self._stop_event.set()
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=30)

        # --- check for simulated writer crash ----------------------------
        if writer_ctx.crashed:
            raise RuntimeError("[MOCK] Writer thread crashed mid-session.")

        # --- simulate delayed OS flush -----------------------------------
        flush_delay = float(config.MOCK_CONFIG.get("simulate_flush_delay_sec", 1.5))
        log.info("[MOCK] Waiting %.2fs for final flush...", flush_delay)
        time.sleep(flush_delay)

        # --- wait for stability ------------------------------------------
        try:
            wait_for_file_stability(
                writer_ctx.files_written,
                window_sec=float(
                    config.ROTATION_CONFIG["stability_window_sec"]
                ),
                timeout_sec=120.0,
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
        # QMDL-style container: a fixed 16-byte header followed by chunks of
        # synthetic 0x7E-framed diagnostic payloads.
        seq = 0
        session_idx = 1
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        current = ctx.prefix_full.with_name(
            f"{ctx.prefix}_{timestamp}_session_{session_idx:03d}.qmdl"
        )
        f = open(current, "wb", buffering=0)
        ctx.add_file(current)
        log.info("[MOCK-WRITER] created %s", current.name)
        consecutive_failures = 0

        while not stop_event.is_set():
            if ctx.fail_mode == "crash" and seq >= 4 and seq < 6:
                # simulate intermittent I/O failure mid-session
                f.close()
                log.warning("[MOCK-WRITER] simulated crash, aborting file.")
                return

            chunk = _synthetic_chunk(ctx.chunk_bytes, seq)
            f.write(chunk)
            f.flush()
            seq += 1

            # Rollover
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

        # graceful shutdown
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
    """Build a QMDL-like packet: header + random bytes + trailer."""
    header = b"\x7E\x00" + seq.to_bytes(4, "big") + os.urandom(2) + b"\x10"
    body = os.urandom(max(1, size - len(header) - 1))
    trailer = b"\x7E"
    return header + body + trailer
