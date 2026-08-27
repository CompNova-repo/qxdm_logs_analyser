"""
Log Retention Engine.

Two entry points:

* :func:`run_log_rotation` -- run a single rotation cycle (also exposed
  via the ``/api/v1/rotate-logs`` endpoint and the ``LogRotator`` class
  for in-process use).
* :func:`serve` -- long-lived daemon that re-runs the cycle every
  ``Settings.rotation_interval_sec``.  Callable as ``python -m log_rotator``.

Safety rules
------------
* Only considers ``*.zip`` entries that are regular files (no symlinks,
  no in-progress ``.*.part`` archives).
* Iterates a stable sorted list -- oldest first.
* Quota deletion keeps deleting until size is below the cap.
* Per-file ``OSError`` is captured, never propagated.
* Structured :class:`RotationResult` for every run.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

try:
    from .settings import Settings, from_env
except ImportError:
    from settings import Settings, from_env  # type: ignore

log = logging.getLogger("Log_Rotator")

# When this module is the entry point (python -m log_rotator), force
# logging to stdout so subprocess tests can capture it.  We still allow
# other callers (pytest) to configure logging normally.
if os.getenv("QXDM_ROTATOR_FORCE_LOG") == "1":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------
@dataclass
class FileEvent:
    path: str
    reason: str  # "age" | "quota" | "skipped_active" | "skipped_symlink" | "delete_failed"

    def to_dict(self) -> Dict:
        return {"path": self.path, "reason": self.reason}


@dataclass
class RotationResult:
    target_directory: str
    pruned_by_age: int = 0
    pruned_by_quota: int = 0
    skipped_active: int = 0
    skipped_symlink: int = 0
    delete_failures: int = 0
    initial_size_mb: float = 0.0
    final_size_mb: float = 0.0
    duration_sec: float = 0.0
    events: List[FileEvent] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = {
            "target_directory": self.target_directory,
            "pruned_by_age": self.pruned_by_age,
            "pruned_by_quota": self.pruned_by_quota,
            "skipped_active": self.skipped_active,
            "skipped_symlink": self.skipped_symlink,
            "delete_failures": self.delete_failures,
            "initial_size_mb": round(self.initial_size_mb, 3),
            "final_size_mb": round(self.final_size_mb, 3),
            "duration_sec": round(self.duration_sec, 3),
        }
        d["events"] = [e.to_dict() for e in self.events]
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_directory_size_mb(directory: Path) -> float:
    if not directory.exists():
        return 0.0
    total = 0
    for f in directory.glob("*"):
        try:
            st = f.stat()
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total / (1024 * 1024)


def _iter_archive_candidates(directory: Path) -> Iterable[Path]:
    """Yield ``*.zip`` files, oldest first.  Skips symlinks + active temp files."""
    if not directory.exists():
        return []
    items = []
    symlinks: List[Path] = []
    actives: List[Path] = []
    for entry in directory.iterdir():
        try:
            st = entry.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            symlinks.append(entry)
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if not entry.name.endswith(".zip"):
            continue
        if entry.name.startswith(".") or entry.name.endswith(".part"):
            actives.append(entry)
            continue
        items.append((entry, st.st_mtime))
    items.sort(key=lambda x: x[1])
    # Bundle skipped entries so callers can record them as events.
    return [p for p, _ in items]  # type: ignore[return-value]


def _iter_skipped(directory: Path) -> tuple[List[Path], List[Path]]:
    """Return (symlinks, active_temp) entries separately so the caller can count them."""
    if not directory.exists():
        return [], []
    symlinks: List[Path] = []
    actives: List[Path] = []
    for entry in directory.iterdir():
        try:
            st = entry.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            symlinks.append(entry)
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        # Any file ending in .part is an in-progress write -- never touch it.
        if entry.name.endswith(".part"):
            actives.append(entry)
            continue
        # Hidden file naming convention for temp archives.
        if entry.name.startswith("."):
            actives.append(entry)
            continue
    return symlinks, actives


# ---------------------------------------------------------------------------
# Rotation logic
# ---------------------------------------------------------------------------
class LogRotator:
    def __init__(
        self,
        settings: Settings,
        directory: Optional[Path] = None,
        max_retention_days: Optional[int] = None,
        max_backup_dir_mb: Optional[int] = None,
    ):
        self.settings = settings
        self.directory = Path(directory or settings.backup_directory)
        # Check the *original* path (before resolution) -- we never want
        # to operate on a symlinked backup dir because the target could
        # point anywhere.
        if self.directory.is_symlink():
            raise RuntimeError(
                f"backup directory {self.directory} is a symlink; refusing "
                "to operate."
            )
        self.directory = self.directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_retention_days = (
            int(max_retention_days)
            if max_retention_days is not None
            else int(settings.retention_days)
        )
        self.max_backup_dir_mb = (
            int(max_backup_dir_mb)
            if max_backup_dir_mb is not None
            else int(settings.backup_quota_mb)
        )

    # ------------------------------------------------------------------
    def run_once(self) -> RotationResult:
        started = time.monotonic()
        initial_size = get_directory_size_mb(self.directory)
        candidates = list(_iter_archive_candidates(self.directory))
        skipped_syms, skipped_acts = _iter_skipped(self.directory)
        cutoff = datetime.now() - timedelta(days=self.max_retention_days)

        result = RotationResult(target_directory=str(self.directory))
        result.initial_size_mb = round(initial_size, 3)
        result.skipped_symlink = len(skipped_syms)
        result.skipped_active = len(skipped_acts)
        for s in skipped_syms:
            result.events.append(
                FileEvent(path=str(s), reason="skipped_symlink")
            )
        for a in skipped_acts:
            result.events.append(
                FileEvent(path=str(a), reason="skipped_active")
            )

        surviving: List[Path] = []
        for archive in candidates:
            # Defensive: re-check inside the loop in case mtime changed.
            try:
                st = archive.stat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                result.skipped_symlink += 1
                result.events.append(
                    FileEvent(path=str(archive), reason="skipped_symlink")
                )
                continue
            mtime = datetime.fromtimestamp(st.st_mtime)
            if mtime < cutoff:
                log.info(
                    "Deleting expired archive (mtime=%s, %d-day cutoff): %s",
                    mtime.isoformat(),
                    self.max_retention_days,
                    archive.name,
                )
                if self._safe_unlink(archive):
                    result.pruned_by_age += 1
                    result.events.append(
                        FileEvent(path=str(archive), reason="age")
                    )
                else:
                    result.delete_failures += 1
                    result.events.append(
                        FileEvent(path=str(archive), reason="delete_failed")
                    )
            else:
                surviving.append(archive)

        current_size = get_directory_size_mb(self.directory)
        while current_size > self.max_backup_dir_mb and surviving:
            oldest = surviving.pop(0)
            log.warning(
                "Backup quota exceeded (%.2f MB > %d MB). Deleting oldest: %s",
                current_size,
                self.max_backup_dir_mb,
                oldest.name,
            )
            if self._safe_unlink(oldest):
                result.pruned_by_quota += 1
                result.events.append(
                    FileEvent(path=str(oldest), reason="quota")
                )
            else:
                result.delete_failures += 1
                result.events.append(
                    FileEvent(path=str(oldest), reason="delete_failed")
                )
            current_size = get_directory_size_mb(self.directory)

        result.final_size_mb = round(current_size, 3)
        result.duration_sec = round(time.monotonic() - started, 3)
        log.info("Log rotation finished: %s", result.to_dict())
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _safe_unlink(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True  # already gone
        except OSError as exc:
            log.warning("Failed to delete %s: %s", path, exc)
            return False


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------
def run_log_rotation(
    directory: Optional[Path] = None,
    max_retention_days: Optional[int] = None,
    max_backup_dir_mb: Optional[int] = None,
    settings: Optional[Settings] = None,
) -> RotationResult:
    settings = settings or from_env()
    rotator = LogRotator(
        settings=settings,
        directory=directory,
        max_retention_days=max_retention_days,
        max_backup_dir_mb=max_backup_dir_mb,
    )
    return rotator.run_once()


# ---------------------------------------------------------------------------
# Long-lived daemon
# ---------------------------------------------------------------------------
_run = True


def _handle_signal(signum, frame):  # noqa: ARG001
    global _run
    log.info("Received signal %d; shutting down rotator...", signum)
    _run = False


def serve(interval: Optional[int] = None) -> int:
    settings = from_env()
    interval = int(interval if interval is not None else settings.rotation_interval_sec)
    log.info(
        "Starting log rotator daemon (interval=%ds) against %s",
        interval,
        settings.backup_directory,
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    rotator = LogRotator(settings=settings)
    while _run:
        try:
            rotator.run_once()
        except Exception as exc:  # noqa: BLE001
            log.exception("Rotation cycle failed: %s", exc)
        slept = 0.0
        while _run and slept < interval:
            time.sleep(min(5.0, interval - slept))
            slept += 5.0
    log.info("Rotator daemon exiting cleanly.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the QXDM log retention engine.  Use --once to run "
        "a single cycle or no flag to daemonise."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single rotation cycle and exit.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override settings rotation_interval_sec.",
    )
    args = parser.parse_args()

    if args.once:
        run_log_rotation()
        return 0
    return serve(interval=args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
