"""Per-domain CLI argument groups for ``cli.build_parser``.

Each module exposes ``register(parser)`` holding the ``add_argument``
calls that used to live inline in ``cli.py``.  ``cli.build_parser``
calls them in the original order, so the assembled parser is
behaviourally identical; this package only exists so that adding a
flag for domain X edits ``cli_parsers/X.py`` instead of a 198-line
block in the middle of ``cli.py``.
"""
