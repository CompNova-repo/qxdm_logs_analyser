"""Verify Linux-side imports do NOT require Windows-only libraries."""

from __future__ import annotations

import importlib
import sys


def _import_fresh(name: str):
    """Import ``name`` with no cached module."""
    if name in sys.modules:
        del sys.modules[name]
    return importlib.import_module(name)


def test_settings_imports_clean():
    _import_fresh("settings")


def test_decoders_imports_clean():
    _import_fresh("decoders")


def test_log_processor_imports_clean():
    _import_fresh("log_processor")


def test_log_rotator_imports_clean():
    _import_fresh("log_rotator")


def test_qxdm_service_imports_clean():
    _import_fresh("qxdm_service")


def test_manifest_imports_clean():
    _import_fresh("manifest")


def test_device_agent_imports_clean():
    _import_fresh("device_agent.server")
    _import_fresh("device_agent.client")


def test_windows_only_imports_are_lazy():
    """pywinauto must NOT be imported just by importing qxdm_service."""
    import qxdm_service  # noqa: F401
    # If pywinauto were eager, it would already be in sys.modules
    assert "pywinauto" not in sys.modules


def test_real_controller_rejected_on_linux():
    import pytest
    from qxdm_service import RealQXDMController
    from settings import Settings

    settings = Settings()
    if sys.platform == "win32":
        pytest.skip("Windows-only behaviour")
    with pytest.raises(RuntimeError) as excinfo:
        RealQXDMController(settings=settings)
    assert "Windows" in str(excinfo.value)


def test_windows_backend_rejected_on_linux():
    import pytest
    from device_agent.backend import WindowsDeviceBackend

    if sys.platform == "win32":
        pytest.skip("Windows-only behaviour")
    with pytest.raises(RuntimeError):
        WindowsDeviceBackend(
            qxdm_exe="x", qcat_exe="y", store_dir="/tmp/x"
        )


def test_remote_controller_does_not_scan_tty():
    """The remote path must NOT enumerate /sys/class/tty on Linux."""
    from qxdm_service import RemoteAgentController
    from settings import Settings

    settings = Settings(
        mock_mode=False,
        device_agent_url="http://127.0.0.1:1",
    )
    controller = RemoteAgentController(settings=settings, job_id="x")
    # The class must not have any /sys/class/tty or COM-port enumeration
    # methods that the remote path is allowed to use.
    assert not hasattr(controller, "discover_com_ports")
    assert not hasattr(controller, "get_com_port")
