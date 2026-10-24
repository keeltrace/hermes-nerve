"""Enabled Selective Context must APPLY through the real Hermes consumers.

These tests cover profile resolution, per-surface switches, real candidate
selection and a scoped Verified Experience hook. No Hermes gateway or network
service is needed for these deterministic contracts.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import consumer, experience
from hermes_nerve.profiles import save_profile


def _config(*, profile="operator", modules=None, **settings):
    cfg = {"nerve_profile": profile, **settings}
    if modules is not None:
        cfg["nerve_modules"] = modules
    return {"plugins": {"entries": {"nerve": {"settings": cfg}}}}


class SelectiveContextActivationTests(unittest.TestCase):
    def test_enabled_named_profile_applies_all_three_surfaces(self):
        with tempfile.TemporaryDirectory() as home:
            effective = consumer.settings_from_config(_config(), home=home)
            self.assertTrue(effective["selective_context_enabled"])
            for kind in ("memory", "tools", "skills"):
                self.assertEqual(effective[f"selective_context_{kind}_mode"], "apply")

    def test_named_sidecar_activates_without_explicit_hermes_settings_entry(self):
        with tempfile.TemporaryDirectory() as home:
            save_profile({"version": 1, "nerve_profile": "operator",
                          "nerve_modules": {}, "advanced": {}}, home)
            effective = consumer.settings_from_config({}, home=home)
            self.assertTrue(effective["selective_context_enabled"])
            self.assertEqual(effective["selective_context_memory_mode"], "apply")
            self.assertEqual(effective["selective_context_tools_mode"], "apply")
            self.assertEqual(effective["selective_context_skills_mode"], "apply")

    def test_unconfigured_legacy_keeps_stock_context(self):
        with tempfile.TemporaryDirectory() as home:
            effective = consumer.settings_from_config(_config(profile=""), home=home)
            self.assertFalse(effective["selective_context_enabled"])
            for kind in ("memory", "tools", "skills"):
                self.assertEqual(effective[f"selective_context_{kind}_mode"], "off")
        self.assertFalse(consumer.settings_from_config({})["selective_context_enabled"])

    def test_explicit_parent_disable_turns_every_surface_off(self):
        with tempfile.TemporaryDirectory() as home:
            effective = consumer.settings_from_config(
                _config(selective_context_enabled=False), home=home)
            self.assertFalse(effective["selective_context_enabled"])
            self.assertEqual([effective[f"selective_context_{k}_mode"]
                              for k in ("memory", "tools", "skills")], ["off"] * 3)

    def test_explicit_module_off_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as home:
            effective = consumer.settings_from_config(
                _config(modules={"selective_context_skills": False}), home=home)
            self.assertEqual(effective["selective_context_skills_mode"], "off")
            self.assertEqual(effective["selective_context_tools_mode"], "apply")
            self.assertEqual(effective["selective_context_memory_mode"], "apply")

    def test_explicit_shadow_preserved_and_sidecar_wins_profile(self):
        with tempfile.TemporaryDirectory() as home:
            save_profile({"version": 1, "nerve_profile": "operator",
                          "nerve_modules": {},
                          "advanced": {"selective_context_skills_mode": "shadow"}}, home)
            effective = consumer.settings_from_config(_config(profile="lean"), home=home)
            self.assertTrue(effective["selective_context_enabled"])
            self.assertEqual(effective["selective_context_skills_mode"], "shadow")
            self.assertEqual(effective["selective_context_tools_mode"], "apply")

    def test_tool_and_skill_consumers_change_the_selection_when_on(self):
        with tempfile.TemporaryDirectory() as home:
            settings = consumer.settings_from_config(
                _config(selective_context_tools_max_admitted=1,
                        selective_context_skills_max_admitted=1), home=home)
            definitions = [
                {"function": {"name": "github_pr_review",
                              "description": "Review GitHub pull request code diff",
                              "parameters": {}}},
                {"function": {"name": "gmail_messages",
                              "description": "Read messages and emails",
                              "parameters": {}}},
            ]
            receipt = consumer.select_tools_context(
                definitions, "Review GitHub pull request", settings)
            self.assertEqual(receipt["mode"], "apply")
            self.assertFalse(receipt["fail_open"])
            self.assertIn("github_pr_review", receipt["effective_selected_ids"])
            index = ("<available_skills>\n"
                     "    - github-review: Review GitHub pull request code\n"
                     "    - recipes: Cook dinner from recipes\n"
                     "</available_skills>")
            result = consumer.select_skills_index(index, "Review GitHub pull request", settings)
            self.assertIn("github-review: Review GitHub", result)
            self.assertNotIn("recipes: Cook dinner", result)
            self.assertIn("    - recipes\n", result)

    def test_verified_experience_produces_scoped_live_memory(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(
                os.environ, {"HERMES_HOME": home}):
            store = experience.ExperienceStore(Path(home) / "memory.jsonl")
            claim = store.create_claim(
                subject="API parser repair",
                statement="API parser regression resolved by normalized header matching",
                claim_type="verified", evidence_ids=("test-parser-green",),
                confidence=0.99, scope={"user": "alice"}, admit=False)
            self.assertTrue(claim["admitted"] or claim.get("claim"))
            effective = consumer.settings_from_config(_config(), home=home)
            experience.configure(enabled=True)
            try:
                with patch.object(experience, "default_store", return_value=store):
                    result = consumer.experience_context_hook(
                        session_id="session-1", sender_id="alice",
                        user_message="Fix API parser regression")
                    self.assertIsInstance(result, dict)
                    envelope = result["context"]
                    text, receipt = consumer.select_memory_context(
                        envelope, "Fix API parser regression", effective, envelope["scope"])
                    self.assertEqual(receipt["mode"], "apply")
                    self.assertIn("normalized header matching", text)
                    self.assertEqual(len(receipt["selected_ids"]), 1)
                    # A different user may not retrieve this user's scoped claim.
                    self.assertIsNone(consumer.experience_context_hook(
                        session_id="session-2", sender_id="bob",
                        user_message="Fix API parser regression"))
                    with self.assertRaises(ValueError):
                        consumer.select_memory_context(
                            envelope, "Fix API parser regression", effective,
                            {"home": home, "session": "another-session", "user": "alice"})
            finally:
                experience.configure(enabled=False)


if __name__ == "__main__":
    unittest.main()