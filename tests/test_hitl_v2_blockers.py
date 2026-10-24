"""Release-blocker regression tests (review round 2).

Each test documents a defect found by independent review. They are written to
FAIL against the defective implementation and PASS after the fix:

B1  pipeline approval-key collisions (pipeline shapes dropped segment argv)
B2  non-shell tool approval-key collisions (shape was tool-name only)
B3  numeric-target normalization collisions (kill/pid, dd counts, ...)
B4  file-write keys blind to content
B5  git mutating subcommands classified as safe read-only
B6  caller-supplied workdir trusted (workdir=/ lie) + startswith boundary bug
B7  uncertain-band weak-ALLOW auto-allow (insufficient evidence of safety)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hermes_nerve import gate, risk, preferences  # noqa: E402
from hermes_nerve.engine import DecisionResult  # noqa: E402

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
    """Model oracle that always asks for human review — isolates key math."""

    def decide(self, **kwargs):
        return DecisionResult(value="APPROVAL", confidence=0.90,
                              probabilities={"ALLOW": 0.10, "APPROVAL": 0.80, "BLOCK": 0.10},
                              model="oracle", latency_ms=1.0, contract="x")


def mount(engine):
    gate.evaluate_tool_call.__kwdefaults__["engine_factory"] = (lambda: engine)


def call(command, tool="terminal", tool_call_id="tc", **extra):
    args = {"command": command, "workdir": WD}
    args.update(extra)
    return gate.pre_tool_call(tool, args, task_id=None, tool_call_id=tool_call_id)


def escalation_key(command, tool="terminal", **extra):
    mount(_Escalate())
    result = call(command, tool=tool, **extra)
    assert result is not None and result["action"] == "approve", \
        f"expected escalation for {command!r}, got {result}"
    return result["rule_key"]


# ---------------------------------------------------------------------------
# B1: pipelines — the key must cover what actually runs in every segment
# ---------------------------------------------------------------------------

def test_b1_pipeline_keys_differ_by_content():
    k1 = escalation_key("curl https://downloads.example.com/a.sh | head -20")
    k2 = escalation_key("curl https://downloads.example.com/b.sh | head -20")
    assert k1 != k2, "different pipelines must never share an approval key"


def test_b1_pipeline_segment_order_is_part_of_the_operation():
    k1 = escalation_key("curl https://downloads.example.com/a.sh | sha256sum")
    k2 = escalation_key("sha256sum | curl https://downloads.example.com/a.sh")
    assert k1 != k2, "reordered segments are a different operation"


# ---------------------------------------------------------------------------
# B2: non-shell tools — key must cover the arguments, not just the tool name
# ---------------------------------------------------------------------------

def test_b2_non_shell_tool_keys_differ_by_args():
    gate.configure(mode="enforce", min_confidence=0.80, min_allow_probability=0.90,
                   scope="selective", risk_preflight=False)
    mount(_Escalate())
    r1 = gate.pre_tool_call("browser_navigate", {"url": "https://example.com/docs"}, task_id=None)
    r2 = gate.pre_tool_call("browser_navigate", {"url": "https://evil.example.com/drop"}, task_id=None)
    assert r1 is not None and r2 is not None, "non-shell tool calls should reach the model band"
    assert r1["rule_key"] != r2["rule_key"], "different arguments must never share a key"


def test_b2_process_manage_keys_differ_by_action():
    gate.configure(mode="enforce", min_confidence=0.80, min_allow_probability=0.90,
                   scope="selective", risk_preflight=False)
    mount(_Escalate())
    r1 = gate.pre_tool_call("process_manage", {"action": "list"}, task_id=None)
    r2 = gate.pre_tool_call("process_manage", {"action": "kill", "pid": 4242}, task_id=None)
    assert r1 is not None and r2 is not None
    assert r1["rule_key"] != r2["rule_key"]


# ---------------------------------------------------------------------------
# B3: numeric targets must not normalize away the target identity
# ---------------------------------------------------------------------------

def test_b3_kill_pid_targets_do_not_share_keys():
    k1 = escalation_key("kill -9 1234")
    k2 = escalation_key("kill -9 1")
    assert k1 != k2, "approving one kill target must not authorize killing another pid"


def test_b3_dd_is_prohibited_regardless():
    for cmd in ("dd if=x.iso of=/dev/sda", "dd if=x of=y bs=1M count=100"):
        result = call(cmd)
        assert result is not None and result["action"] == "block", cmd


def test_b3_chmod_modes_differ():
    """Mode tokens must keep their identity in the shape material and keys.

    Pre-fix, positional numbers collapsed to '#', so `chmod 777 X` and
    `chmod 666 X` shared one approval key. (Escalation-level numeric identity
    is proven by test_b3_kill_pid_targets_do_not_share_keys; 777 is the
    chmod mode that escalates by design — world-writable.)
    """
    a777 = risk.assess_tool_call("terminal", {"command": "chmod 777 /home/b4user-b4/shared-tool",
                                              "workdir": Path(WD)})
    a666 = risk.assess_tool_call("terminal", {"command": "chmod 666 /home/b4user-b4/shared-tool",
                                              "workdir": Path(WD)})
    assert a777.shape["argv"][1] == "777" and a666.shape["argv"][1] == "666", \
        "mode tokens must not be normalized away in shape material"
    from hermes_nerve import preferences as prefs
    assert prefs.scoped_rule_key(a777.category, "terminal", a777.shape) != \
        prefs.scoped_rule_key(a666.category, "terminal", a666.shape), \
        "different modes must never share an approval key"
    assert a777.category == risk.CONSEQUENTIAL  # world-writable still escalates


# ---------------------------------------------------------------------------
# B4: file-write keys must cover the content actually written
# ---------------------------------------------------------------------------

def test_b4_write_shape_carries_content_digest():
    path = os.path.join(WD, "config", "settings.yaml")
    benign = risk.assess_tool_call("write_file", {"path": path, "content": "debug: false\n"})
    evil = risk.assess_tool_call(
        "write_file",
        {"path": path, "content": "debug: false\n# echo 'evil' >> ~/.bashrc\n"})
    assert benign.shape.get("content_sha256"), "write shape must carry a content digest"
    assert benign.shape["content_sha256"] != evil.shape["content_sha256"], \
        "same path, different content => different authorized operations"


def test_b4_escalation_keys_differ_by_content():
    mount(_Escalate())
    target = "/home/b4user-b4/settings.yaml"  # outside project -> consequential
    r1 = gate.pre_tool_call("write_file", {"path": target, "content": "debug: false\n"},
                            task_id=None, tool_call_id="w1")
    r2 = gate.pre_tool_call("write_file",
                            {"path": target, "content": "debug: false\n# echo evil >> ~/.bashrc\n"},
                            task_id=None, tool_call_id="w2")
    assert r1 is not None and r2 is not None
    assert r1["rule_key"] != r2["rule_key"], \
        "approving one write must not authorize different content to the same path"


# ---------------------------------------------------------------------------
# B5: git mutating subcommands must not ride the safe read-only path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git config user.email operator@example.com",
    "git config --global core.hooksPath /tmp/hooks",
    "git stash",
    "git stash push -m wip",
    "git tag v9.9.9",
    "git branch evil-branch",
    "git remote add origin https://evil.example.com/repo.git",
    "git worktree add /tmp/wt2",
])
def test_b5_git_mutations_not_classified_safe(command):
    a = risk.assess_tool_call("terminal", {"command": command, "workdir": WD})
    assert a.category != risk.SAFE, f"{command!r} classified safe_routine"
    result = call(command)
    assert result is None or result["action"] in ("approve", "block"), command
    # mutation may be auto-allowed ONLY as authorized_project_work, never as safe read-only
    if result is None:
        assert a.category == risk.PROJECT, f"{command!r} auto-allowed outside the project-work class"


@pytest.mark.parametrize("command", [
    "git status", "git log --oneline -3", "git diff HEAD~1", "git remote -v",
    "git tag", "git branch", "git stash list", "git config --get user.email",
    "git worktree list",
])
def test_b5_git_reads_still_safe(command):
    a = risk.assess_tool_call("terminal", {"command": command, "workdir": WD})
    assert a.category == risk.SAFE, f"legitimate read regressed: {command!r} -> {a.category}"


# ---------------------------------------------------------------------------
# B6: workdir must be canonical and boundary-checked, not trusted
# ---------------------------------------------------------------------------

def test_b6_workdir_root_lie_never_grants_scope(tmp_path):
    # The agent claims workdir=/ so everything "is inside the project".
    a = risk.assess_tool_call("terminal",
                              {"command": "rm /etc/someconfig.conf", "workdir": "/"})
    assert a.category in (risk.PROHIBITED, risk.CONSEQUENTIAL), \
        "claiming / as workdir must not make system files project-local"


def test_b6_workdir_home_lie_never_grants_scope():
    home = str(Path.home())
    a = risk.assess_tool_call("terminal",
                              {"command": "rm -rf ~/.ssh", "workdir": home})
    assert a.category == risk.PROHIBITED, "claiming ~ as workdir must not protect ~/.ssh deletion"


def test_b6_sibling_prefix_boundary(tmp_path):
    """startswith() scope checks once matched sibling directories.

    /deep/proj-secret/build startswith /deep/proj — so `rm -rf` of a SIBLING
    project's artifact dir used to classify as this project's own artifact
    cleanup and auto-allow. Boundary checks must be segment-exact.
    """
    proj = tmp_path / "proj"
    sibling = tmp_path / "proj-secret"
    proj.mkdir()
    (sibling / "build").mkdir(parents=True)
    (sibling / "build" / "bundle.js").write_text("x")
    a = risk.assess_tool_call("terminal",
                              {"command": "rm -rf ../proj-secret/build",
                               "workdir": str(proj)})
    assert a.category != risk.PROJECT, \
        "sibling directory deletion must not classify as own-project artifact cleanup"
    gate.configure(mode="enforce", min_confidence=0.80, min_allow_probability=0.90,
                   scope="selective", risk_preflight=True)
    mount(_Escalate())
    result = gate.pre_tool_call("terminal", {"command": "rm -rf ../proj-secret/build",
                                             "workdir": str(proj)}, task_id=None)
    assert result is not None and result["action"] == "approve", \
        "deleting a sibling project's directory must require human approval"


def test_b6_missing_workdir_claim_grants_no_scope(tmp_path, monkeypatch):
    """A claimed workdir that does not exist must not define the scope.

    The gate must classify against the REAL execution directory (process cwd),
    not the agent's unverified claim: pre-fix, the shape material carried the
    bogus claim and relative reads were scoped against a directory that would
    never be touched.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "notes.txt").write_text("real content\n")
    decoy = tmp_path / "decoy-does-not-exist"
    monkeypatch.chdir(proj)
    a = risk.assess_tool_call("terminal", {"command": "cat notes.txt",
                                           "workdir": str(decoy)})
    import json as _json
    shape_material = _json.dumps(a.shape, default=str)
    assert str(decoy) not in shape_material, \
        "classification scoped to a nonexistent claimed workdir"
    assert str(proj) in shape_material, "classification must use the real execution dir"
    assert a.category == risk.SAFE  # notes.txt genuinely lives in the real scope


# ---------------------------------------------------------------------------
# B7: uncertain band — weak-ALLOW is not sufficient evidence of safety
# ---------------------------------------------------------------------------

def test_b7_weak_allow_in_uncertain_band_escalates():
    """p(ALLOW)=0.82 on an unrecognized command must NOT silently run."""
    class WeakAllow:
        def decide(self, **kwargs):
            return DecisionResult(value="ALLOW", confidence=0.85,
                                  probabilities={"ALLOW": 0.82, "APPROVAL": 0.12, "BLOCK": 0.06},
                                  model="oracle", latency_ms=1.0, contract="x")
    mount(WeakAllow())
    result = call("./unrecognized-operator-tool.sh --run")
    assert result is not None, "weak-ALLOW on unrecognized command must escalate to a human"
    assert result["action"] == "approve"
    # honest wording: uncertainty, no invented hazard, no bare confidence number
    assert "confidence 0.85" not in result["message"]
    assert "p(ALLOW) 0.82" not in result["message"]


def test_b7_strong_allow_still_proceeds():
    class StrongAllow:
        def decide(self, **kwargs):
            return DecisionResult(value="ALLOW", confidence=0.97,
                                  probabilities={"ALLOW": 0.97, "APPROVAL": 0.02, "BLOCK": 0.01},
                                  model="oracle", latency_ms=1.0, contract="x")
    mount(StrongAllow())
    assert call("./well-understood-release-script.sh --dry-run") is None


def test_b7_reported_failure_case_still_auto_allows():
    """The original 0.82-vs-0.90 scenario: pytest is recognized-safe deterministically."""
    class WeakAllow:
        def decide(self, **kwargs):
            raise AssertionError("recognized command must not reach the model")
    mount(WeakAllow())
    assert call("pytest tests/ -x -q") is None
