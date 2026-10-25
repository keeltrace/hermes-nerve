"""Adversarial fixtures for test-command recognition and verified-pass detection.

Each case is a terminal command plus the tool result Hermes would hand back.
``is_test`` asks whether the command counts as a test run; ``succeeded`` asks
whether that run proves the tests passed (JSON envelopes, launcher prefixes,
shell wrappers, "error" words inside passing test names, and ``|| true``
masking are all covered).
"""
import unittest

from hermes_nerve import progress_integrity as pi


CASES = [{'case': 'npm test success',
  'command': 'npm test',
  'status': 'ok',
  'result': '{"output":"1 passing","exit_code":0,"error":null}',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'yarn test success',
  'command': 'yarn test',
  'status': 'success',
  'result': '2 passed',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'pnpm test success',
  'command': 'pnpm test',
  'status': 'passed',
  'result': 'Tests: 4 passed',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'go test ./... with go ok lines',
  'command': 'go test ./...',
  'status': 'ok',
  'result': 'ok\t./cmd/app\t0.012s\nok\t./internal/pkg\t0.008s',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'cargo test result ok',
  'command': 'cargo test',
  'status': 'ok',
  'result': 'test result: ok. 5 passed; 0 failed; 0 ignored',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'make test tox nox chain',
  'command': 'make test && tox && nox',
  'status': 'ok',
  'result': 'make: test target passed\n4 passed\n7 passed',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'bun jest vitest mocha chain',
  'command': 'bun test && jest && vitest && mocha',
  'status': 'ok',
  'result': 'Tests: 4 passed, 4 total',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'python -m unittest module',
  'command': 'python -m unittest tests.test_app',
  'status': 'ok',
  'result': '{"output":"Ran 3 tests in 0.01s\\n\\nOK","exit_code":0,"error":null}',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'pytest script wrapper',
  'command': './scripts/test.sh',
  'status': 'ok',
  'result': '================ 8 passed in 0.31s ================',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'pytest passing test named error',
  'command': 'pytest -q',
  'status': 'ok',
  'result': 'tests/test_error_handling.py::test_error_handling PASSED\n1 passed in 0.02s',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'pytest warnings summary with pass',
  'command': 'pytest -q',
  'status': 'ok',
  'result': '================ warnings summary ================\n1 passed, 2 warnings in 0.04s',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'jest Tests passed summary',
  'command': 'jest',
  'status': 'ok',
  'result': 'Tests: 3 passed, 3 total\nSnapshots: 0 total',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'pytest FAIL lines',
  'command': 'pytest -q',
  'status': 'ok',
  'result': 'FAILED tests/test_calc.py::test_add - AssertionError\n1 failed, 2 passed',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': False}},
 {'case': 'pytest exit 1 despite passed text',
  'command': 'pytest -q',
  'status': 'ok',
  'result': '{"output":"3 passed","exit_code":1,"error":null}',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': False}},
 {'case': 'pytest collected zero',
  'command': 'pytest -q',
  'status': 'ok',
  'result': '{"output":"collected 0 items\\n\\nno tests ran","exit_code":0,"error":null}',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': False}},
 {'case': 'Windows pytest executable path',
  'command': 'C:\\repo\\Scripts\\pytest.exe -q',
  'status': 'ok',
  'result': '1 passed in 0.02s',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'pytest from subdirectory',
  'command': 'cd sub && pytest -q',
  'status': 'ok',
  'result': '1 passed in 0.01s',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'bash wrapper around pytest',
  'command': 'bash -c "pytest -q"',
  'status': 'ok',
  'result': '1 passed in 0.01s',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}},
 {'case': 'pytest masked by shell true',
  'command': 'python -m pytest -q || true',
  'status': 'ok',
  'result': '{"output":"","exit_code":0,"error":null}',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': False}},
 {'case': 'jest passing ErrorHandler name',
  'command': 'jest',
  'status': 'ok',
  'result': 'ErrorHandler.test.js ✓\nTests: 1 passed, 1 total',
  'error_message': '',
  'expected': {'is_test': True, 'succeeded': True}}]


class TestRunnerRecognitionTests(unittest.TestCase):
    def test_fixtures(self):
        for case in CASES:
            with self.subTest(case=case["case"]):
                is_test = pi.tool_kind("terminal", {"command": case["command"]}) == "test"
                ok = (
                    is_test
                    and pi.test_succeeded(case["status"], case["result"], case["error_message"])
                    and not pi.test_result_masked(case["command"])
                )
                self.assertEqual(is_test, case["expected"]["is_test"], "is_test")
                self.assertEqual(ok, case["expected"]["succeeded"], "succeeded")

    def test_go_fail_line_is_failure(self):
        self.assertFalse(pi.test_succeeded("ok", "--- FAIL: TestX\nFAIL\texample 0.01s"))

    def test_warnings_summary_passes(self):
        self.assertTrue(pi.test_succeeded("ok", "3 passed, 2 warnings in 0.10s"))


if __name__ == "__main__":
    unittest.main()
