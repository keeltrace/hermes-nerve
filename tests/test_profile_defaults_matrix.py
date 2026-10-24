"""Profile defaults audit (config-repair Phase 6).

Pins each named profile's canonical module set against its intended role,
cost and safety characteristics, and asserts the invariants that must hold on
every profile. Audit conclusion (recorded 2026-10-23, revision a040a67 basis):
the existing canonical sets already reflect each profile's role — evidence and
completion modules are ON everywhere, no profile enables dev-calibration or
opt-in-install modules by default, and the setup-dependent remote_workers
default for Fat Cat is pruned at resolution time ("no remote_hosts
configured") with the reason surfaced in UI and CLI. No defaults were changed;
this test locks the matrix so future edits must justify themselves here.

Profile roles as implemented:
- operator: interactive safety and context (action gate, context governor,
  local learning); no background supervision loop.
- lean: work supervision with token efficiency and low overhead; no reflex
  probing, no interactive gating, no assistant loops.
- fat_cat: broadest supervisory surface; only shadow_testing (dev
  calibration) and shared_context (opt-in install) stay off.
- marie_kondo: minimum Nerve; reflex probing plus supervision that clearly
  earns its cost; no nervous runtime or interactive gating.
- custom / legacy: base maps are all-OFF; the resolver layers existing
  configuration flags (and, for custom, the newer default-on modules).
"""
import unittest

from hermes_nerve.modules import MODULES
from hermes_nerve.profiles import PROFILES

EVIDENCE_INVARIANTS = ("receipts", "verified_experience")

EXPECTED_MATRIX = {
    "operator": {
        "reflex", "nervous", "action_gate", "context_governor", "receipts",
        "local_learning", "verified_experience", "selective_context",
        "selective_context_memory", "selective_context_tools",
        "selective_context_skills",
    },
    "lean": {
        "reflex", "nervous", "work_supervision", "token_trajectory",
        "receipts", "verified_experience", "selective_context",
        "selective_context_memory", "selective_context_tools",
        "selective_context_skills",
    },
    "fat_cat": {
        "reflex", "nervous", "work_supervision", "token_trajectory",
        "action_gate", "context_governor", "remote_workers",
        "assistant_loops", "assistant_audit", "receipts", "local_learning",
        "verified_experience", "selective_context",
        "selective_context_memory", "selective_context_tools",
        "selective_context_skills",
    },
    "marie_kondo": {
        "reflex", "work_supervision", "token_trajectory", "receipts",
        "verified_experience", "selective_context",
        "selective_context_memory", "selective_context_tools",
        "selective_context_skills",
    },
    "custom": set(),
    "legacy": set(),
}


class ProfileDefaultsMatrixTests(unittest.TestCase):
    def test_matrix_matches_canonical_sets(self):
        for name, expected in EXPECTED_MATRIX.items():
            actual = {k for k, on in PROFILES[name].items() if on}
            self.assertEqual(actual, expected, name)

    def test_completion_evidence_on_every_named_profile(self):
        for name in ("operator", "lean", "fat_cat", "marie_kondo"):
            for mid in EVIDENCE_INVARIANTS:
                self.assertTrue(PROFILES[name][mid], f"{name}.{mid}")

    def test_no_dev_or_optin_module_on_by_default(self):
        for name in ("operator", "lean", "fat_cat", "marie_kondo"):
            self.assertFalse(PROFILES[name]["shadow_testing"], f"{name}.shadow_testing")
            self.assertFalse(PROFILES[name]["shared_context"], f"{name}.shared_context")

    def test_remote_workers_default_only_on_fat_cat(self):
        for name in ("operator", "lean", "marie_kondo"):
            self.assertFalse(PROFILES[name]["remote_workers"], f"{name}.remote_workers")
        self.assertTrue(PROFILES["fat_cat"]["remote_workers"])

    def test_matrix_covers_registry_without_extras(self):
        for name, expected in EXPECTED_MATRIX.items():
            self.assertEqual(set(PROFILES[name]), set(MODULES), name)
            self.assertEqual({k for k, on in PROFILES[name].items() if on}, expected, name)


if __name__ == "__main__":
    unittest.main()
