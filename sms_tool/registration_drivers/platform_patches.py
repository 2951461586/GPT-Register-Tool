"""Scoped launch workarounds, never process-global event-loop/env changes."""

import os
from typing import Any, Mapping

MOZ_DISABLE_CONTENT_SANDBOX = "MOZ_DISABLE_CONTENT_SANDBOX"


def camoufox_launch_env(config: Mapping[str, Any]) -> dict[str, str]:
    """Preserve the configured workaround in the child process only.

    Some Windows hosts cannot launch Firefox content processes with its sandbox
    enabled. Keep the existing opt-out setting, but avoid racing other browser
    launches by mutating os.environ. Disabling the workaround preserves the
    caller's original environment, including an explicitly set value.
    """
    environment = dict(os.environ)
    if bool(config.get("disable_content_sandbox", True)):
        environment[MOZ_DISABLE_CONTENT_SANDBOX] = "1"
    return environment
