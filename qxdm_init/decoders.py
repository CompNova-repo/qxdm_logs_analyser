"""
Decoder abstraction for converting captured QXDM binaries to plain text.

The QCAT command line is build-/version-specific and CANNOT be hard-coded
inside the orchestrator.  Instead, :class:`LogDecoder` is an interface with
two implementations:

* :class:`MockDecoder` -- synthetic output used for offline Linux tests and
  explicitly enabled mock mode.  Clearly labelled as synthetic in every
  produced header.

* :class:`ExternalDecoder` -- invokes an external converter via a
  configurable command template (e.g. ``"{qcat} -i {input} -o {output}"``).
  Production deployments MUST supply a working command template; if the
  template is missing or the executable fails, the decoder raises and
  **the orchestrator never silently substitutes synthetic output**.

The :func:`build_decoder` factory chooses the implementation based on the
``Settings.decoder_kind`` field.
"""

from __future__ import annotations

import abc
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("QXDM_Decoder")


@dataclass
class DecoderResult:
    text_log: Path
    line_count: int
    decoder_label: str
    notes: list


class LogDecoder(abc.ABC):
    """Convert a captured binary into plain text."""

    label: str = "abstract"

    @abc.abstractmethod
    def decode(
        self, binary: Path, text_out: Path, scenario_name: str
    ) -> DecoderResult:
        """Produce a decoded text file; raise on failure."""


# ---------------------------------------------------------------------------
# Mock decoder (synthetic, clearly labelled)
# ---------------------------------------------------------------------------
class MockDecoder(LogDecoder):
    label = "mock"

    def decode(
        self, binary: Path, text_out: Path, scenario_name: str
    ) -> DecoderResult:
        text_out.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        lines = [
            f"### SYNTHETIC QXDM DECODE (NOT REAL QCAT OUTPUT) ###",
            f"# binary:    {binary.name}",
            f"# scenario:  {scenario_name}",
            f"# decoded:   {ts}",
            "# WARNING: produced by MockDecoder for offline Linux testing.",
            f"2026-08-27 10:48:00.120 [0x1544] LTE Serving Cell Info: "
            "RSRP=-88dBm RSRQ=-10dB SNR=18.5dB PCI=142",
            "2026-08-27 10:48:00.250 [0x1FEA] RRC_OTA_MSG: RRCReconfiguration complete",
            "2026-08-27 10:48:01.010 [0xB80A] 5GMM_REGISTRATION_REJECT: "
            "Cause #22 (Congestion), T3346=30s",
        ]
        text_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return DecoderResult(
            text_log=text_out,
            line_count=len(lines),
            decoder_label=self.label,
            notes=["mock decoder"],
        )


# ---------------------------------------------------------------------------
# External decoder (configurable command template)
# ---------------------------------------------------------------------------
class ExternalDecoder(LogDecoder):
    """Run a configured external converter with explicit placeholder substitution.

    The template MUST contain ``{input}`` and ``{output}`` placeholders.
    Optional placeholders: ``{binary_dir}``, ``{binary_stem}``, ``{qcat_exe}``.

    Example::

        QXDM_DECODER=external
        QXDM_DECODER_TEMPLATE="{qcat_exe} -i {input} -o {output}"
        QCAT_EXE=C:\\QCAT\\QCAT.exe
    """

    label = "external"

    def __init__(self, command_template: str, qcat_exe: Optional[str] = None):
        if not command_template or "{input}" not in command_template or "{output}" not in command_template:
            raise ValueError(
                "ExternalDecoder requires a command_template containing "
                "{input} and {output} placeholders."
            )
        self.template = command_template
        self.qcat_exe = qcat_exe

    def decode(
        self, binary: Path, text_out: Path, scenario_name: str
    ) -> DecoderResult:
        text_out.parent.mkdir(parents=True, exist_ok=True)
        cmd_str = self.template.format(
            input=str(binary),
            output=str(text_out),
            binary_dir=str(binary.parent),
            binary_stem=binary.stem,
            qcat_exe=self.qcat_exe or "",
        )
        cmd = shlex.split(cmd_str, posix=(os.name != "nt"))
        log.info(
            "Running external decoder (%s) for %s -> %s",
            self.label,
            binary.name,
            text_out.name,
        )
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
                f"External decoder executable not found: {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"External decoder timed out after {exc.timeout}s for {binary.name}"
            ) from exc
        if res.returncode != 0:
            raise RuntimeError(
                f"External decoder failed for {binary.name}: rc={res.returncode} "
                f"stderr={res.stderr.strip()[:500]!r}"
            )
        if not text_out.exists():
            raise RuntimeError(
                f"External decoder returned 0 but produced no output file at {text_out}"
            )
        if text_out.stat().st_size == 0:
            raise RuntimeError(
                f"External decoder produced an empty output file at {text_out}"
            )
        with text_out.open("r", encoding="utf-8", errors="replace") as fh:
            line_count = sum(1 for _ in fh)
        return DecoderResult(
            text_log=text_out,
            line_count=line_count,
            decoder_label=self.label,
            notes=[f"command={cmd_str}"],
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_decoder(
    decoder_kind: str,
    decoder_template: Optional[str] = None,
    qcat_exe: Optional[str] = None,
) -> LogDecoder:
    """Construct a :class:`LogDecoder` from settings.

    Production rule: ``decoder_kind='external'`` without a usable template
    is a HARD error -- the orchestrator must never silently fall back to
    the mock decoder in production.
    """
    kind = (decoder_kind or "mock").lower()
    if kind == "mock":
        return MockDecoder()
    if kind == "external":
        if not decoder_template:
            raise RuntimeError(
                "Production decoder requested (decoder_kind='external') but no "
                "QXDM_DECODER_TEMPLATE supplied.  Configure the deployment-"
                "specific command template (e.g. "
                "\"{qcat_exe} -i {input} -o {output}\")."
            )
        return ExternalDecoder(command_template=decoder_template, qcat_exe=qcat_exe)
    if kind == "none":
        raise RuntimeError(
            "decoder_kind='none' -- the pipeline is intentionally disabled. "
            "Set QXDM_DECODER=mock or external to enable."
        )
    raise RuntimeError(f"Unknown decoder_kind={decoder_kind!r}")
