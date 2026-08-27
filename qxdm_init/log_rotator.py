"""
Log Retention Engine
====================

Two entry points:

* :func:`run_log_rotation` - run a single rotation cycle (also exposed via
  the ``/api/v1/rotate-logs`` endpoint).
* :func:`main` (and ``python -m log_rotator``) - long-lived daemon that
  re-runs the cycle every ``config.ROTATION_CONFIG["check_interval_sec"]``
  seconds.  Suitable to wire into ``systemd`` or ``cron``.
"""

from __future__ import annotations

import logging
import shutil
import signal
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List

try:
    import config  # type: ignore
except (ImportError, ModuleNotFoundError):
    from . import config  # type: ignore


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("Log_Rotator")


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------
@dataclass
class RotationResult:
    target_directory: str
    pruned_by_age: int
    pruned_by_quota: int
    initial_size_mb: float
    final_size_mb: float
    duration_sec: float

    def to_dict(self) -> Dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_directory_size_mb(directory: Path) -> float:
    if not directory.exists():
        return 0.0
    total = 0
    for f in directory.glob("*"):
        if f.is_file() and not f.is_symlink():
            try:
                total += f.stat().st_size
            except OSError:
                continue
    return total / (1024 * 1024)


def _iter_archives(directory: Path) -> Iterable[Path]:
    yield from sorted(
        [p for p in directory.glob("*.zip") if p.is_file()],
        key=lambda f: f.stat().st_mtime,
    )


# ---------------------------------------------------------------------------
# Retention logic
# ---------------------------------------------------------------------------
def run_log_rotation(
    directory: Path | None = None,
    max_retention_days: int | None = None,
    max_backup_dir_mb: int | None = None,
) -> RotationResult:
    """Run one rotation cycle."""
    target = Path(directory or config.BACKUP_DIRECTORY)
    max_days = max_retention_days if max_retention_days is not None else (
        config.ROTATION_CONFIG["max_retention_days"]
    )
    max_dir_mb = max_backup_dir_mb if max_backup_dir_mb is not None else (
        config.ROTATION_CONFIG["max_backup_dir_mb"]
    )

    target.mkdir(parents=True, exist_ok=True)
    initial_size_mb = get_directory_size_mb(target)
    archives = sorted(
        target.glob("*.zip"), key=lambda f: f.stat().st_mtime
    )
    cutoff = datetime.now() - timedelta(days=max_days)

    pruned_age = 0
    pruned_quota = 0
    started = time.monotonic()

    # Rule 1 - age cutoff
    surviving: List[Path] = []
    for archive in archives:
        mtime = datetime.fromtimestamp(archive.stat().st_mtime)
        if mtime < cutoff:
            log.info(
                "Deleting expired archive (mtime=%s > %d days): %s",
                mtime.isoformat(),
                max_days,
                archive.name,
            )
            try:
                archive.unlink()
                pruned_age += 1
            except OSError as exc:
                log.warning("Failed to delete %s: %s", archive, exc)
        else:
            surviving.append(archive)

    # Rule 2 - quota enforcement (keep deleting oldest until under quota)
    current_size_mb = get_directory_size_mb(target)
    while current_size_mb > max_dir_mb and surviving:
        oldest = surviving.pop(0)
        log.warning(
            "Backup quota exceeded (%.2f MB > %d MB). Deleting oldest: %s",
            current_size_mb,
            max_dir_mb,
            oldest.name,
        )
        try:
            oldest.unlink()
            pruned_quota += 1
        except OSError as exc:
            log.warning("Failed to delete %s: %s", oldest, exc)
        current_size_mb = get_directory_size_mb(target)

    final_size_mb = current_size_mb
    elapsed = time.monotonic() - started
    result = RotationResult(
        target_directory=str(target),
        pruned_by_age=pruned_age,
        pruned_by_quota=pruned_quota,
        initial_size_mb=round(initial_size_mb, 3),
        final_size_mb=round(final_size_mb, 3),
        duration_sec=round(elapsed, 3),
    )
    log.info(
        "Log rotation finished: %s",
        result.to_dict(),
    )
    return result


# ---------------------------------------------------------------------------
# Long-lived daemon
# ---------------------------------------------------------------------------
_run = True


def _handle_signal(signum, frame):  # noqa: ARG001
    global _run
    log.info("Received signal %d; shutting down rotator...", signum)
    _run = False


def serve() -> int:
    """Run forever, rotating logs every ``check_interval_sec``."""
    interval = int(config.ROTATION_CONFIG["check_interval_sec"])
    log.info(
        "Starting log rotator daemon (interval=%ds) against %s",
        interval,
        config.BACKUP_DIRECTORY,
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    while _run:
        try:
            run_log_rotation()
        except Exception as exc:  # noqa: BLE001
            log.exception("Rotation cycle failed: %s", exc)
        # sleep in short increments so signals are responsive
        slept = 0.0
        while _run and slept < interval:
            time.sleep(min(5.0, interval - slept))
            slept += 5.0
    log.info("Rotator daemon exiting cleanly.")
    return 0


def main() -> int:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the QXDM log retention engine.  Use --once to run a "
        "single cycle or no flag to daemonise."
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
        help="Override config check_interval_sec.",
    )
    args = parser.parse_args()

    if args.interval is not None:
        config.ROTATION_CONFIG["check_interval_sec"] = int(args.interval)

    if args.once:
        return 0 if not run_log_rotation().__dict__ else 0

    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
