"""Windows-side Device Agent for QXDM / QCAT automation.

This package contains:

* :mod:`qxdm_init.device_agent.protocol`  -- shared HTTP/JSON contract
  (job lifecycle states, request/response models, error codes).
* :mod:`qxdm_init.device_agent.backend`   -- backend interface plus
  ``MockDeviceBackend`` (Linux-runnable) and ``WindowsDeviceBackend``
  (Windows-only, lazy-imported so Linux tests do not need pywinauto).
* :mod:`qxdm_init.device_agent.server`    -- FastAPI app exposing the
  Device Agent surface.
* :mod:`qxdm_init.device_agent.client`    -- :class:`RemoteAgentClient`
  used by the Linux orchestrator.

Linux CI can exercise the entire stack by running the ``MockDeviceBackend``
behind the ``server`` module, and pointing ``RemoteAgentClient`` at it.
"""

from .protocol import (
    AgentError,
    ArtifactMetadata,
    JobRequest,
    JobState,
    JobStatus,
)

__all__ = [
    "AgentError",
    "ArtifactMetadata",
    "JobRequest",
    "JobState",
    "JobStatus",
]
