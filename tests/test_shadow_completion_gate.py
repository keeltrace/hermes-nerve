import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import nervous
from tests.test_nervous import ScriptedEngine


class ShadowCompletionGateTests(unittest.TestCase):
    def run_gate(self, mode):
        td = tempfile.mkdtemp()
        env = patch.dict(os.environ, {"HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "n.jsonl")}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        system = nervous.NervousSystem(engine_factory=ScriptedEngine)
        system.configure(enabled=True, admission_enabled=False, mode=mode)
        system.start_turn(user_message="Fix the bug in app/calc.py and run the tests to prove it", session_id="s1", turn_id="t1")
        out = system.completion_gate(turn_id="t1", coding=True)
        return out, (Path(td) / "n.jsonl").read_text()

    def test_correct_next_blocks_unverified_completion(self):
        out, _ = self.run_gate("correct_next")
        self.assertEqual(out["action"], "continue")

    def test_shadow_logs_but_does_not_block(self):
        out, log = self.run_gate("shadow")
        self.assertIsNone(out)
        self.assertIn("progress_completion_blocked", log)
        self.assertIn('"shadow": true', log)


if __name__ == "__main__":
    unittest.main()
