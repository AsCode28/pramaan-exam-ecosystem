"""Environment-driven configuration for demo mode and the early-warning signal.

All settings follow the existing convention used by :mod:`app.core.cors` and
:mod:`app.services.ai_incident_service`: read from the environment at call
time, with a safe default when unset.

DEMO_MODE defaults to OFF so a non-demo deployment is never able to inject
failures, corrupt the ledger, or wipe its own database.
"""

from __future__ import annotations

import os

DEMO_MODE_ENV = "DEMO_MODE"

# Seconds without a heartbeat after which a node is flagged as becoming stale.
HEARTBEAT_STALE_SECONDS_ENV = "HEARTBEAT_STALE_SECONDS"
DEFAULT_HEARTBEAT_STALE_SECONDS = 45

# Finite, configurable Gemini request timeout in milliseconds.
GEMINI_TIMEOUT_MS_ENV = "GEMINI_TIMEOUT_MS"
DEFAULT_GEMINI_TIMEOUT_MS = 30_000

_TRUTHY = {"1", "true", "yes", "on", "enabled"}


def _as_bool(raw: str | None) -> bool:
    return raw is not None and raw.strip().lower() in _TRUTHY


def is_demo_mode() -> bool:
    """True only when DEMO_MODE is explicitly enabled. Defaults to False."""
    return _as_bool(os.getenv(DEMO_MODE_ENV))


def heartbeat_stale_seconds() -> int:
    """Configurable stale-heartbeat threshold, in seconds."""
    raw = os.getenv(HEARTBEAT_STALE_SECONDS_ENV)
    if raw is None:
        return DEFAULT_HEARTBEAT_STALE_SECONDS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_HEARTBEAT_STALE_SECONDS
    return value if value > 0 else DEFAULT_HEARTBEAT_STALE_SECONDS


def gemini_timeout_ms() -> int:
    """Finite Gemini request timeout, in milliseconds. Never infinite."""
    raw = os.getenv(GEMINI_TIMEOUT_MS_ENV)
    if raw is None:
        return DEFAULT_GEMINI_TIMEOUT_MS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_GEMINI_TIMEOUT_MS
    return value if value > 0 else DEFAULT_GEMINI_TIMEOUT_MS
