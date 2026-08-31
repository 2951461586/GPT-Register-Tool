import unittest
from argparse import ArgumentParser

from sms_tool import cli


class RegistrationDriverCliChoicesTests(unittest.TestCase):
    def _driver_choices(self):
        # Read the real choices straight off the production parser so the test
        # cannot silently drift if the help string is edited.
        parser = cli.build_parser()
        action = parser._option_string_actions["--registration-driver"]
        return list(action.choices)

    def test_adspower_is_a_known_registration_driver(self):
        choices = self._driver_choices()
        self.assertIn("adspower", choices)

    def test_all_expected_drivers_are_present(self):
        choices = self._driver_choices()
        for expected in ["protocol", "playwright", "roxy", "cloak", "camoufox", "adspower"]:
            self.assertIn(expected, choices)

    def test_adspower_parses_without_error(self):
        parser = cli.build_parser()
        args = parser.parse_args(["--registration-driver", "adspower"])
        self.assertEqual(args.registration_driver, "adspower")

    def test_unknown_driver_is_rejected(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--registration-driver", "not-a-driver"])


if __name__ == "__main__":
    unittest.main()
