"""Application configuration: shard layout, validation, runtime view and ``CFG``.

This package was one 1,100-line module until 2026-10-22.  The split is by
responsibility -- :mod:`.loader` owns the shard files, :mod:`.schema` owns
validation, :mod:`.runtime` owns the frozen view, :mod:`.legacy` owns ``CFG`` --
and **every public name is re-exported here**, so the 73 modules that do
``from .config import ...`` did not have to change.

Three things about this file are load-bearing rather than cosmetic:

* ``current_config_data`` must stay re-exported: ``registration_probe`` calls it
  as ``config_module.current_config_data()`` and ``sentinel/bundle`` imports it
  function-locally from the package, so patching
  ``sms_tool.config.current_config_data`` reaches both.  Removing the re-export
  would make those seams inert without failing anything loudly.
* the private helpers (``_freeze``, ``_write_shards``, ...) stay re-exported
  because tests and docstrings name them as ``config._freeze``.  These are
  re-exports of the real definitions, never copies.
* every re-export uses the redundant-alias form (``import x as x``) so
  ``unused_import_ratchet`` keeps reading this file as "no unused imports"
  instead of counting 30 new ones.

Mutable state is deliberately **not** re-exported: ``CONFIG_DIR`` lives in
:mod:`sms_tool.config._state` and has to be patched there, because a package-level
copy would freeze the value and turn the seam inert.
"""

from __future__ import annotations

from ..kernel.paths import (
    resolve_config_file as resolve_config_file,
)  # re-export: config is the layer subpackages already depend on
from .legacy import CFG as CFG
from .legacy import LegacyConfigView as LegacyConfigView
from .loader import EXAMPLE_CONFIG_NAME as EXAMPLE_CONFIG_NAME
from .loader import SHARD_FILES as SHARD_FILES
from .loader import SHARD_OWNERSHIP as SHARD_OWNERSHIP
from .loader import _atomic_write_json as _atomic_write_json
from .loader import _deep_merge as _deep_merge
from .loader import _dump_json as _dump_json
from .loader import _read_text_or_none as _read_text_or_none
from .loader import _split_into_shards as _split_into_shards
from .loader import _write_shards as _write_shards
from .loader import default_config_dir as default_config_dir
from .loader import default_config_path as default_config_path
from .loader import example_config_candidates as example_config_candidates
from .loader import initialize_config_shards as initialize_config_shards
from .loader import load_merged_config as load_merged_config
from .runtime import ConfigInput as ConfigInput
from .runtime import RuntimeConfig as RuntimeConfig
from .runtime import _freeze as _freeze
from .runtime import _load_config as _load_config
from .runtime import _thaw as _thaw
from .runtime import current_config_data as current_config_data
from .runtime import current_runtime_config as current_runtime_config
from .runtime import default_runtime_config as default_runtime_config
from .runtime import initialize_runtime_config as initialize_runtime_config
from .runtime import load_config as load_config
from .runtime import load_runtime_config as load_runtime_config
from .runtime import resolve_runtime_config as resolve_runtime_config
from .runtime import runtime_config_scope as runtime_config_scope
from .schema import ACCOUNT_HEALTH_KEYS as ACCOUNT_HEALTH_KEYS
from .schema import ACCOUNT_HEALTH_WORKER_RANGE as ACCOUNT_HEALTH_WORKER_RANGE
from .schema import ConfigError as ConfigError
from .schema import _log_config_warnings as _log_config_warnings
from .schema import _validate_payment_config as _validate_payment_config
from .schema import _validate_positive_numbers as _validate_positive_numbers
from .schema import config_warnings as config_warnings
from .schema import validate_config as validate_config
from .schema import validate_registration_driver_config as validate_registration_driver_config
