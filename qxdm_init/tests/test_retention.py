"""Retention safety tests: active files, symlinks, errors."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from log_rotator import LogRotator
from settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        base_dir=tmp_path,
        jobs_root=tmp_path / "jobs",
        converted_directory=tmp_path / "converted",
        backup_directory=tmp_path / "backup",
        configs_directory=tmp_path / "configs",
    )


def test_active_temp_file_protected(tmp_path: Path):
    """Files beginning with '.' or ending with .part must be skipped."""
    backup = tmp_path / "backup"
    backup.mkdir()
    settings = _settings(tmp_path)
    visible = backup / "complete.zip"
    visible.write_bytes(b"x")
    os.utime(visible, (time.time() - 86400 * 30, time.time() - 86400 * 30))
    hidden = backup / ".hidden.zip"
    hidden.write_bytes(b"x")
    os.utime(hidden, (time.time() - 86400 * 30, time.time() - 86400 * 30))
    active = backup / "writing.zip.part"
    active.write_bytes(b"x")
    os.utime(active, (time.time() - 86400 * 30, time.time() - 86400 * 30))

    result = LogRotator(settings=settings, directory=backup, max_retention_days=7).run_once()
    assert not visible.exists(), "old zip should be removed"
    assert hidden.exists(), "hidden/temp zip should not be touched"
    assert active.exists(), ".part zip should not be touched"
    assert result.skipped_active >= 1 or result.events


def test_symlink_outside_backup_is_ignored(tmp_path: Path):
    backup = tmp_path / "backup"
    backup.mkdir()
    settings = _settings(tmp_path)
    # Create a symlink that points outside the backup dir
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"x")
    link = backup / "trick.zip"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink not supported on this filesystem")

    result = LogRotator(settings=settings, directory=backup, max_retention_days=7).run_once()
    assert link.exists()  # not deleted
    assert result.skipped_symlink >= 1


def test_backup_dir_is_symlink_rejected(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    settings = _settings(tmp_path)
    link = tmp_path / "backup-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink not supported")
    with pytest.raises(RuntimeError):
        LogRotator(settings=settings, directory=link)


def test_age_and_quota_together(tmp_path: Path):
    backup = tmp_path / "backup"
    backup.mkdir()
    settings = _settings(tmp_path)
    old = backup / "old.zip"
    old.write_bytes(b"X" * 1024 * 1024)  # 1 MiB
    os.utime(old, (time.time() - 86400 * 30, time.time() - 86400 * 30))
    fresh = backup / "fresh.zip"
    fresh.write_bytes(b"Y" * 1024 * 1024)
    os.utime(fresh, (time.time() - 60, time.time() - 60))

    result = LogRotator(
        settings=settings, directory=backup,
        max_retention_days=7, max_backup_dir_mb=0,
    ).run_once()
    assert result.pruned_by_age >= 1
    assert not old.exists()


def test_corrupted_unreadable_archive_does_not_crash(tmp_path: Path):
    """Files we cannot stat should be skipped, not propagated."""
    backup = tmp_path / "backup"
    backup.mkdir()
    settings = _settings(tmp_path)
    # Create a directory named like a zip; the iteration should skip it.
    (backup / "looks-like.zip").mkdir()
    real = backup / "real.zip"
    real.write_bytes(b"x")
    result = LogRotator(settings=settings, directory=backup).run_once()
    assert result is not None


def test_concurrent_archive_during_rotation(tmp_path: Path):
    """Rotation must not delete a file that is currently being written.

    The convention is: archives are written to ``<name>.zip.part`` first
    and only renamed once stable.  The rotator must never touch ``.part``
    files even if the quota says delete everything else.
    """
    backup = tmp_path / "backup"
    backup.mkdir()
    settings = _settings(tmp_path)
    active_part = backup / "active.zip.part"
    active_part.write_bytes(b"new")
    os.utime(active_part, (time.time(), time.time()))

    rotator = LogRotator(settings=settings, directory=backup, max_backup_dir_mb=0)
    result = rotator.run_once()
    assert active_part.exists(), "active .part must survive even at quota=0"
    assert result.skipped_active >= 1
