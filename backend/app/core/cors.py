"""CORS configuration for local frontend development.

Allowed origins are read from the ``CORS_ALLOW_ORIGINS`` environment variable as
a comma-separated list, e.g.::

    CORS_ALLOW_ORIGINS=http://localhost:3000,http://localhost:5173

Design rules
------------
- The default is the two normal local frontend dev servers (CRA/Next on 3000
  and Vite on 5173), so the stack works out of the box.
- ``allow_credentials`` stays False. That is what makes a wildcard origin
  legal; a wildcard plus credentials is rejected by browsers, so we never
  default to ``*``. If an operator does configure ``*``, credentials are
  forced off rather than emitting an unusable, browser-rejected policy.
- An unparsable/empty variable falls back to the local defaults instead of
  silently opening the API up.
"""

from __future__ import annotations

import os

CORS_ALLOW_ORIGINS_ENV = "CORS_ALLOW_ORIGINS"

# Normal local frontend development origins.
DEFAULT_ALLOW_ORIGINS = (
    "http://localhost:3000",
    "http://localhost:5173",
)

# Headers a browser frontend needs on cross-origin JSON requests.
DEFAULT_ALLOW_METHODS = ("GET", "POST", "OPTIONS")
DEFAULT_ALLOW_HEADERS = ("Accept", "Authorization", "Content-Type", "Origin")


def parse_origins(raw: str | None) -> list[str]:
    """Parse the env var into a clean list of origins.

    Falls back to the local dev defaults when unset, blank or unparsable.
    """
    if raw is None:
        return list(DEFAULT_ALLOW_ORIGINS)

    origins = [origin.strip().rstrip("/") for origin in raw.split(",")]
    origins = [origin for origin in origins if origin]
    return origins or list(DEFAULT_ALLOW_ORIGINS)


def get_allow_origins() -> list[str]:
    """Resolved CORS origins for the current environment."""
    return parse_origins(os.getenv(CORS_ALLOW_ORIGINS_ENV))


def get_allow_credentials(origins: list[str]) -> bool:
    """Credentials are never allowed with a wildcard origin."""
    if "*" in origins:
        return False
    return True
