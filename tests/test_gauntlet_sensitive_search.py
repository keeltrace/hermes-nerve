"""RED/GREEN acceptance for the search_files sensitive-path approval-bypass fix (HITL-1).

Scope: hermes_nerve/gate.py, hermes_nerve/risk.py + tests/test_gauntlet_sensitive_search.py only.

Confirmed defect: ``gate.bypass_reason`` granted the unconditional read-only bypass
(``read-only-tool`` / ``catalog-read-only``) for ``search_files`` rooted at
credential / protected-system paths, so a search like
``search_files({pattern: "PRIVATE KEY", path: "~/.ssh"})`` returned private-key
paths into the session context with no approval.  ``risk.assess_tool_call`` and
the pre_tool_call pipeline were never reached because the bypass ran first.

Contract this file verifies:
  * search_files over credential / protected / protected-system targets is NO
    LONGER auto-allowed: bypass becomes None and the classification lands on
    escalation (approve in enforce mode).
  * ``risk.assess_tool_call`` classifies search_files over sensitive paths as
    CONSEQUENTIAL ("Sensitive file access") with path-scoped shape material.
  * Path aliases (``path``/``dir``/``root``/``directory``/``paths``, file:// URIs,
    nested objects, tilde) are screened for sensitive targets.
  * Safe-search behaviour is preserved: benign roots (tmp, project dir) keep the
    catalog-read-only bypass / safe classification.
  * Content-vs-filenames semantics: the *search pattern* never drives the
    escalation; only the *searched path* does.  A pattern that looks like
    credential content over a benign root stays auto-allowed.
  * Approval rule keys stay content-scoped: identical path -> stable key,
    different sensitive path -> different key.

Red-baseline fixtures only; no actual credentials are read or transmitted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hermes_nerve import gate, preferences, risk, catalog  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated home whose sensitive locations are deterministic on disk.

    TMPDIR is pinned away from the fixture so the isolated home is not itself
    classified as a benign temp scope (temp_roots precedence is by design).
    """
    monkeypatch.setattr(risk, "_HOME", tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "no-tmp-root"))
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("PRIVATE KEY-----BEGIN OPENSSH PRIVATE KEY-----\nx\n")
    (tmp_path / ".ssh" / "authorized_keys").write_text("ssh-rsa AAA fake")
    (tmp_path / ".aws" / "credentials").write_text("default = fake-creds")
    (tmp_path / "notes.md").write_text("# project notes\n")
    return tmp_path


def _sensitive_args(home, **overrides):
    """search_files args whose *searched path* is a sensitive target."""
    return {"pattern": "PRIVATE KEY", "path": str(home / ".ssh"), **overrides}


# ---------------------------------------------------------------------------
# RED: bypass must be revoked and classification must escalate
# ---------------------------------------------------------------------------


def test_bypass_reason_is_none_for_credential_home(home):
    bypass = gate.bypass_reason("search_files", _sensitive_args(home))
    assert bypass is None, f"search_files over ~/.ssh bypassed as {bypass!r}"


def test_bypass_reason_is_none_for_protected_systempaths(home):
    for path in ("/etc/ssh", "/etc/shadow", "/etc/sudoers", "/etc/ssl/private", "/root"):
        bypass = gate.bypass_reason("search_files", {"pattern": "x", "path": path})
        assert bypass is None, f"search_files over {path} bypassed as {bypass!r}"


def test_pre_tool_call_lands_on_approval_for_credential_targets(home):
    gate.configure(mode="enforce", min_confidence=0.80, min_allow_probability=0.90,
                   scope="selective", risk_preflight=True, learned_preferences=False,
                   preference_min_samples=2)
    result = gate.pre_tool_call("search_files", _sensitive_args(home),
                                 task_id=None, tool_call_id="tc-test")
    assert result is not None and result["action"] == "approve", (
        f"search_files over ~/.ssh auto-allowed: {result!r}")


def test_risk_assess_tool_call_marks_sensitive_search_consequential(home):
    a = risk.assess_tool_call("search_files", _sensitive_args(home))
    assert a.category == risk.CONSEQUENTIAL, a.category
    assert a.action_label == "Sensitive file access"
    assert "credential/protected location(s)" in a.why
    assert "paths" in (a.shape or {})
    assert "paths_sha256" in (a.shape or {})
    assert any(str(home / ".ssh") in p for p in (a.shape or {}).get("sensitive", []))


def test_risk_assess_tool_call_keeps_benign_search_safe(home):
    a = risk.assess_tool_call("search_files", {"pattern": "x", "path": "/tmp"})
    assert a.category == risk.SAFE, a.category
    assert a.shape and "paths" in a.shape


# ---------------------------------------------------------------------------
# Alias / shape screening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ("dir", "root", "directory"))
def test_search_files_alias_keys_are_screened(home, alias):
    bypass = gate.bypass_reason("search_files", {"pattern": "x", alias: str(home / ".ssh")})
    assert bypass is None, f"{alias}=~/.ssh bypassed as {bypass!r}"


def test_search_files_paths_list_form(home):
    bypass = gate.bypass_reason("search_files", {"pattern": "x",
                                                 "paths": [str(home / "notes.md"),
                                                           str(home / ".ssh")]})
    assert bypass is None, f"paths=[...~/.ssh...] bypassed as {bypass!r}"


def test_file_uri_prefix_is_scanned(home):
    bypass = gate.bypass_reason("search_files", {"pattern": "x",
                                                 "path": "file://" + str(home / ".ssh")})
    assert bypass is None, f"file://~/.ssh bypassed as {bypass!r}"


def test_nested_object_path_is_scanned(home):
    bypass = gate.bypass_reason("search_files", {"pattern": "x",
                                                 "path": {"dir": str(home / ".ssh")}})
    assert bypass is None, f"nested path dict bypassed as {bypass!r}"


@pytest.mark.parametrize("rel", ("id_rsa", ".ssh", "authorized_keys"))
def test_relative_alias_plus_sensitive_workdir_claim_is_screened(home, rel):
    """A relative path whose *claimed workdir* is credential/protected must not
    auto-allow: the search would actually run inside the sensitive location."""
    args = {"pattern": "x", "path": rel, "workdir": str(home / ".ssh")}
    bypass = gate.bypass_reason("search_files", args)
    assert bypass is None, f"{rel!r} with workdir=~/.ssh bypassed as {bypass!r}"
    a = risk.assess_tool_call("search_files", args)
    assert a.category == risk.CONSEQUENTIAL, (rel, a.category)


# ---------------------------------------------------------------------------
# Safe-search preservation + content-vs-filenames semantics
# ---------------------------------------------------------------------------


def test_benign_temp_root_stays_auto_allowed(home):
    bypass = gate.bypass_reason("search_files", {"pattern": "x", "path": "/tmp"})
    assert bypass == "catalog-read-only", bypass
    a = risk.assess_tool_call("search_files", {"pattern": "x", "path": "/tmp"})
    assert a.category == risk.SAFE


def test_benign_project_root_stays_auto_allowed(home):
    project = home / "project"
    project.mkdir()
    bypass = gate.bypass_reason("search_files", {"pattern": "x", "path": str(project)})
    assert bypass == "catalog-read-only", bypass
    a = risk.assess_tool_call("search_files", {"pattern": "x", "path": str(project)})
    assert a.category == risk.SAFE, a.category


def test_pattern_content_never_drives_escalation(home):
    """Content-vs-filenames semantics: the pattern is NOT the search target."""
    # Same credential root, different (critical-looking) pattern -> still escalate.
    a = risk.assess_tool_call("search_files", {"pattern": "SECRET TOKEN",
                                               "path": str(home / ".ssh")})
    assert a.category == risk.CONSEQUENTIAL, a.category

    # Credential pattern over a benign root -> still auto-allowed.
    a = risk.assess_tool_call("search_files", {"pattern": "id_rsa",
                                               "path": "/tmp/notes.md"})
    assert a.category == risk.SAFE, a.category

    b = gate.bypass_reason("search_files", {"pattern": "PRIVATE KEY",
                                            "path": "/tmp"})
    assert b == "catalog-read-only", b


def test_filename_credential_like_pattern_over_benign_root_is_safe(home):
    """A filename that merely *looks* credential over a benign path must not
    escalate (the file is not under a sensitive location, and never was)."""
    a = risk.assess_tool_call("search_files", {"pattern": "id_rsa",
                                               "path": str(home / "notes.md")})
    assert a.category == risk.SAFE, a.category


# ---------------------------------------------------------------------------
# Content-scoped approval rule keys
# ---------------------------------------------------------------------------


def test_approval_rule_key_is_content_scoped_to_searched_paths(home):
    shp1 = risk.assess_tool_call("search_files",
                                  {"pattern": "x", "path": str(home / ".ssh")}).shape
    shp2 = risk.assess_tool_call("search_files",
                                  {"pattern": "x", "path": str(home / ".aws")}).shape
    shp3 = risk.assess_tool_call("search_files",
                                  {"pattern": "x", "path": str(home / ".ssh")}).shape
    k1 = preferences.scoped_rule_key(risk.CONSEQUENTIAL, "search_files", shp1)
    k2 = preferences.scoped_rule_key(risk.CONSEQUENTIAL, "search_files", shp2)
    k3 = preferences.scoped_rule_key(risk.CONSEQUENTIAL, "search_files", shp3)
    assert k1 != k2, "different sensitive roots shared one key"
    assert k1 == k3, "identical searched path produced different keys"
    assert shp1["paths"] and shp2["paths"], "shape did not bind every searched path"


def test_effect_identity_binds_search_files_to_searched_paths(home):
    """A sensitive search approval must be content-scoped (stable for identical
    searched paths, distinct for different roots) rather than a per-invocation
    nonce like the unbound consequential branch."""
    def _effect(path):
        args = {"pattern": "x", "path": path}
        return risk.effect_identity("search_files", args, risk.assess_tool_call("search_files", args))

    e1 = _effect(str(home / ".ssh"))
    e2 = _effect(str(home / ".ssh"))
    e3 = _effect(str(home / ".aws"))
    assert e1 is not None and e1 == e2, "identical sensitive searches must share one key"
    assert e3 is not None and e3 != e1, "different searched roots must produce different keys"
    # A benign search is not consequential, so no effect binding is emitted.
    benign = risk.assess_tool_call("search_files", {"pattern": "x", "path": "/tmp"})
    assert risk.effect_identity("search_files", {"pattern": "x", "path": "/tmp"}, benign) is None
