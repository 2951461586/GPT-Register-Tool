"""Package marker for the vendored protocol-payment extractors.

This directory is **not importable as a package**: the hyphen in
``protocol-payment`` makes ``services.protocol-payment`` an invalid module name.
The marker exists for the tools that need to know the directory is one unit --
static analysers, coverage sharding, dead-code heuristics -- because without it
every ``common.*`` import below looks like it comes from nowhere.

Two contracts meet here and neither is changed by this file:

* **Runtime**: each extractor is launched as a script with
  ``cwd=<its own directory>``. It puts this directory on ``sys.path`` itself and
  imports ``common.*`` as a *top-level* package. Nothing imports this module, so
  adding it cannot alter a subprocess launch.
* **Static analysis**: ``pyrightconfig.json`` mirrors the same import root with
  an ``executionEnvironments`` entry, and
  ``scripts/services_import_resolution.py`` verifies every ``common.*`` import
  resolves and every ``common/*.py`` is reachable.

Dependencies are declared in ``requirements.txt`` beside this file. The host
application's ``requirements.txt`` remains authoritative for pinned versions;
this file exists so the subprocess boundary has a readable dependency contract
of its own instead of inheriting one implicitly.
"""
