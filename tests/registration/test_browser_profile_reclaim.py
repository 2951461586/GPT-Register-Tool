"""Finding the process that still holds a browser profile (2026-09-22).

Why this exists
---------------
Profiles are persistent and per-account
(``runtime/browser_profiles/camoufox/<email>/``). A browser that outlives its
parent keeps the profile open, and the next launch on that directory fails with a
message that never names the holder. The operator sees a dead profile and no
cause.

The load-bearing decision here is **what counts as evidence**. Every Camoufox
profile carries ``parent.lock`` -- created at startup and normally left behind,
so its presence proves nothing (measured 2026-09-22: all four profiles under
``runtime/browser_profiles/camoufox/`` have one while no browser runs). The only
portable signal is a live process whose command line references the directory,
which is why the tests below are all about the process lookup and never about
lock files.

The second decision is that **termination is opt-in**. ``reclaim_stale_profile``
reports by default; even when asked to terminate it only touches processes whose
image looks like a browser, so a backup tool or a grep that merely mentions the
path is listed and left alone.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from sms_tool import browser_profile_reclaim as reclaim


def _processes(*rows):
    """``rows`` are ``(pid, image, cmdline)``."""
    return lambda: list(rows)


class ImageClassificationTests(unittest.TestCase):
    def test_browser_images_are_recognized(self):
        for image in ("camoufox.exe", "firefox", "chrome.exe", "msedge.exe",
                      "playwright.cmd", "node.exe", "CamouFox.exe"):
            with self.subTest(image=image):
                self.assertTrue(reclaim.is_browser_image(image))

    def test_unrelated_images_are_not(self):
        for image in ("python.exe", "explorer.exe", "backup-agent", "grep", ""):
            with self.subTest(image=image):
                self.assertFalse(reclaim.is_browser_image(image))


class HolderLookupTests(unittest.TestCase):
    def test_a_process_referencing_the_profile_is_a_holder(self):
        holders = reclaim.profile_holders(
            "/profiles/camoufox/box@example.com",
            enumerate_processes=_processes(
                (11, "camoufox.exe", "-profile /profiles/camoufox/box@example.com -headless"),
                (12, "python.exe", "-c pass"),
            ),
        )
        self.assertEqual([h["pid"] for h in holders], [11])
        self.assertTrue(holders[0]["is_browser"])

    def test_the_chromium_flag_form_is_recognized(self):
        holders = reclaim.profile_holders(
            "/profiles/chromium/box",
            enumerate_processes=_processes(
                (13, "chrome.exe", "--user-data-dir=/profiles/chromium/box --headless"),
            ),
        )
        self.assertEqual([h["pid"] for h in holders], [13])

    def test_windows_backslashes_and_case_do_not_hide_a_holder(self):
        holders = reclaim.profile_holders(
            r"C:\repo\runtime\browser_profiles\camoufox\box@example.com",
            enumerate_processes=_processes(
                (21, "camoufox.exe",
                 r'-profile C:\repo\runtime\browser_profiles\camoufox\BOX@example.com -headless'),
            ),
        )
        self.assertEqual([h["pid"] for h in holders], [21])

    def test_a_trailing_separator_does_not_break_the_match(self):
        holders = reclaim.profile_holders(
            "/profiles/box/",
            enumerate_processes=_processes((31, "firefox", "-profile /profiles/box")),
        )
        self.assertEqual([h["pid"] for h in holders], [31])

    def test_a_sibling_profile_directory_is_not_a_holder(self):
        holders = reclaim.profile_holders(
            "/profiles/box@example.com",
            enumerate_processes=_processes(
                (41, "firefox", "-profile /profiles/box@example.com.bak"),
            ),
        )
        self.assertEqual(holders, [])

    def test_a_path_that_merely_appears_inside_another_argument_is_not_a_holder(self):
        """The false positive that made the first implementation useless.

        Measured 2026-09-22: a plain substring match flagged the *python* process
        running a ``-c`` script that happened to contain the profile path, so
        every diagnosis named an innocent process.
        """
        holders = reclaim.profile_holders(
            "/profiles/box",
            enumerate_processes=_processes(
                (42, "python.exe", 'python -c "print(\'/profiles/box\')"'),
                (43, "grep", "grep -r something /profiles/box/cache2"),
            ),
        )
        self.assertEqual(holders, [])

    def test_an_empty_target_matches_nothing(self):
        self.assertEqual(
            reclaim.profile_holders("", enumerate_processes=_processes((51, "firefox", "anything"))),
            [],
        )

    def test_a_process_without_a_command_line_is_ignored(self):
        holders = reclaim.profile_holders(
            "/profiles/box",
            enumerate_processes=_processes((61, "firefox", "")),
        )
        self.assertEqual(holders, [])


class DescriptionTests(unittest.TestCase):
    def test_no_holder_describes_as_empty_string(self):
        self.assertEqual(
            reclaim.describe_contention("/profiles/box", enumerate_processes=_processes()),
            "",
        )

    def test_the_description_names_the_pids_and_images(self):
        text = reclaim.describe_contention(
            "/profiles/box",
            enumerate_processes=_processes((71, "camoufox.exe", "--profile /profiles/box")),
        )
        self.assertIn("PID 71", text)
        self.assertIn("camoufox.exe", text)
        self.assertIn("/profiles/box", text)

    def test_a_long_holder_list_is_truncated_but_counted(self):
        rows = [(100 + n, "camoufox.exe", "--profile /profiles/box") for n in range(7)]
        text = reclaim.describe_contention("/profiles/box", enumerate_processes=_processes(*rows))
        self.assertIn("and 3 more", text)


class ReclaimTests(unittest.TestCase):
    def setUp(self):
        self.rows = _processes(
            (81, "camoufox.exe", "--profile /profiles/box"),
            (82, "backup-agent", "tar /profiles/box"),
            (os.getpid(), "python.exe", "pytest /profiles/box"),
        )

    def test_reporting_is_the_default_and_kills_nothing(self):
        with mock.patch.object(reclaim, "_terminate") as killer:
            report = reclaim.reclaim_stale_profile("/profiles/box", enumerate_processes=self.rows)
        killer.assert_not_called()
        self.assertEqual(report["terminated"], [])
        self.assertEqual(len(report["holders"]), 3)

    def test_termination_skips_non_browser_images_and_ourselves(self):
        with mock.patch.object(reclaim, "_terminate", return_value=True) as killer:
            report = reclaim.reclaim_stale_profile(
                "/profiles/box", terminate=True, enumerate_processes=self.rows
            )
        killer.assert_called_once_with(81)
        self.assertEqual(report["terminated"], [81])
        self.assertCountEqual(report["skipped"], [82, os.getpid()])

    def test_a_failed_termination_is_reported_as_skipped_not_terminated(self):
        with mock.patch.object(reclaim, "_terminate", return_value=False):
            report = reclaim.reclaim_stale_profile(
                "/profiles/box", terminate=True, enumerate_processes=self.rows
            )
        self.assertEqual(report["terminated"], [])
        self.assertIn(81, report["skipped"])


class EnumeratorSmokeTests(unittest.TestCase):
    def test_the_real_enumerator_yields_well_formed_rows(self):
        """The lookup is best-effort: an unavailable process list is empty, not fatal.

        Asserted loosely on purpose -- in a sandbox the enumeration legitimately
        returns nothing, and a test that demanded rows would fail there for a
        reason that has nothing to do with this module.
        """
        for row in reclaim.iter_processes():
            pid, image, cmdline = row
            self.assertIsInstance(pid, int)
            self.assertIsInstance(image, str)
            self.assertIsInstance(cmdline, str)


class WindowsDecodeTests(unittest.TestCase):
    """Regression: non-UTF-8 PowerShell output silently emptied the process list.

    ``subprocess.run(text=True)`` decodes with the console code page. On this
    machine a GBK path in a command line raised ``UnicodeDecodeError`` inside the
    reader thread, which ``subprocess`` does not re-raise -- ``stdout`` was simply
    empty, so the lookup answered "no holders" on a machine that had them. The
    smoke test above is what surfaced it.
    """

    def _run_with(self, stdout: bytes, *, returncode: int = 0):
        proc = mock.Mock(returncode=returncode, stdout=stdout, stderr=b"")
        return mock.patch.object(reclaim.subprocess, "run", return_value=proc)

    def test_a_gbk_path_in_the_process_list_does_not_empty_the_lookup(self):
        payload = json.dumps(
            [{"ProcessId": 91, "Name": "camoufox.exe",
              "CommandLine": r"--profile C:\用户\box"}]
        ).encode("utf-8")
        with self._run_with(payload):
            rows = list(reclaim._iter_processes_windows())
        self.assertEqual([row[0] for row in rows], [91])

    def test_undecodable_bytes_are_replaced_not_fatal(self):
        payload = b'[{"ProcessId": 92, "Name": "firefox.exe", "CommandLine": "--profile \xcc\xd8"}]'
        with self._run_with(payload):
            rows = list(reclaim._iter_processes_windows())
        self.assertEqual([row[0] for row in rows], [92], "a bad byte must not drop the row")

    def test_garbage_output_is_reported_and_yields_nothing(self):
        with self._run_with(b"not json at all"):
            self.assertEqual(list(reclaim._iter_processes_windows()), [])

    def test_a_single_object_response_is_wrapped_into_a_list(self):
        payload = b'{"ProcessId": 93, "Name": "chrome.exe", "CommandLine": "--profile /p"}'
        with self._run_with(payload):
            rows = list(reclaim._iter_processes_windows())
        self.assertEqual([row[0] for row in rows], [93])

    def test_a_nonzero_exit_yields_nothing(self):
        with self._run_with(b"", returncode=1):
            self.assertEqual(list(reclaim._iter_processes_windows()), [])

    def test_a_missing_powershell_is_not_fatal(self):
        with mock.patch.object(reclaim.subprocess, "run", side_effect=OSError("no powershell")):
            self.assertEqual(list(reclaim._iter_processes_windows()), [])


if __name__ == "__main__":
    unittest.main()
