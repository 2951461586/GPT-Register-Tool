"""The mutable ``CFG`` compatibility view.

``CFG`` predates :class:`~.runtime.RuntimeConfig` and stays for the modules that
mutate config in place and for ``unittest.mock.patch.dict``.  It is a *view*, not
a store: every read resolves the active ``RuntimeConfig``, so a workflow scope
cannot be bypassed by a module that kept an old reference.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from .runtime import _thaw, current_config_data


class LegacyConfigView(MutableMapping[str, Any]):
    """Compatibility view that reads from the injected RuntimeConfig.

    Local overrides exist only for legacy tests/integrations that mutate CFG;
    production reads always follow the ContextVar-backed application scope.
    """

    def __init__(self) -> None:
        self._overrides: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        return _thaw(current_config_data()[key])

    def __setitem__(self, key: str, value: Any) -> None:
        self._overrides[str(key)] = value

    def __delitem__(self, key: str) -> None:
        if key in self._overrides:
            del self._overrides[key]
            return
        raise KeyError(key)

    def __iter__(self):
        return iter(dict.fromkeys((*current_config_data().keys(), *self._overrides.keys())))

    def __len__(self) -> int:
        return len(set(current_config_data()) | set(self._overrides))

    def copy(self) -> dict[str, Any]:
        # unittest.mock.patch.dict must restore only explicit overrides.
        return dict(self._overrides)

    def clear(self) -> None:
        self._overrides.clear()


# CFG is a mutable-shape compatibility view for existing modules and tests.
# It performs no import-time I/O and follows the RuntimeConfig active in the
# current workflow context.
CFG: MutableMapping[str, Any] = LegacyConfigView()
