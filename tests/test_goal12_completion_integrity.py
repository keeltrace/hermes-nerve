import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_nerve.work.deterministic import auto_verify_completion
from hermes_nerve.work.models import CriterionSpec, DoDContract


class _Identity:
    task_id = "goal12"
    run_id = 1
    contract_hash = "goal12-contract"


class _Store:
    def __init__(self, workspace, verdicts, base_revision=""):
        self.workspace = workspace
        self.verdicts = verdicts
        self.base_revision = base_revision
        self._evidence = []

    def latest_contract_verdicts(self, task_id, contract_hash):
        return self.verdicts

    def run_context(self, identity):
        return {"workspace_path": self.workspace, "base_revision": self.base_revision}

    def evidence(self, identity, criterion_id=None, include_stale=True):
        if criterion_id is None:
            return list(self._evidence)
        return [row for row in self._evidence if row.get("criterion_id") == criterion_id]


class _Supervisor:
    def __init__(self, contract, store):
        self._contract = contract
        self.store = store

    def active_contract(self, task_id):
        return self._contract

    def observe_evidence(self, identity, *, value, kind, criterion_id="", source="", tool_name="", is_error=False):
        self.store._evidence.append({
            "evidence_id": str(len(self.store._evidence) + 1),
            "criterion_id": criterion_id,
            "kind": kind,
            "is_error": bool(is_error),
            "preview": __import__("json").dumps(value),
        })
        return self.store._evidence[-1]

    def mark_deterministic_verdict(self, identity, criterion_id, *, passed, reason, evidence_ids):
        self.store.verdicts[criterion_id] = {
            "state": "VERIFIED_PASS" if passed else "VERIFIED_FAIL",
            "reason": reason,
            "evidence_ids": list(evidence_ids),
        }


def _contract(description):
    return DoDContract(
        task_id="goal12",
        version=1,
        goal="prove completion against current state",
        criteria=(CriterionSpec(id="DOD-01", description=description, required=True),),
        reserve_tokens=100000,
        checkpoint_fractions=(0.5,),
        contract_hash="goal12-contract",
        locked_at="2026-09-30T00:00:00Z",
        actor="test",
    )


class Goal12CompletionIntegrityTests(unittest.TestCase):
    def test_previously_passed_file_criterion_is_rechecked(self):
        with tempfile.TemporaryDirectory() as td:
            store = _Store(td, {"DOD-01": {"state": "VERIFIED_PASS", "reason": "old pass"}})
            sup = _Supervisor(_contract("Required file `must-exist.txt` exists"), store)
            auto_verify_completion(sup, _Identity())
            self.assertEqual(store.verdicts["DOD-01"]["state"], "VERIFIED_FAIL")
            self.assertIn("missing", store.verdicts["DOD-01"]["reason"].lower())

    def test_previously_passed_test_criterion_is_rerun(self):
        with tempfile.TemporaryDirectory() as td:
            tests_dir = Path(td) / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(
                "import unittest\n\n"
                "class Sample(unittest.TestCase):\n"
                "    def test_now_fails(self):\n"
                "        self.fail('current state is broken')\n",
                encoding="utf-8",
            )
            store = _Store(td, {"DOD-01": {"state": "VERIFIED_PASS", "reason": "old green suite"}})
            sup = _Supervisor(
                _contract("Run `python3 -m unittest discover -s tests -p test_sample.py` and require green"),
                store,
            )
            auto_verify_completion(sup, _Identity())
            self.assertEqual(store.verdicts["DOD-01"]["state"], "VERIFIED_FAIL")
            checks = [row for row in store._evidence if row["kind"] == "deterministic_check"]
            self.assertEqual(len(checks), 1)
            self.assertTrue(checks[0]["is_error"])

    def test_previously_passed_clean_git_criterion_rejects_new_untracked_file(self):
        with tempfile.TemporaryDirectory() as td:
            def git(*args):
                subprocess.run(
                    ["git", *args], cwd=td, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
            git("init", "-q")
            git("config", "user.email", "goal12@example.invalid")
            git("config", "user.name", "Goal 12 Test")
            Path(td, "tracked.txt").write_text("base\n", encoding="utf-8")
            git("add", "tracked.txt")
            git("commit", "-qm", "base")
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=td, text=True).strip()
            Path(td, "tracked.txt").write_text("fixed\n", encoding="utf-8")
            git("add", "tracked.txt")
            git("commit", "-qm", "fix")
            Path(td, "scratch.txt").write_text("untracked\n", encoding="utf-8")
            store = _Store(
                td,
                {"DOD-01": {"state": "VERIFIED_PASS", "reason": "old clean status"}},
                base_revision=base,
            )
            sup = _Supervisor(_contract("Changes are committed and clean git status is required"), store)
            auto_verify_completion(sup, _Identity())
            self.assertEqual(store.verdicts["DOD-01"]["state"], "VERIFIED_FAIL")
            self.assertIn("not satisfied", store.verdicts["DOD-01"]["reason"].lower())


if __name__ == "__main__":
    unittest.main()
