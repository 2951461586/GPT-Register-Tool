"""Gate scripts, importable as a package.

Why this file exists
--------------------
Every gate under ``scripts/`` is designed to be run as ``python
scripts/<name>.py``, and the tests that pin them reach them the same way the
gate does: ``sys.path.insert(0, ROOT / "scripts")`` followed by a bare ``import
<name>``. That works at runtime but is invisible to a static analyser, so
``tests/governance/test_precommit_guard.py`` carried a frozen
``reportMissingImports`` for years, and ``pyrightconfig.json`` grew an
``extraPaths`` entry purely to paper over it.

Making ``scripts`` a real package lets a test write ``from scripts import
precommit_guard``, which resolves from the repository root with no path
manipulation and no configuration. Scripts that import their siblings by bare
name are unaffected: they still run as ``python scripts/<name>.py`` with
``scripts/`` first on ``sys.path``.

Do **not** start importing gates through this package from code that already
holds a bare-name reference -- ``scripts.foo`` and a bare ``import foo`` are two
distinct module objects, and a ``monkeypatch`` applied to one is invisible to the
other.
"""
