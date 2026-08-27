"""
Log Processing Pipeline.

Responsibilities
----------------
* Convert per-job binary diagnostics (``.qmdl``/``.dlf``/...) into plain
  text via the :class:`qxdm_init.decoders.LogDecoder` interface.
* Compress the original binaries into the global backup directory.
* Enforce isolation: only operate on the **exact** binary file paths
  passed in by the controller -- never scan a global raw directory.
* Transactional: if any step fails, the original raw binary is preserved
  and the failure is reported in :class:`ProcessingResult`.  Success only
  happens after decode + archive + zip validation.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .decoders import LogDecoder, build_decoder
    from .manifest import ManifestArtifact
    from .settings import Settings
except ImportError:
    from decoders import LogDecoder, build_decoder  # type: ignore
    from manifest import ManifestArtifact  # type: ignore
    from settings import Settings  # type: ignore

log = logging.getLogger("Log_Processor")


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------
@dataclass
class ProcessedArtifact:
    """One converted binary, its zip archive and decoded text path."""

    job_id: str
    scenario_name: str
    binary_archive: Path
    text_log: Optional[Path]
    decoded_lines: int = 0
    decoder: str = ""
    notes: List[str] = field(default_factory=list)

    def to_manifest(self) -> ManifestArtifact:
        return ManifestArtifact(
            filename=Path(self.binary_archive).name,
            size_bytes=Path(self.binary_archive).stat().st_size if Path(self.binary_archive).exists() else 0,
            archive_path=str(self.binary_archive),
            text_log_path=str(self.text_log) if self.text_log else None,
            decoder=self.decoder,
            decoded_lines=self.decoded_lines,
            notes=list(self.notes),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["binary_archive"] = str(self.binary_archive)
        d["text_log"] = str(self.text_log) if self.text_log else None
        return d


@dataclass
class ProcessingResult:
    job_id: str
    scenario_name: str
    artifacts: List[ProcessedArtifact] = field(default_factory=list)
    failures: List[Dict[str, str]] = field(default_factory=list)
    deleted_raw: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and bool(self.artifacts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "scenario_name": self.scenario_name,
            "ok": self.ok,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "failures": list(self.failures),
            "deleted_raw": list(self.deleted_raw),
        }


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------
_FILENAME_SAFE_KEEP = (
    "-_.abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


def _safe_filename(value: str, max_len: int = 80) -> str:
    cleaned = "".join(c if c in _FILENAME_SAFE_KEEP else "_" for c in (value or "scenario"))
    cleaned = cleaned.strip("._")
    return (cleaned or "scenario")[:max_len]


def _safe_zip_stem(stem: str) -> str:
    return _safe_filename(stem, max_len=60)


# ---------------------------------------------------------------------------
# Processing engine
# ---------------------------------------------------------------------------
class LogProcessor:
    """Stateless processor that converts + archives binaries for one job."""

    def __init__(self, settings: Settings, decoder: Optional[LogDecoder] = None):
        self.settings = settings
        self.decoder = decoder or build_decoder(
            decoder_kind=settings.decoder_kind,
            decoder_template=settings.decoder_template,
            qcat_exe=settings.qcat_exe,
        )

    # ------------------------------------------------------------------
    def convert_and_archive(
        self,
        scenario_name: str,
        job_id: str,
        binary_files: Optional[List[Path]] = None,
    ) -> ProcessingResult:
        result = ProcessingResult(job_id=job_id, scenario_name=scenario_name)
        if not binary_files:
            log.warning(
                "convert_and_archive called with no binary_files (job_id=%s, "
                "scenario=%s) - normal only if the controller captured nothing.",
                job_id,
                scenario_name,
            )
            return result

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for raw in binary_files:
            bin_path = Path(raw)
            if not bin_path.exists():
                msg = f"missing binary file: {bin_path}"
                log.error(msg)
                result.failures.append({"file": str(bin_path), "reason": msg})
                continue

            # 1. decode (writes to .part then atomically renames)
            sanitized_stem = _safe_zip_stem(bin_path.stem)
            archive_name = (
                f"{_safe_filename(scenario_name)}_"
                f"{sanitized_stem}_{job_id}_{timestamp}.zip"
            )
            zip_path = self.settings.backup_directory / archive_name
            text_out = self.settings.converted_directory / (
                sanitized_stem + f"_{job_id}.txt"
            )

            try:
                decoder_result = self.decoder.decode(
                    binary=bin_path,
                    text_out=text_out,
                    scenario_name=scenario_name,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Conversion failed for %s", bin_path)
                result.failures.append({
                    "file": str(bin_path),
                    "reason": f"decoder_error: {exc}",
                })
                # Raw binary must remain for debugging.
                continue

            # 2. archive raw binary atomically
            try:
                self._create_archive(zip_path, bin_path)
            except Exception as exc:  # noqa: BLE001
                log.exception("Archive failed for %s", bin_path)
                result.failures.append({
                    "file": str(bin_path),
                    "reason": f"archive_error: {exc}",
                })
                # Leave the decoded text in place but keep raw; do not
                # pretend success.
                continue

            # 3. validate archive contents
            try:
                self._validate_archive(zip_path, bin_path)
            except Exception as exc:  # noqa: BLE001
                log.exception("Archive validation failed for %s", zip_path)
                result.failures.append({
                    "file": str(bin_path),
                    "reason": f"archive_validation_error: {exc}",
                })
                # Remove the bad archive so it doesn't pollute retention.
                try:
                    zip_path.unlink()
                except OSError:
                    pass
                continue

            # 4. only NOW remove the raw binary
            try:
                bin_path.unlink()
                result.deleted_raw.append(str(bin_path))
            except OSError as exc:
                log.warning("Could not delete raw %s: %s", bin_path, exc)
                # Not a fatal failure -- archive is good.

            result.artifacts.append(
                ProcessedArtifact(
                    job_id=job_id,
                    scenario_name=scenario_name,
                    binary_archive=zip_path,
                    text_log=decoder_result.text_log
                    if decoder_result.text_log.exists()
                    else None,
                    decoded_lines=decoder_result.line_count,
                    decoder=decoder_result.decoder_label,
                    notes=list(decoder_result.notes),
                )
            )
            log.info(
                "Archived %s -> %s (decoder=%s, %d lines)",
                bin_path.name,
                zip_path.name,
                decoder_result.decoder_label,
                decoder_result.line_count,
            )
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _create_archive(zip_path: Path, bin_path: Path) -> None:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(zip_path.parent), prefix=f".{zip_path.name}.", suffix=".part"
        )
        os.close(tmp_fd)
        try:
            with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(bin_path, arcname=bin_path.name)
            os.replace(tmp_name, zip_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _validate_archive(zip_path: Path, bin_path: Path) -> None:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError(f"zip integrity test failed: {bad}")
            names = zf.namelist()
        if bin_path.name not in names:
            raise RuntimeError(
                f"archive missing expected entry {bin_path.name!r}: {names!r}"
            )


# ---------------------------------------------------------------------------
# Convenience function (legacy module-level API)
# ---------------------------------------------------------------------------
def convert_and_archive(
    scenario_name: str,
    job_id: str,
    binary_files: Optional[List[Path]] = None,
    settings: Optional[Settings] = None,
    decoder: Optional[LogDecoder] = None,
) -> ProcessingResult:
    """Module-level convenience shim for backwards compatibility."""
    from .settings import from_env
    settings = settings or from_env()
    processor = LogProcessor(settings=settings, decoder=decoder)
    return processor.convert_and_archive(
        scenario_name=scenario_name,
        job_id=job_id,
        binary_files=binary_files,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    """CLI: convert/zip a known set of binaries without going through HTTP."""
    import argparse

    from .settings import from_env

    parser = argparse.ArgumentParser(description="Process QXDM binaries.")
    parser.add_argument("--scenario", required=True)
    parser.add_argument(
        "--job-id",
        default=datetime.now().strftime("%Y%m%d%H%M%S"),
    )
    parser.add_argument("--raw-dir", type=Path, default=None)
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Explicit binaries to process.",
    )
    args = parser.parse_args()

    files = list(args.files)
    if not files and args.raw_dir:
        for pattern in ("*.dlf", "*.bin", "*.isf", "*.hdf", "*.qmdl"):
            files.extend(sorted(args.raw_dir.glob(pattern)))

    settings = from_env()
    result = convert_and_archive(
        scenario_name=args.scenario,
        job_id=args.job_id,
        binary_files=files,
        settings=settings,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
