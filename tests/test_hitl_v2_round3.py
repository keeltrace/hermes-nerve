"""Round-3 release-blocker regression tests.

R1  workdir authorization: sensitive directories and system paths are
    protected REGARDLESS of the supplied working directory; claiming a
    sensitive directory as workdir grants no scope. Probed: ~/.ssh,
    /etc/ssh, sibling projects.
R2  destructive git: stash clear/drop, force branch deletion, global/system
    config writes, worktree remove --force must not auto-run as project work.
R3  permanent approval semantics: consequential approvals bind to the
    distinct EFFECT (resolved commit SHAs for pushes; target stat identity
    for deletions). An approved push must not authorize publishing different
    commits. Unresolvable effects re-approve every time.
R4  read_file must not bypass sensitive-file checks.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hermes_nerve import gate, risk, preferences as prefs  # noqa: E402

WD = str(REPO)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_NERVE_GATE_EVENTS", str(tmp_path / "gate-events.jsonl"))
    monkeypatch.setenv("HERMES_NERVE_PREFERENCES", str(tmp_path / "preferences.json"))
    gate.configure(mode="enforce", min_confidence=0.80, min_allow_probability=0.90,
                   scope="selective", risk_preflight=True,
                   learned_preferences=False, preference_min_samples=2)
    yield


class _Escalate:
    def decide(self, **kwargs):
        from hermes_nerve.engine import DecisionResult
        return DecisionResult(value="APPROVAL", confidence=0.90,
                              probabilities={"ALLOW": 0.10, "APPROVAL": 0.80, "BLOCK": 0.10},
                              model="oracle", latency_ms=1.0, contract="x")


def call(command, tool="terminal", tool_call_id="tc", **extra):
    args = {"command": command, "workdir": WD}
    args.update(extra)
    return gate.pre_tool_call(tool, args, task_id=None, tool_call_id=tool_call_id)


# ---------------------------------------------------------------------------
# R1: sensitive dirs and system paths protected regardless of workdir
# ---------------------------------------------------------------------------

def test_r1_workdir_claim_inside_ssh_grants_no_scope():
    """workdir=~/.ssh must not make its contents project reads."""
    a = risk.assess_tool_call("terminal", {"command": "cat known_hosts",
                                           "workdir": str(Path.home() / ".ssh")})
    assert a.category != risk.SAFE, "reading via a claimed .ssh workdir must not be safe_routine"
    gate.evaluate_tool_call.__kwdefaults__["engine_factory"] = _Escalate
    result = gate.pre_tool_call("terminal", {"command": "cat known_hosts",
                                             "workdir": str(Path.home() / ".ssh")}, task_id=None)
    assert result is not None and result["action"] == "approve"


def test_r1_workdir_claim_inside_etc_ssh_grants_no_scope():
    a = risk.assess_tool_call("terminal", {"command": "cat sshd_config",
                                           "workdir": "/etc/ssh"})
    assert a.category != risk.SAFE, "reading via a claimed /etc/ssh workdir must not be safe"


def test_r1_etc_ssh_read_escalates_with_legit_workdir():
    a = risk.assess_tool_call("terminal", {"command": "cat /etc/ssh/sshd_config",
                                           "workdir": WD})
    assert a.category != risk.SAFE, "/etc/ssh read must escalate regardless of workdir"
    assert call("cat /etc/ssh/sshd_config") is not None


def test_r1_system_deletion_escalates():
    a = risk.assess_tool_call("terminal", {"command": "rm -r /etc/ssh", "workdir": WD})
    assert a.category in (risk.PROHIBITED, risk.CONSEQUENTIAL)


def test_r1_ssh_dir_deletion_via_claimed_workdir_not_project_cleanup():
    a = risk.assess_tool_call("terminal", {"command": "rm known_hosts",
                                           "workdir": str(Path.home() / ".ssh")})
    assert a.category in (risk.PROHIBITED, risk.CONSEQUENTIAL), \
        "deleting inside ~/.ssh must never be project work"


def test_r1_sibling_project_isolation(tmp_path):
    projA = tmp_path / "projA"
    projB = tmp_path / "projB"
    projA.mkdir()
    (projB / "build").mkdir(parents=True)
    a = risk.assess_tool_call("terminal", {"command": "rm -rf ../projB/build",
                                           "workdir": str(projA)})
    assert a.category != risk.PROJECT, "sibling project cleanup must not be own-project work"
    result = gate.pre_tool_call("terminal", {"command": "rm -rf ../projB/build",
                                             "workdir": str(projA)}, task_id=None)
    assert result is not None and result["action"] == "approve"


def test_r1_routine_system_read_still_safe():
    """Guard: harmless system reads (os-release) keep flowing."""
    a = risk.assess_tool_call("terminal", {"command": "cat /etc/os-release", "workdir": WD})
    assert a.category == risk.SAFE, "routine system inspection regressed"


# ---------------------------------------------------------------------------
# R2: destructive git operations are never auto-run project work
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git stash clear",
    "git stash drop",
    "git branch -D feature-x",
    "git branch --delete --force feature-x",
    "git branch -B feature-x",
    "git config --global user.email operator@example.com",
    "git config --global core.hooksPath /tmp/hooks",
    "git config --global --unset user.email",
    "git config --system core.hooksPath /tmp/hooks",
    "git worktree remove --force /tmp/wt2",
])
def test_r2_destructive_git_requires_approval(command):
    a = risk.assess_tool_call("terminal", {"command": command, "workdir": WD})
    assert a.category == risk.CONSEQUENTIAL, f"{command!r} -> {a.category}"
    assert a.auto_allow is False
    result = call(command)
    assert result is not None and result["action"] == "approve", command


@pytest.mark.parametrize("command", [
    "git branch -d merged-branch",          # git refuses unmerged: safe to auto
    "git branch -m renamed-branch",
    "git config --global --get user.email",  # global READ stays safe
    "git stash list",
    "git stash push -m wip",
])
def test_r2_benign_git_still_project_or_safe(command):
    a = risk.assess_tool_call("terminal", {"command": command, "workdir": WD})
    assert a.category in (risk.SAFE, risk.PROJECT), f"{command!r} -> {a.category}"
    assert a.auto_allow is True


# ---------------------------------------------------------------------------
# R3: consequential approvals bind to the distinct effect
# ---------------------------------------------------------------------------

def _push_key(sha: str, unresolved: bool = False) -> str | None:
    """Gate key for a push under a given resolved HEAD sha."""
    def fake_resolve(workdir, refspecs):
        if unresolved:
            return {}
        return {ref: sha for ref in refspecs}

    original = risk.resolve_git_refs
    risk.resolve_git_refs = fake_resolve
    try:
        a = risk.assess_tool_call("terminal", {"command": "git push origin feature/hitl-v2",
                                               "workdir": WD})
        gate.evaluate_tool_call.__kwdefaults__["engine_factory"] = _Escalate
        result = gate.pre_tool_call("terminal", {"command": "git push origin feature/hitl-v2",
                                                 "workdir": WD}, task_id=None,
                                    tool_call_id="r3")
    finally:
        risk.resolve_git_refs = original
    assert result is not None and result["action"] == "approve"
    return result["rule_key"]


def test_r3_push_key_binds_to_commit_state():
    k1 = _push_key("aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111")
    k2 = _push_key("bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222")
    assert k1 is not None and k2 is not None
    assert k1 != k2, "an approved push must not authorize publishing different commits"


def test_r3_same_commit_same_key():
    k1 = _push_key("aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111")
    k2 = _push_key("aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111")
    assert k1 == k2, "identical effect should keep a stable key"


def test_r3_unresolvable_effect_reapproves_every_time():
    k1 = _push_key("x", unresolved=True)
    k2 = _push_key("x", unresolved=True)
    assert k1 is not None and k2 is not None
    assert k1 != k2, "unverifiable effect must re-approve on every invocation"


def test_r3_deletion_key_binds_target_identity(tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("v1")

    def stat_key():
        original = risk.target_stat_signature
        risk.target_stat_signature = lambda p: ("dev1", 111, 3, 1000)
        try:
            gate.evaluate_tool_call.__kwdefaults__["engine_factory"] = _Escalate
            r = gate.pre_tool_call("terminal", {"command": f"rm {target}",
                                                "workdir": WD}, task_id=None)
        finally:
            risk.target_stat_signature = original
        assert r is not None and r["action"] == "approve"
        return r["rule_key"]

    k1 = stat_key()
    # file recreated with a different identity (same path)
    risk.target_stat_signature_orig = None
    original = risk.target_stat_signature
    risk.target_stat_signature = lambda p: ("dev1", 222, 5, 2000)
    try:
        gate.evaluate_tool_call.__kwdefaults__["engine_factory"] = _Escalate
        r2 = gate.pre_tool_call("terminal", {"command": f"rm {target}",
                                             "workdir": WD}, task_id=None)
    finally:
        risk.target_stat_signature = original
    assert r2 is not None and r2["action"] == "approve"
    assert r2["rule_key"] != k1, "recreated file is a distinct deletion effect"


# ---------------------------------------------------------------------------
# R4: read_file cannot bypass sensitive-file checks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    str(Path.home() / ".ssh" / "id_ed25519"),
    "/etc/ssh/sshd_config",
    "/etc/shadow",
    str(Path.home() / ".aws" / "credentials"),
])
def test_r4_read_file_sensitive_paths_escalate(path):
    assert gate.bypass_reason("read_file", {"path": path}) is None, \
        f"read_file({path}) must not bypass the gate"
    a = risk.assess_tool_call("read_file", {"path": path})
    assert a.category == risk.CONSEQUENTIAL, f"read_file({path}) -> {a.category}"
    result = gate.pre_tool_call("read_file", {"path": path}, task_id=None)
    assert result is not None and result["action"] == "approve"


def test_r4_read_file_routine_paths_still_flow():
    assert gate.bypass_reason("read_file", {"path": os.path.join(WD, "README.md")}) is not None or \
        risk.assess_tool_call("read_file", {"path": os.path.join(WD, "README.md")}).auto_allow
    assert call("cat README.md") is None  # terminal read path unchanged
