"""Minimal correlation envelope for backend logs and registration records."""

import contextvars
import os

SCHEMA_VERSION = 1
current_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("telemetry_run_id", default="")


def correlation_fields() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": os.environ.get("SMS_TOOL_EVENT_SOURCE", "live"),
        "command_id": os.environ.get("SMS_TOOL_COMMAND_ID", ""),
        "run_id": current_run_id.get(),
    }
