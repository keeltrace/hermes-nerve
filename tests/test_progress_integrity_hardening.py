import unittest

from hermes_nerve import progress_integrity as P


class AdversarialHardeningTests(unittest.TestCase):
    def test_zero_tests_is_not_a_pass(self):
        for out in ("no tests ran", "collected 0 items", "Ran 0 tests\nOK"):
            self.assertFalse(P.test_succeeded("ok", out), out)

    def test_hermes_terminal_json_envelope(self):
        ok = '{"output": "1 passed in 0.01s", "exit_code": 0, "error": null}'
        self.assertTrue(P.test_succeeded("ok", ok))
        self.assertFalse(P.test_succeeded("ok", '{"output": "1 failed", "exit_code": 1, "error": null}'))
        self.assertFalse(P.test_succeeded("ok", '{"output": "no tests ran", "exit_code": 0, "error": null}'))

    def test_zero_failed_is_a_pass(self):
        self.assertTrue(P.test_succeeded("ok", "1 passed, 0 failed"))

    def test_direct_test_file_run_counts_as_test(self):
        self.assertEqual(P.tool_kind("terminal", {"command": "python app/test_calc.py"}), "test")
        self.assertEqual(P.tool_kind("terminal", {"command": "echo 1 passed"}), "")
        cmd = "PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .test-tools/venv/bin/python -m pytest -q app/test_calc.py"
        self.assertEqual(P.tool_kind("terminal", {"command": cmd}), "test")
        self.assertEqual(P.tool_kind("terminal", {"command": ".test-tools/venv/bin/pytest -q app/test_calc.py"}), "test")
        self.assertEqual(P.tool_kind("terminal", {"command": '.venv/bin/python -B -m pytest -q app/test_calc.py --rootdir="$PWD"'}), "test")

    def test_relative_decoy_path_is_not_relevant(self):
        self.assertFalse(P.changed_paths_relevant(["evil/app/calc.py"], ("app/calc.py",)))
        self.assertTrue(P.changed_paths_relevant(["/tmp/sb/app/calc.py"], ("app/calc.py",)))


if __name__ == "__main__":
    unittest.main()
