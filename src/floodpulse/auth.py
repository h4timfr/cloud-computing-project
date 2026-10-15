"""Shared-secret protection for the ingestion endpoint (M2).

Only ``POST /readings`` is protected. The dashboard's read endpoints
(``GET /zones``, ``GET /zones/{zone_id}/history``) stay public and never accept,
require or reveal the ingestion secret.

Design rules:

* the secret lives in the Lambda environment (``FLOODPULSE_INGEST_KEY``) — never in
  source code, never in the dashboard, never in a response payload
* it is compared with :func:`hmac.compare_digest`, so comparison time does not leak it
* in cloud storage mode (``FLOODPULSE_STORE=dynamodb``) a missing secret **fails closed**
  (HTTP 503) rather than silently accepting anonymous writes
* local in-process runs without a configured secret stay open, which keeps the M0/M1
  tests and ``tools/local_demo.py`` working unchanged
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Mapping

INGEST_KEY_ENV = "FLOODPULSE_INGEST_KEY"
INGEST_KEY_HEADER = "x-fp-key"
STORE_ENV = "FLOODPULSE_STORE"
CLOUD_STORE = "dynamodb"


def configured_key() -> str | None:
    """The configured ingestion secret, or ``None`` when unset/blank."""
    value = os.environ.get(INGEST_KEY_ENV, "")
    value = value.strip() if isinstance(value, str) else ""
    return value or None


def cloud_mode() -> bool:
    """True when the process is configured to persist into DynamoDB."""
    return os.environ.get(STORE_ENV, "local").strip().lower() == CLOUD_STORE


def requires_key() -> bool:
    """True when ingestion must be authenticated."""
    return configured_key() is not None or cloud_mode()


def header_key(headers: Mapping[str, Any] | None) -> str | None:
    """Case-insensitive lookup of the ingestion header; returns the raw value or ``None``."""
    if not headers:
        return None
    for name, value in headers.items():
        if str(name).strip().lower() == INGEST_KEY_HEADER:
            if value is None:
                return None
            text = str(value)
            return text if text else None
    return None


def check_ingest(headers: Mapping[str, Any] | None) -> tuple[int, dict[str, Any]] | None:
    """Return an error response when the request must be rejected, else ``None``.

    The error payloads never contain the secret, the expected value, its length or a
    partial match.
    """
    expected = configured_key()
    if expected is None:
        if cloud_mode():
            return 503, {
                "error": "ingest_not_configured",
                "detail": (
                    "ingestion is disabled: this deployment has no ingestion secret "
                    f"configured (set the {INGEST_KEY_ENV} environment variable)"
                ),
            }
        return None

    supplied = header_key(headers)
    if supplied is None:
        return 401, {
            "error": "unauthorized",
            "detail": f"missing required header '{INGEST_KEY_HEADER}'",
        }

    # compare as bytes so arbitrary header content can never raise, and do it in
    # constant time relative to the secret.
    if not hmac.compare_digest(supplied.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass")):
        return 401, {"error": "unauthorized", "detail": "invalid ingestion key"}
    return None
