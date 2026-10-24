"""Packaged plaintext policy tables for Nerve's deterministic risk policy.

Each ``*.txt`` file here is protective security policy DATA (protected
system locations, blocked command names) kept separate from the
classification engine in :mod:`hermes_nerve.risk` and
:mod:`hermes_nerve.policy_tables`. Tables are loaded once at import time
and must never be edited at runtime; edits are source changes covered by
the test suite's exact-set pins.
"""
