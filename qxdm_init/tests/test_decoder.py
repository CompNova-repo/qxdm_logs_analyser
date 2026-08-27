"""Decoder adapter tests."""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import pytest

from decoders import (
    ExternalDecoder,
    MockDecoder,
    build_decoder,
)


def test_mock_decoder_writes_text(tmp_path: Path):
    binary = tmp_path / "fake.qmdl"
    binary.write_bytes(b"\x7E" + b"x" * 100)
    text = tmp_path / "out.txt"
    result = MockDecoder().decode(binary, text, "scenario")
    assert text.exists()
    body = text.read_text(encoding="utf-8")
    assert "SYNTHETIC" in body
    assert result.decoder_label == "mock"
    assert result.line_count > 0


def test_external_decoder_missing_executable(tmp_path: Path):
    binary = tmp_path / "fake.qmdl"
    binary.write_bytes(b"x")
    decoder = ExternalDecoder(
        command_template="{qcat_exe} -i {input} -o {output}",
        qcat_exe="/nonexistent/qcat-binary",
    )
    with pytest.raises(RuntimeError):
        decoder.decode(binary, tmp_path / "out.txt", "scenario")


def test_external_decoder_fails_on_zero_exit(tmp_path: Path):
    if not shutil.which("false"):
        pytest.skip("'false' executable not available")
    decoder = ExternalDecoder(
        command_template="false {input} {output}",
    )
    binary = tmp_path / "fake.qmdl"
    binary.write_bytes(b"x")
    with pytest.raises(RuntimeError) as excinfo:
        decoder.decode(binary, tmp_path / "out.txt", "scenario")
    assert "rc=" in str(excinfo.value) or "exited" in str(excinfo.value).lower() or "returned" in str(excinfo.value).lower()


def test_external_decoder_produces_valid_output(tmp_path: Path):
    """Use a small Python script that copies input bytes to output."""
    binary = tmp_path / "fake.qmdl"
    binary.write_bytes(b"hello world\n")
    text = tmp_path / "out.txt"
    # shlex.split with posix=True treats '>' as a literal char; use a
    # portable redirection via /bin/sh -c.
    decoder = ExternalDecoder(command_template="/bin/sh -c 'cat {input} > {output}'")
    result = decoder.decode(binary, text, "scenario")
    assert text.exists()
    assert text.read_text() == "hello world\n"
    assert result.line_count == 1


def test_build_decoder_mock():
    decoder = build_decoder(decoder_kind="mock")
    assert isinstance(decoder, MockDecoder)


def test_build_decoder_external_requires_template():
    with pytest.raises(RuntimeError) as excinfo:
        build_decoder(decoder_kind="external", decoder_template=None)
    assert "QXDM_DECODER_TEMPLATE" in str(excinfo.value)


def test_build_decoder_none_refuses_to_run():
    with pytest.raises(RuntimeError):
        build_decoder(decoder_kind="none")


def test_build_decoder_unknown_kind():
    with pytest.raises(RuntimeError):
        build_decoder(decoder_kind="totally-made-up")


def test_production_decoder_missing_template_raises(tmp_path: Path):
    """Simulating production: mock_mode=False but external template absent."""
    from settings import Settings
    from log_processor import LogProcessor

    settings = Settings(
        decoder_kind="external",
        decoder_template=None,
        qcat_exe="/nonexistent",
        jobs_root=tmp_path / "jobs",
        converted_directory=tmp_path / "converted",
        backup_directory=tmp_path / "backup",
    )
    settings.ensure_directories()
    with pytest.raises(RuntimeError):
        LogProcessor(settings=settings)


def test_processor_preserves_raw_on_decoder_failure(tmp_path: Path, isolated_settings):
    """Raw binary must remain when the decoder fails."""
    from log_processor import LogProcessor

    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    binary = raw / "x.qmdl"
    binary.write_bytes(b"x" * 1024)

    class FailDecoder:
        label = "fail"
        def decode(self, binary, text_out, scenario_name):
            raise RuntimeError("decoder intentionally failed")

    isolated_settings = isolated_settings.with_overrides(
        decoder_kind="mock",  # ignored; we pass our own decoder
    )
    processor = LogProcessor(settings=isolated_settings, decoder=FailDecoder())
    result = processor.convert_and_archive(
        scenario_name="s", job_id="j", binary_files=[binary]
    )
    assert result.failures, "expected a recorded failure"
    assert binary.exists(), "raw binary must remain after decode failure"


def test_processor_preserves_raw_on_archive_failure(tmp_path: Path, isolated_settings, monkeypatch):
    from log_processor import LogProcessor

    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    binary = raw / "x.qmdl"
    binary.write_bytes(b"x" * 1024)

    isolated_settings = isolated_settings.with_overrides()
    processor = LogProcessor(settings=isolated_settings)

    import log_processor
    def _raise(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(log_processor.LogProcessor, "_create_archive",
                        staticmethod(_raise))
    result = processor.convert_and_archive(
        scenario_name="s", job_id="j", binary_files=[binary]
    )
    assert result.failures
    assert binary.exists(), "raw binary must remain after archive failure"
