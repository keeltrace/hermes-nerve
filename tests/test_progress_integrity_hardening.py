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

    def test_affinity_and_niceness_wrapped_real_test_is_recognized(self):
        command = ('taskset -c 0 nice -n 15 python -B -m unittest '
                   'tests.test_progress_integrity_hardening -v')
        self.assertEqual(P.tool_kind('terminal', {'command': command}), 'test')

    def test_resource_wrapper_composition_preserves_runner_identity(self):
        commands = (
            'timeout --signal=TERM --kill-after=2s 25s taskset -c 0 nice -n 15 python -B -m pytest -q tests/test_parser.py',
            '/usr/bin/taskset --cpu-list 0,2-4:2 /usr/bin/nice -n +15 /venv/bin/python3.11 -m unittest tests.test_parser',
            'nice -n 15 timeout -k 2s 25s taskset -c 0 pytest -q tests/test_parser.py',
            'set -o pipefail; taskset -c 0 nice -n 15 python -B -m unittest tests.test_parser -v 2>&1 | tee run.log',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(P.tool_kind('terminal', {'command': command}), 'test')

    def test_resource_wrappers_never_turn_printed_runner_names_into_tests(self):
        commands = (
            'nice -n 15 echo pytest',
            'taskset -c 0 nice -n 15 echo "python -m pytest"',
            'taskset -c 0 python -c "print(\'1 passed; pytest\')"',
            'timeout 25s taskset -c 0 echo pytest',
            'taskset -p 123 pytest', 'taskset -pc 0 123 pytest',
            'taskset --help pytest', 'taskset -c invalid pytest',
            'taskset -c 0', 'taskset -c pytest', 'taskset -c -1 pytest',
            'nice --help pytest', 'nice -n nope pytest', 'nice -n 15',
            'nice -n pytest', 'nice -n 15 python -m pytest_not_a_runner',
            'taskset -c 0 pyfake -m pytest',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertNotEqual(P.tool_kind('terminal', {'command': command}), 'test')

    def test_relative_decoy_path_is_not_relevant(self):
        self.assertFalse(P.changed_paths_relevant(["evil/app/calc.py"], ("app/calc.py",)))
        self.assertTrue(P.changed_paths_relevant(["/tmp/sb/app/calc.py"], ("app/calc.py",)))


if __name__ == "__main__":
    unittest.main()
