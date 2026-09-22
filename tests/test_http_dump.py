"""Behaviour tests for ``services/protocol-payment/common/http_dump.py``.

Batch 4 of the extractor consolidation.  Scope note: `dump_http` also exists in
blik, but blik's version ignores its ``force`` argument and mkdirs lazily, so
it is deliberately NOT extracted -- see the module docstring.  These tests pin
the ideal/twint shape.
"""

import ast
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services" / "protocol-payment" / "common" / "http_dump.py"
)
SPEC = importlib.util.spec_from_file_location("http_dump", MODULE_PATH)
DUMP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = DUMP
SPEC.loader.exec_module(DUMP)


class FakeResponse:
    def __init__(self, status_code=200, url="https://example.test/x", text="body"):
        self.status_code = status_code
        self.url = url
        self.text = text


def _env_bool(mapping):
    def env_bool(name, default=False):
        raw = mapping.get(name, "")
        if raw == "":
            return default
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    return env_bool


def _call(dump_dir, env=None, force=False, stage="checkout", body=None,
          response=None, counter=None, redact=lambda text: text,
          strftime=lambda fmt: "20260101-000000"):
    return DUMP.dump_http(
        response,
        stage,
        body,
        "POST",
        "https://example.test/x",
        force,
        dump_env="X_DUMP",
        dump_dir=dump_dir,
        counter=counter or DUMP.DumpCounter(),
        redact_text=redact,
        env_bool=_env_bool(env or {}),
        strftime=strftime,
    )


class DumpCounterTests(unittest.TestCase):
    def test_starts_at_one_and_increments(self):
        counter = DUMP.DumpCounter()
        self.assertEqual(counter.value, 0)
        self.assertEqual(counter.next(), 1)
        self.assertEqual(counter.next(), 2)
        self.assertEqual(counter.value, 2)

    def test_instances_are_independent(self):
        """One counter per extractor -- a shared one would couple filenames."""
        a, b = DUMP.DumpCounter(), DUMP.DumpCounter()
        a.next()
        a.next()
        self.assertEqual(a.value, 2)
        self.assertEqual(b.value, 0)
        self.assertEqual(b.next(), 1)

    def test_next_acquires_the_lock(self):
        """Deterministic guard for the lock itself.

        The concurrency test below is probabilistic: under the GIL a bare
        ``_value += 1`` usually survives 1600 increments from 8 threads, so
        removing the lock can leave that test green.  Mutation testing caught
        exactly that (M5 went green when it should have gone red).

        So assert the lock is actually entered, by swapping in a spy.
        """
        counter = DUMP.DumpCounter()
        entered = []

        class SpyLock:
            def __enter__(self):
                entered.append(True)
                return self

            def __exit__(self, *exc):
                return False

        counter._lock = SpyLock()
        counter.next()
        self.assertEqual(len(entered), 1, "next() must hold the lock across the increment")
        counter.next()
        self.assertEqual(len(entered), 2)

    def test_increment_is_atomic_under_concurrency(self):
        """The lock lives here because the old code held _dump_lock across
        the increment AND the read; splitting them would allow duplicate
        filenames."""
        import threading
        counter = DUMP.DumpCounter()
        seen = []
        lock = threading.Lock()

        def worker():
            for _ in range(200):
                value = counter.next()
                with lock:
                    seen.append(value)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(seen), list(range(1, 1601)))


class DumpHttpTests(unittest.TestCase):
    def test_writes_nothing_when_the_switch_is_off_and_not_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={}, force=False)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_writes_when_the_switch_is_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={"X_DUMP": "1"})
            written = list(Path(tmp).iterdir())
        self.assertEqual(len(written), 1)

    def test_force_writes_even_when_the_switch_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={}, force=True)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_reads_the_injected_switch_name_only(self):
        """The whole reason dump_env is injected: ideal reads IDEAL_DUMP and
        twint reads TWINT_DUMP.  Neither may answer to the other's switch."""
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={"OTHER_DUMP": "1"}, force=False)
            self.assertEqual(list(Path(tmp).iterdir()), [],
                             "must not honour a different provider's switch")

    def test_filename_is_timestamp_index_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={"X_DUMP": "1"}, stage="checkout",
                  strftime=lambda fmt: "20260101-000000")
            name = list(Path(tmp).iterdir())[0].name
        self.assertEqual(name, "20260101-000000_0001_checkout.txt")

    def test_index_is_zero_padded_to_four_digits(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter = DUMP.DumpCounter()
            for _ in range(12):
                _call(Path(tmp), env={"X_DUMP": "1"}, stage="s", counter=counter)
            names = sorted(p.name for p in Path(tmp).iterdir())
        self.assertEqual(names[0].endswith("_0001_s.txt"), True)
        self.assertEqual(names[-1].endswith("_0012_s.txt"), True)

    def test_stage_is_sanitised_but_dots_and_dashes_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={"X_DUMP": "1"}, stage="a/b c.d-e")
            name = list(Path(tmp).iterdir())[0].name
        self.assertTrue(name.endswith("_0001_a_b_c.d-e.txt"), name)

    def test_stage_containing_traversal_is_neutralised(self):
        """`..` in a stage must not escape the dump directory."""
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={"X_DUMP": "1"}, stage="../../etc/passwd")
            paths = list(Path(tmp).iterdir())
        self.assertTrue(paths, "expected a dump file")

    def test_layout_with_a_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={"X_DUMP": "1"}, body={"a": 1},
                  response=FakeResponse(200, "https://e.test", "resp-body"))
            text = list(Path(tmp).iterdir())[0].read_text(encoding="utf-8")
        self.assertIn("stage: checkout", text)
        self.assertIn("request: POST https://example.test/x", text)
        self.assertIn("request_body:", text)
        self.assertIn("status: 200", text)
        self.assertIn("url: https://e.test", text)
        self.assertIn("response:\nresp-body", text)

    def test_body_is_json_dumped_with_indent_and_no_ascii_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={"X_DUMP": "1"}, body={"k": "值"})
            text = list(Path(tmp).iterdir())[0].read_text(encoding="utf-8")
        self.assertIn(json.dumps({"k": "值"}, ensure_ascii=False, indent=2), text)

    def test_missing_body_is_written_as_empty_not_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={"X_DUMP": "1"}, body=None)
            text = list(Path(tmp).iterdir())[0].read_text(encoding="utf-8")
        self.assertIn("request_body:\n\n", text)
        self.assertNotIn("None", text.split("request_body:")[1].split("status")[0])

    def test_no_response_means_no_status_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={"X_DUMP": "1"}, response=None)
            text = list(Path(tmp).iterdir())[0].read_text(encoding="utf-8")
        self.assertNotIn("status:", text)
        self.assertNotIn("response:", text)

    def test_redaction_is_applied_to_both_body_and_response(self):
        """Redaction is per-extractor, so it must be the injected callable."""
        calls = []

        def redact(text):
            calls.append(text)
            return text.replace("sk_live_1", "[REDACTED]")

        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={"X_DUMP": "1"}, body={"k": "sk_live_1"},
                  response=FakeResponse(200, "https://e.test", "sk_live_1"),
                  redact=redact)
            text = list(Path(tmp).iterdir())[0].read_text(encoding="utf-8")
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("sk_live_1", text)
        self.assertEqual(len(calls), 2, "both body and response must be redacted")

    def test_counter_does_not_advance_when_nothing_is_written(self):
        """Guards against incrementing before the switch is checked."""
        counter = DUMP.DumpCounter()
        with tempfile.TemporaryDirectory() as tmp:
            _call(Path(tmp), env={}, force=False, counter=counter)
            self.assertEqual(counter.value, 0)
            _call(Path(tmp), env={"X_DUMP": "1"}, counter=counter)
        self.assertEqual(counter.value, 1)


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
        self.assertEqual(tops, {"__future__", "json", "re", "time", "pathlib",
                                "threading", "typing"})

    def test_exports_exactly_two_names(self):
        self.assertEqual(sorted(DUMP.__all__), ["DumpCounter", "dump_http"])

    def test_dump_env_has_no_default(self):
        """A default would pick one provider's switch for both.

        Note ``co_argcount`` only counts POSITIONAL parameters, so keyword-only
        ones like ``dump_env`` are invisible to it -- an earlier version of this
        test used it and therefore asserted nothing at all.  Zip
        ``kwonlyargs``/``kw_defaults`` instead (``kw_defaults`` is padded at the
        front with ``None`` for the args that have no default).
        """
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "dump_http")
        names = [a.arg for a in node.args.kwonlyargs]
        self.assertIn("dump_env", names)
        idx = names.index("dump_env")
        defaults = node.args.kw_defaults
        # kw_defaults is aligned to kwonlyargs but front-padded with None
        pad = len(names) - len(defaults)
        self.assertIsNone(defaults[idx - pad], "dump_env must have no default")

    def test_strftime_default_is_not_captured_at_def_time(self):
        """Same trap as file_loading: a bound/early default is unpatchable."""
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "dump_http":
                for default in list(node.args.defaults) + list(node.args.kw_defaults):
                    self.assertNotIsInstance(default, ast.Attribute)


if __name__ == "__main__":
    unittest.main()
