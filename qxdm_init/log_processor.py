"""
Log Processing Pipeline.

Responsibilities
----------------
* Convert per-job binary diagnostics (.qmdl/.dlf/...) into plain text.
* Compress the original binaries into the global BACKUP_DIRECTORY.
* Enforce isolation: NEVER scan the global raw directory; only operate
  on the *exact* binary file paths passed in by the controller.

Configuration
-------------
The decoder is selected by ``config.MOCK_MODE``.  In production we
**never** fall back to the synthetic decoder if QCAT is missing - we
raise so an operator notices the misconfiguration.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import config  # type: ignore
except (ImportError, ModuleNotFoundError):
    from . import config  # type: ignore


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
    decoder: str = ""  # "mock" | "qcat"
    notes: List[str] = field(default_factory=list)

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
        }


# ---------------------------------------------------------------------------
# Decoder implementations
# ---------------------------------------------------------------------------
def _decode_with_mock(binary: Path, text_out: Path, scenario_name: str) -> int:
    """Generate a synthetic decoded-text log entry."""
    text_out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"=== QXDM DECODED LOG FILE: {binary.name} ===",
        f"Scenario: {scenario_name}",
        f"Decoded Time: {datetime.now().isoformat()}",
        "2026-08-27 10:48:00.120 [0x1544] LTE Serving Cell Info: "
        "RSRP=-88dBm RSRQ=-10dB SNR=18.5dB PCI=142",
        "2026-08-27 10:48:00.250 [0x1FEA] RRC_OTA_MSG: RRCReconfiguration complete",
        "2026-08-27 10:48:01.010 [0xB80A] 5GMM_REGISTRATION_REJECT: "
        "Cause #22 (Congestion), T3346=30s",
    ]
    text_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def _decode_with_qcat(binary: Path, text_out: Path) -> int:
    """Invoke the real QCAT decoder. Raises on failure."""
    if not os.path.isfile(config.CONVERTER_EXE):
        raise RuntimeError(
            f"QCAT converter not found at {config.CONVERTER_EXE!r}. "
            "Set QCAT_EXE or run with QXDM_MOCK_MODE=True."
        )
    text_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [config.CONVERTER_EXE, str(binary), str(text_out)]
    log.info("Executing QCAT (%s) on %s", config.CONVERTER_EXE, binary.name)
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"QCAT executable not runnable: {config.CONVERTER_EXE}"
        ) from exc
    if res.returncode != 0:
        raise RuntimeError(
            f"QCAT failed for {binary.name}: rc={res.returncode} "
            f"stderr={res.stderr.strip()[:500]!r}"
        )
    if not text_out.exists():
        raise RuntimeError(
            f"QCAT returned 0 but produced no output file at {text_out}"
        )
    # crude line count
    with text_out.open("r", encoding="utf-8", errors="replace") as fh:
        lines = sum(1 for _ in fh)
    return lines


def _decode_one(
    binary: Path, text_out: Path, scenario_name: str
) -> tuple[int, str]:
    """Run the appropriate decoder; return (line_count, decoder_label)."""
    if config.MOCK_MODE:
        return _decode_with_mock(binary, text_out, scenario_name), "mock"
    return _decode_with_qcat(binary, text_out), "qcat"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def convert_and_archive(
    scenario_name: str,
    job_id: str,
    binary_files: Optional[List[Path]] = None,
) -> ProcessingResult:
    """Process *only* the binary files in ``binary_files`` (per-job set).

    Parameters
    ----------
    scenario_name: str
        Logical scenario tag; used in archive names.
    job_id: str
        Unique per-call identifier; used in archive names for traceability.
    binary_files: list[Path] | None
        The exact set of files captured by the QXDM session.  When None
        and the global LOG_DIRECTORY is the *job-isolated* raw dir, we
        scan that directory instead.

    Returns
    -------
    ProcessingResult
    Structured outcome with per-artifact info and any failure details.
    """
    if not binary_files:
        log.warning(
            "convert_and_archive called with no binary_files (job_id=%s, "
            "scenario=%s) - this is normal only if the controller "
            "captured nothing.",
            job_id,
            scenario_name,
        )
        return ProcessingResult(job_id=job_id, scenario_name=scenario_name)

    result = ProcessingResult(job_id=job_id, scenario_name=scenario_name)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for bin_path in binary_files:
        bin_path = Path(bin_path)
        if not bin_path.exists():
            msg = f"missing binary file: {bin_path}"
            log.error(msg)
            result.failures.append({"file": str(bin_path), "reason": msg})
            continue

        # resolved output paths - the *text* log lives in the global
        # CONVERTED_DIRECTORY so downstream DB indexing can find it; the
        # *zip* archive is also global so retention is enforced.
        sanitized_stem = _safe_zip_stem(bin_path.stem)
        archive_name = (
            f"{_safe_filename(scenario_name)}_"
            f"{sanitized_stem}_"
            f"{job_id}_{timestamp}.zip"
        )
        zip_path = config.BACKUP_DIRECTORY / archive_name
        text_out = config.CONVERTED_DIRECTORY / (
            sanitized_stem + f"_{job_id}.txt"
        )

        # 1. convert
        try:
            line_count, decoder = _decode_one(bin_path, text_out, scenario_name)
        except Exception as exc:  # noqa: BLE001
            log.exception("Conversion failed for %s", bin_path)
            result.failures.append({
                "file": str(bin_path),
                "reason": str(exc),
            })
            continue

        # 2. compress raw binary into backup
        if zip_path.exists():
            log.warning("Overwriting existing archive %s", zip_path)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(bin_path, arcname=bin_path.name)

        # 3. cleanup the raw file in the *job* directory only
        try:
            bin_path.unlink()
        except OSError as exc:
            log.warning("Could not delete raw file %s: %s", bin_path, exc)

        result.artifacts.append(
            ProcessedArtifact(
                job_id=job_id,
                scenario_name=scenario_name,
                binary_archive=zip_path,
                text_log=text_out if text_out.exists() else None,
                decoded_lines=line_count,
                decoder=decoder,
            )
        )
        log.info(
            "Archived %s -> %s (decoder=%s, %d lines)",
            bin_path.name,
            zip_path.name,
            decoder,
            line_count,
        )

    log.info(
        "convert_and_archive done: %d artifacts, %d failures",
        len(result.artifacts),
        len(result.failures),
    )
    return result


# ---------------------------------------------------------------------------
# Path safety helpers
# ---------------------------------------------------------------------------
def _safe_filename(value: str, max_len: int = 80) -> str:
    """Sanitise a string for safe use in filenames."""
    keep = "-_.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    cleaned = "".join(c if c in keep else "_" for c in (value or "scenario"))
    cleaned = cleaned.strip("._")
    return (cleaned or "scenario")[:max_len]


def _safe_zip_stem(stem: str) -> str:
    return _safe_filename(stem, max_len=60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    """CLI: convert/zip a known set of binaries without going through HTTP."""
    import argparse

    parser = argparse.ArgumentParser(description="Process QXDM binaries.")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--job-id", default=datetime.now().strftime("%Y%m%d%H%M%S"))
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=None,
        help="Optional isolated raw dir to glob (per-job only).",
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Explicit binaries to process.  If empty and --raw-dir is "
        "given, the raw-dir is scanned.",
    )
    args = parser.parse_args()

    files = list(args.files)
    if not files and args.raw_dir:
        for pattern in ("*.dlf", "*.bin", "*.isf", "*.hdf", "*.qmdl"):
            files.extend(sorted(args.raw_dir.glob(pattern)))

    result = convert_and_archive(
        scenario_name=args.scenario,
        job_id=args.job_id,
        binary_files=files,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
