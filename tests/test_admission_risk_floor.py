import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import nervous
from tests.test_nervous import FakeDecisionResult, ScriptedEngine


class AdmissionRiskFloorTests(unittest.TestCase):
    def make_system(self, td, **cfg):
        system = nervous.NervousSystem(engine_factory=ScriptedEngine)
        system.configure(enabled=True, admission_enabled=True, mode="shadow", **cfg)
        env = patch.dict(os.environ, {"HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "n.jsonl")}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        return system

    def admit(self, system, prompt):
        system.start_turn(user_message=prompt, session_id="s1", turn_id="t1")
        system._turns["t1"].admission = "PENDING"  # admission disabled at start; drive it directly
        try:
            system._do_admission({"turn_id": "t1", "user_message": prompt})
        except Exception:
            pass
        return system._turns["t1"].admission

    def test_floor_patterns(self):
        risky = [
            "Run deploy.sh to production",
            "Delete old backups but keep two",
            "Rotate the API key in app/fake.env",
            "Plan the database migration",
            "git push --force to main",
        ]
        benign = ["What is the capital of France?", "Say hello", "Summarize this file"]
        for p in risky:
            self.assertEqual(nervous.admission_risk_floor(p), "WATCH", p)
        for p in benign:
            self.assertEqual(nervous.admission_risk_floor(p), "", p)

    def test_floor_raises_off_to_watch_and_logs_provider_value(self):
        with tempfile.TemporaryDirectory() as td:
            system = self.make_system(td)
            system.configure(admission_enabled=False)
            ScriptedEngine.reset(FakeDecisionResult("OFF", 0.6))
            self.assertEqual(self.admit(system, "Deploy to production now"), "WATCH")
            log = (Path(td) / "n.jsonl").read_text()
            self.assertIn('"provider_admission": "OFF"', log)
            self.assertEqual(system.report()["metrics"]["admission_floor_raised"], 1)

    def test_floor_never_lowers_on(self):
        with tempfile.TemporaryDirectory() as td:
            system = self.make_system(td)
            system.configure(admission_enabled=False)
            ScriptedEngine.reset(FakeDecisionResult("ON", 0.9))
            self.assertEqual(self.admit(system, "delete the temp folder"), "ON")

    def test_benign_prompt_keeps_provider_off(self):
        with tempfile.TemporaryDirectory() as td:
            system = self.make_system(td)
            system.configure(admission_enabled=False)
            ScriptedEngine.reset(FakeDecisionResult("OFF", 0.99))
            self.assertEqual(self.admit(system, "What is 2+2?"), "OFF")

    def test_provider_failure_applies_floor_for_risky_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            system = self.make_system(td)
            system.configure(admission_enabled=False)
            ScriptedEngine.reset(RuntimeError("HTTP 402"))
            self.assertEqual(self.admit(system, "Run the production deploy"), "WATCH")
            self.assertIn("admission_floor_fallback", (Path(td) / "n.jsonl").read_text())

    def test_engine_construction_failure_applies_floor(self):
        with tempfile.TemporaryDirectory() as td:
            def broken_factory():
                raise RuntimeError("TYPESAFE_API_KEY is not configured")
            system = nervous.NervousSystem(engine_factory=broken_factory)
            system.configure(enabled=True, admission_enabled=False, mode="shadow")
            env = patch.dict(os.environ, {"HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "n.jsonl")}, clear=False)
            env.start()
            self.addCleanup(env.stop)
            self.assertEqual(self.admit(system, "delete the old backups"), "WATCH")

    def test_provider_failure_benign_prompt_stays_pending(self):
        with tempfile.TemporaryDirectory() as td:
            system = self.make_system(td)
            system.configure(admission_enabled=False)
            ScriptedEngine.reset(RuntimeError("timeout"))
            self.assertEqual(self.admit(system, "Say hello"), "PENDING")

    def test_floor_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            system = self.make_system(td, admission_risk_floor=False)
            system.configure(admission_enabled=False)
            ScriptedEngine.reset(FakeDecisionResult("OFF", 0.6))
            self.assertEqual(self.admit(system, "Deploy to production now"), "OFF")


if __name__ == "__main__":
    unittest.main()
