"""Behaviour tests for ``services/protocol-payment/common/file_loading.py``.

Batch 3 of the extractor consolidation (see
``docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md``).

Why these tests exist on top of the AST-equivalence proof
---------------------------------------------------------
The extraction is verified by ``runtime/tmp/p1_3_verify_batch3.py``, which
proves the shared bodies are AST-identical to every provider original after a
declared set of parameter substitutions.  That proof is necessary but **not
sufficient**, and this module is the counter-example:

The first draft wrote the ``env_get`` production default as a *nested* ``def``::

    if env_get is None:
        def env_get(name: str) -> str:        # shadows the parameter
            return os.environ.get(name, "")   # ...so this calls ITSELF

The inner ``def`` shadows the parameter of the same name, so the body can only
recurse into itself -- ``RecursionError`` on the very first default call.  Both
the AST proof and ``compileall`` passed it, because both only look at
*structure*: the recursion is a *binding* fact, not a syntactic one.

So the tests below are written to fail on binding and behaviour, not shape.
The ``_environ_get`` helper exists specifically to keep the default out of the
function body, and ``test_default_env_get_is_not_self_recursive`` is a guard
that would have caught the original mistake.
"""

import ast
import importlib.util
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services" / "protocol-payment" / "common" / "file_loading.py"
)
SPEC = importlib.util.spec_from_file_location("file_loading", MODULE_PATH)
LOADING = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = LOADING
SPEC.loader.exec_module(LOADING)


def _norm_identity(value: str) -> tuple[str, str]:
    return (value, "")


def _norm_split(value: str) -> tuple[str, str]:
    """Model the real normalisers, which split off a session token."""
    if "|" in value:
        token, session = value.split("|", 1)
        return (token, session)
    return (value, "")


class LoadProxyFileTests(unittest.TestCase):
    def test_missing_file_yields_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = LOADING.load_proxy_file(
                Path(tmp) / "nope.txt",
                register_for_redaction=lambda line: None,
                normalize=lambda line: line.strip(),
                shuffle=lambda items: None,
            )
        self.assertEqual(out, [])

    def test_each_line_is_registered_before_being_normalised(self):
        """Redaction must see the raw line, including ones that normalise away.

        The extractors register every line so a proxy that gets rejected by
        ``normalize`` still cannot leak into logs verbatim.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxies.txt"
            path.write_text("good-1\n\nbad value\ngood-2\n", encoding="utf-8", newline="\n")
            registered = []

            def normalize(line: str) -> str:
                return line.strip() if line.strip().startswith("good-") else ""

            def register(line: str) -> None:
                self.assertIn(line, ["good-1\n", "\n", "bad value\n", "good-2\n"])
                registered.append(line)

            out = LOADING.load_proxy_file(
                path,
                register_for_redaction=register,
                normalize=normalize,
                shuffle=lambda items: None,
            )

        self.assertEqual(out, ["good-1", "good-2"])
        # Every line, not just the accepted ones.
        self.assertEqual(len(registered), 4)

    def test_shuffle_is_applied_to_the_accepted_list(self):
        """The injection point is real, not decorative."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxies.txt"
            path.write_text("a\nb\nc\n", encoding="utf-8", newline="\n")
            seen = []

            def shuffle(items):
                seen.append(list(items))
                items.reverse()

            out = LOADING.load_proxy_file(
                path,
                register_for_redaction=lambda line: None,
                normalize=lambda line: line.strip(),
                shuffle=shuffle,
            )

        self.assertEqual(seen, [["a", "b", "c"]])
        self.assertEqual(out, ["c", "b", "a"])

    def test_default_shuffle_is_random_shuffle(self):
        """Production callers omit ``shuffle``; it must still shuffle.

        An earlier draft wrote ``shuffle=random.shuffle`` as a default argument.
        Because ``random.shuffle`` is a *bound method of the module-level
        ``Random`` instance*, it is frozen at def time and can never be patched
        afterwards -- so the documented "injectable for deterministic tests"
        seam did not actually work.  Assert the late-bound call, not the object
        identity of a captured default.
        """
        default = inspect.signature(LOADING.load_proxy_file).parameters["shuffle"].default
        self.assertIsNone(default, "default must be None; see _shuffle_in_place docstring")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxies.txt"
            path.write_text("a\nb\nc\n", encoding="utf-8", newline="\n")
            with mock.patch.object(LOADING.random, "shuffle") as patched:
                out = LOADING.load_proxy_file(
                    path,
                    register_for_redaction=lambda line: None,
                    normalize=lambda line: line.strip(),
                )
        self.assertTrue(patched.called, "patching random.shuffle must reach the call")
        self.assertEqual(out, ["a", "b", "c"])


class LoadTokenTests(unittest.TestCase):
    def _call(self, **overrides):
        kwargs = dict(
            env_names=("PP_TOKEN", "IDEAL_TOKEN"),
            token_file=Path("/definitely/not/here/token.txt"),
            normalize_token=_norm_identity,
            log=lambda message: None,
        )
        kwargs.update(overrides)
        # The default `env_names` tuple is empty in most tests' environments, so
        # control would reach the interactive ``input()`` fallback.  Pin a
        # non-interactive prompt unless the test explicitly supplies one.
        kwargs.setdefault("prompt", lambda message: "")
        return LOADING.load_token(**kwargs)

    def test_default_env_get_is_not_self_recursive(self):
        """Regression: a nested ``def env_get`` shadowed the parameter.

        The nested form makes the default path call itself forever.  Asserting
        "it returns" is what distinguishes the two implementations -- an AST
        comparison sees them as nearly identical.

        Note ``env_get`` is deliberately **omitted** here: that is the whole
        point.  ``clear=True`` removes a stray ``PP_TOKEN`` from the shell.
        """
        with mock.patch.dict(os.environ, {"PP_TOKEN": "from-environ"}, clear=True):
            token, session = self._call()
        self.assertEqual(token, "from-environ")
        self.assertEqual(session, "")

        with mock.patch.dict(os.environ, {"PP_TOKEN": "from-environ"}, clear=True):
            self.assertEqual(LOADING._environ_get("PP_TOKEN"), "from-environ")
        self.assertEqual(LOADING._environ_get("__ABSENT__"), "")

    def test_env_var_name_order_is_the_injected_tuple(self):
        """ideal/blik and twint differ here; the injected tuple must decide.

        Uses an explicit ``env_get`` rather than the real environment so the
        test cannot be contaminated by whatever ``PP_TOKEN`` happens to be set
        in the developer's shell.
        """
        env = {"TWINT_TOKEN": "twint-value"}

        def env_get(name: str) -> str:
            return env.get(name, "")

        token, _ = self._call(env_names=("PP_TOKEN", "TWINT_TOKEN"), env_get=env_get)
        self.assertEqual(token, "twint-value")

        token, _ = self._call(env_names=("PP_TOKEN", "IDEAL_TOKEN"), env_get=env_get)
        self.assertEqual(token, "", "ideal's tuple must NOT see TWINT_TOKEN")

    def test_earlier_env_name_wins_over_a_later_one(self):
        """The tuple's order is the contract, not just its membership."""
        env = {"PP_TOKEN": "first", "IDEAL_TOKEN": "second"}

        token, _ = self._call(
            env_names=("PP_TOKEN", "IDEAL_TOKEN"), env_get=lambda name: env.get(name, "")
        )
        self.assertEqual(token, "first")

    def test_env_path_reports_the_name_that_hit(self):
        messages = []
        with mock.patch.dict(os.environ, {"TWINT_TOKEN": "v"}, clear=False):
            self._call(env_names=("PP_TOKEN", "TWINT_TOKEN"), log=messages.append)
        self.assertIn("使用环境变量 TWINT_TOKEN", messages)

    def test_session_cookie_env_overrides_the_parsed_one(self):
        with mock.patch.dict(
            os.environ, {"PP_TOKEN": "tok|parsed", "PP_SESSION_TOKEN": "explicit"}, clear=False
        ):
            token, session = self._call(
                normalize_token=_norm_split, env_get=lambda name: os.environ.get(name, "")
            )
        self.assertEqual(token, "tok")
        self.assertEqual(session, "explicit")

    def test_parsed_session_is_used_when_the_env_is_blank(self):
        with mock.patch.dict(os.environ, {"PP_SESSION_TOKEN": ""}, clear=False):
            token, session = self._call(
                normalize_token=_norm_split,
                env_get=lambda name: {"PP_TOKEN": "tok|parsed"}.get(name, ""),
            )
        self.assertEqual((token, session), ("tok", "parsed"))

    def test_token_file_is_read_when_no_env_var_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.txt"
            path.write_text("file-token\n", encoding="utf-8", newline="\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                token, session = self._call(
                    token_file=path,
                    env_get=lambda name: "",
                )
        self.assertEqual((token, session), ("file-token", ""))

    def test_decode_ladder_handles_utf16_and_bom(self):
        """The ``for/else`` decode ladder is part of the original behaviour."""
        cases = {
            "utf-16": "utf16-token".encode("utf-16"),
            "utf-8-sig": "bom-token".encode("utf-8-sig"),
        }
        for label, payload in cases.items():
            with self.subTest(encoding=label), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "token.txt"
                path.write_bytes(payload)
                with mock.patch.dict(os.environ, {}, clear=True):
                    token, _ = self._call(token_file=path, env_get=lambda name: "")
                self.assertTrue(token.endswith("token"), "%s -> %r" % (label, token))

    def test_undecodable_bytes_fall_back_to_ignore_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.txt"
            path.write_bytes(b"\xff\xfe\x00bad\x00\x01")
            with mock.patch.dict(os.environ, {}, clear=True):
                token, _ = self._call(token_file=path, env_get=lambda name: "")
        self.assertIsInstance(token, str)

    def test_falls_back_to_the_prompt_when_nothing_else_has_a_token(self):
        prompts = []

        def prompt(message: str) -> str:
            prompts.append(message)
            return "  typed-token  "

        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "token.txt"  # exists but is empty
            empty.write_text("", encoding="utf-8", newline="\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                token, session = self._call(
                    token_file=empty, prompt=prompt, env_get=lambda name: ""
                )
        self.assertEqual(token, "typed-token")
        self.assertEqual(session, "")
        self.assertEqual(prompts, ["请输入 access_token: "])

    def test_prompt_session_token_is_parsed_from_the_typed_blob(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            token, session = self._call(
                prompt=lambda message: "typed|session",
                normalize_token=_norm_split,
                env_get=lambda name: "",
            )
        self.assertEqual((token, session), ("typed", "session"))


class InjectableSeamTests(unittest.TestCase):
    """Every injected seam must actually be reachable from outside.

    A parameter whose default is a *bound* callable or a builtin is captured at
    def time and can never be patched afterwards.  That trap already produced
    two dead seams in this module (``shuffle`` and ``prompt``), so it is
    asserted rather than assumed.
    """

    def test_prompt_seam_is_patchable_via_builtins(self):
        with mock.patch("builtins.input", return_value="from-patched-input") as patched:
            with mock.patch.dict(os.environ, {}, clear=True):
                token, _ = LOADING.load_token(
                    env_names=("PP_TOKEN",),
                    token_file=Path("/definitely/not/here/token.txt"),
                    normalize_token=_norm_identity,
                    log=lambda message: None,
                    env_get=lambda name: "",
                )
        self.assertTrue(patched.called, "patching builtins.input must reach prompt()")
        self.assertEqual(token, "from-patched-input")

    def test_shuffle_seam_is_patchable_via_the_module_attribute(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxies.txt"
            path.write_text("a\nb\nc\n", encoding="utf-8", newline="\n")
            with mock.patch.object(LOADING.random, "shuffle") as patched:
                LOADING.load_proxy_file(
                    path,
                    register_for_redaction=lambda line: None,
                    normalize=lambda line: line.strip(),
                )
        self.assertTrue(patched.called, "patching random.shuffle must reach shuffle()")

    def test_no_default_argument_captures_a_callable_at_def_time(self):
        """Guard the pattern, not just the two instances of it.

        Two severities, and the distinction matters because the mild one is
        tolerable while the severe one is not:

        * ``ast.Attribute`` defaults (``random.shuffle``, ``os.environ.get``)
          are **bound methods**: they already carry their hidden receiver, so
          patching the owning module attribute afterwards cannot reach them.
          Strictly un-injectable -- this is what ``shuffle`` was doing.

        * ``ast.Name`` defaults to a builtin (``input``, ``print``, ``open``)
          are merely captured *references*.  ``mock.patch("builtins.input")``
          cannot reach them, but they resolve ``sys.stdin`` / ``sys.stdout`` at
          call time, so ``capsys`` and ``redirect_stdout`` still work.  Bad
          because it removes the seam, not because it is unreachable.

        Both are rejected here; rescinding the second kind needs a reason.
        """
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        bound, builtin = [], []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for default in list(node.args.defaults) + list(node.args.kw_defaults):
                if isinstance(default, ast.Attribute):
                    bound.append("%s: %s" % (node.name, ast.unparse(default)))
                elif isinstance(default, ast.Name) and default.id in {
                    "input", "print", "open", "shuffle",
                }:
                    builtin.append("%s: %s" % (node.name, ast.unparse(default)))
        self.assertEqual(bound + builtin, [],
                         "captured-early default: bound=%s builtin=%s" % (bound, builtin))


class ModuleContractTests(unittest.TestCase):
    def test_stays_free_of_the_host_package(self):
        """architecture.md Rule 10: protocol-payment must not import sms_tool."""
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("sms_tool", imported)

    def test_only_imports_stdlib(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        tops = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tops.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                tops.add(node.module.split(".")[0])
        self.assertEqual(tops, {"__future__", "os", "random", "pathlib", "typing"})

    def test_exports_exactly_the_two_extracted_functions(self):
        self.assertEqual(sorted(LOADING.__all__), ["load_proxy_file", "load_token"])

    def test_no_nested_def_shadows_an_injected_parameter(self):
        """Generalised guard for the bug this module already shipped once.

        A nested function whose name matches one of its enclosing function's
        parameters is unreachable from inside -- every reference to the name
        resolves to the nested ``def``, not the parameter.  When the nested
        def's job is to *supply a default for* that parameter, the result is
        unconditional self-recursion.

        The first version of this guard only looked at direct children of the
        function body, so it missed a ``def`` nested inside an ``if`` -- i.e.
        precisely the shape the real bug had.  Mutation testing caught that
        (M1 went green when it should have gone red).  Walking the whole
        subtree is the fix.
        """
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        offenders = []
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef):
                continue
            params = {a.arg for a in func.args.args + func.args.kwonlyargs + func.args.posonlyargs}
            if func.args.vararg:
                params.add(func.args.vararg.arg)
            if func.args.kwarg:
                params.add(func.args.kwarg.arg)
            if not params:
                continue
            # Walk the WHOLE subtree, and do not descend into a nested function
            # that already shadowed a name -- otherwise its own parameters get
            # compared against the outer scope's names.
            stack = list(func.body)
            while stack:
                node = stack.pop()
                if isinstance(node, ast.FunctionDef):
                    if node.name in params:
                        offenders.append("%s -> nested def %s" % (func.name, node.name))
                    continue
                stack.extend(ast.iter_child_nodes(node))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
