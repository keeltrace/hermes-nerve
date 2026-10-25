"""Progress-guard classification regression tests.

The progress-stall guard blocks read-only tools once an implementation turn has
spent its investigation budget without a task-relative mutation. That is correct
for real implementation work and wrong for a turn that explicitly asks for
read-only verification: such a turn can never produce a mutation, so the guard is
unsatisfiable and turns a legitimate audit into a hard block.

Two defects are covered here:

1. ``change_required`` treated a change word as an instruction even when it was a
   noun ("...after the configuration update"), arming the guard on a read-only
   verification turn.
2. ``change_required`` ignored an explicit read-only declaration, so the guard
   still demanded mutations on turns that said "no configuration changes".

The guard itself must keep working: an implementation turn with no relevant
mutation must still arm the control and still block read-only tools.
"""
from __future__ import annotations

import unittest

from hermes_nerve.progress_integrity import (
    change_required,
    read_only_audit_message,
    verification_required,
)


class ChangeRequiredReadOnlyTests(unittest.TestCase):
    """Legitimate read-only verification must not arm the progress guard."""

    def test_original_false_positive_prompt_is_not_change_required(self):
        """The exact prompt that was blocked: 'update' here is a noun."""
        message = (
            "Verify that Nerve v0.4.11 is fully active in Operator mode after the "
            "configuration update."
        )
        self.assertFalse(change_required(message))
        self.assertTrue(read_only_audit_message(message) or not change_required(message))

    def test_explicit_read_only_declaration_is_not_change_required(self):
        message = (
            "READ-ONLY RUNTIME VERIFICATION of the Nerve Operator activation. "
            "No configuration changes, restarts, delegation, or new missions."
        )
        self.assertFalse(change_required(message))
        self.assertTrue(read_only_audit_message(message))

    def test_verification_turn_without_read_only_marker_is_not_change_required(self):
        message = (
            "Run one harmless tool-using smoke test and inspect fresh telemetry to "
            "prove Nerve actually applies decisions."
        )
        self.assertFalse(change_required(message))
        self.assertTrue(verification_required(message))

    def test_diagnostic_only_turn_is_not_change_required(self):
        message = (
            "Diagnostic only: investigate the five provider-unavailable events and "
            "determine the actual causes."
        )
        self.assertFalse(change_required(message))

    def test_prohibition_verbs_do_not_count_as_instructions(self):
        for message in (
            "Inspect the reflex client. Do not modify anything.",
            "Read-only review of the progress guard. Do not edit any file.",
            "Confirm the effective backend, without changing configuration.",
            "Audit the mission state. No delegation or restarts.",
        ):
            with self.subTest(message=message):
                self.assertFalse(change_required(message))

    def test_change_word_as_noun_is_not_an_instruction(self):
        """A determiner before the change word marks the noun reading."""
        for message in (
            "Confirm the update did not break the tunnel, then report status.",
            "Check the logs after the fix was applied yesterday.",
            "Review the results after my patch landed.",
            "Diff the file against the earlier edit.",
        ):
            with self.subTest(message=message):
                self.assertFalse(change_required(message))


class ChangeRequiredImplementationTests(unittest.TestCase):
    """Genuine implementation work must still arm the progress guard."""

    def test_imperative_change_verbs_still_arm_the_guard(self):
        for message in (
            "Fix the D1 reliability failures by implementing bounded retries.",
            "Update the Operator defaults so fresh installations use active behavior.",
            "Repair the progress guard so it stops blocking legitimate verification.",
            "Patch the systemone.py transport path to add a bounded retry.",
            "Edit the config to pin nerve_profile.",
            "Refactor the reflex client transport layer.",
            "Rewrite the progress guard logic.",
            "Modify systemone.py to add the retry.",
        ):
            with self.subTest(message=message):
                self.assertTrue(change_required(message))

    def test_implementation_after_prerequisite_reads_still_arms_the_guard(self):
        message = "Read the reflex client first, then implement bounded retries in systemone.py."
        self.assertTrue(change_required(message))

    def test_audit_then_implement_still_arms_the_guard(self):
        """A read-only phase does not exempt a turn that also changes code."""
        message = "Read-only audit first, then fix the progress guard afterwards."
        self.assertTrue(change_required(message))

    def test_implement_with_a_prohibition_still_arms_the_guard(self):
        message = "Implement the fix. Do not delegate this work."
        self.assertTrue(change_required(message))

    def test_implementation_without_any_read_only_marker_still_arms_the_guard(self):
        message = "Implement bounded retries in the reflex client and verify reliability."
        self.assertTrue(change_required(message))


class ReadOnlyAuditMessageTests(unittest.TestCase):
    def test_empty_message_is_not_a_read_only_audit(self):
        self.assertFalse(read_only_audit_message(""))
        self.assertFalse(read_only_audit_message(None))

    def test_message_without_read_only_marker_is_not_an_audit(self):
        self.assertFalse(read_only_audit_message("Fix the reflex client."))

    def test_read_only_marker_with_imperative_target_is_not_an_audit(self):
        message = "Read-only review, then fix the progress guard in nervous.py."
        self.assertFalse(read_only_audit_message(message))


if __name__ == "__main__":
    unittest.main()
