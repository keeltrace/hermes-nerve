"""0.4.2 Phase 1/3: effect-aware MCP classification and scoped approval failover.

Fail-before evidence: before this release, `catalog._classify_mcp` treated any
tool whose leading verb was read-style as SAFE, so `mcp__x__get_secret` skipped
the gate, and `gate.instrument_pre_tool_chain` used one shared approval key
(`nerve:hitl:gate-failure`) for every classifier crash.
"""
from __future__ import annotations
from typing import Any

import pytest

from hermes_nerve import catalog, gate, risk


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield


@pytest.fixture
def _owner_home(tmp_path, monkeypatch):
    # Own the policy HOME as well as plugin storage; never assume a specific user home.
    # Sibling, not child: a project workdir must not become HOME's ancestor.
    owner = tmp_path.with_name(tmp_path.name + "-owner")
    owner.mkdir()
    monkeypatch.setenv("HOME", str(owner))
    monkeypatch.setattr(risk, "_HOME", owner)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "temporary"))
    yield owner


def _fixture_home_args(value: Any, owner) -> Any:
    """Bind path templates without changing argument nesting or list shape."""
    if isinstance(value, str):
        return value.replace("@HOME@", str(owner))
    if isinstance(value, list):
        return [_fixture_home_args(item, owner) for item in value]
    if isinstance(value, dict):
        return {key: _fixture_home_args(item, owner) for key, item in value.items()}
    return value


SENSITIVE_MCP = [
    "mcp__server__get_secret",
    "mcp__server__read_private_key",
    "mcp__server__list_credentials",
    "mcp__server__fetch_sensitive_config",
    "mcp__vault__get_token",
    "mcp__aws__read_access_key",
]

HARMLESS_MCP = [
    "mcp__github__list_issues",
    "mcp__github__get_file_contents",
    "mcp__fetch__search_docs",
    "mcp__time__get_time",
]


@pytest.mark.parametrize("tool", SENSITIVE_MCP)
def test_secret_shaped_mcp_reads_are_never_auto_allowed(tool):
    assessed = risk.assess_tool_call(tool, {})
    assert assessed.category == risk.CONSEQUENTIAL, (tool, assessed.category)
    assert gate.bypass_reason(tool, {}) is None, tool
    assert not assessed.auto_allow, tool
    assert "credential" in (assessed.why or "").lower() or "secret" in (assessed.why or "").lower()


@pytest.mark.parametrize("tool", HARMLESS_MCP)
def test_harmless_mcp_reads_are_bounded_without_a_prompt(tool):
    assessed = risk.assess_tool_call(tool, {})
    assert assessed.category == risk.SAFE, (tool, assessed.category)
    assert assessed.auto_allow, tool
    # Inferred-safe, not catalog-verified: the audit must not count it as such.
    assert assessed.shape.get("basis") == "mcp-verb-screened", assessed.shape


@pytest.mark.parametrize("tool,args", [
    ("mcp__files__get_file", {"path": "@HOME@/.ssh/id_rsa"}),
    ("mcp__files__read_file", {"file_path": "@HOME@/.aws/credentials"}),
    ("mcp__files__list_dir", {"paths": ["@HOME@/.ssh/id_ed25519"]}),
    ("mcp__files__read_file", {"options": {"target": "@HOME@/.env"}}),
    ("mcp__files__get_resource", {"uri": "file://@HOME@/.ssh/id_rsa"}),
])
def test_sensitive_paths_through_alternate_argument_shapes_escalate(tool, args, _owner_home):
    args = _fixture_home_args(args, _owner_home)
    assessed = risk.assess_tool_call(tool, args)
    assert assessed.category == risk.CONSEQUENTIAL, (tool, args, assessed.category)
    assert gate.bypass_reason(tool, args) is None


@pytest.mark.parametrize("args", [
    {"path": "@HOME@/.ssh/id_rsa"},
    {"file_path": "@HOME@/.aws/credentials"},
    {"paths": ["@HOME@/.gnupg/secring.gpg"]},
    {"nested": {"path": "@HOME@/.config/gh/hosts.yml"}},
    {"uri": "file://@HOME@/.ssh/id_rsa"},
])
def test_native_read_shapes_still_escalate(args, _owner_home):
    args = _fixture_home_args(args, _owner_home)
    assessed = risk.assess_tool_call("read_file", args)
    assert assessed.category == risk.CONSEQUENTIAL
    assert gate.bypass_reason("read_file", args) is None


def test_credential_bearing_arguments_escalate_even_with_read_verb():
    assessed = risk.assess_tool_call(
        "mcp__server__query_records", {"table": "users", "fields": ["password_hash", "token"]})
    assert assessed.category == risk.CONSEQUENTIAL
    assert gate.bypass_reason("mcp__server__query_records",
                              {"table": "users", "fields": ["password_hash"]}) is None


def test_environment_leak_commands_are_not_read_only_bypasses():
    for command in ["printenv", "printenv OPENROUTER_API_KEY", "env", "set",
                    "export -p", "cat /home/user/.env", "cat /proc/self/environ"]:
        assert gate.bypass_reason("terminal", {"command": command}) is None, command


def test_ordinary_read_commands_still_bypass():
    for command in ["git status --short", "ls -la", "cat README.md",
                    "grep -rn catalog hermes_nerve", "git log --oneline -3"]:
        assert gate.bypass_reason("terminal", {"command": command}) == "read-only-terminal", command


def test_nested_dispatch_is_classified_by_inner_operation():
    sensitive = {"name": "mcp__server__get_secret", "arguments": {}}
    assert risk.assess_tool_call("tool_call", sensitive).category == risk.CONSEQUENTIAL
    assert gate.bypass_reason("tool_call", sensitive) is None
    harmless = {"name": "mcp__github__list_issues", "arguments": {}}
    inner = risk.assess_tool_call("tool_call", harmless)
    assert inner.category == risk.SAFE
    assert gate.bypass_reason("tool_call", harmless) is None  # dispatch itself never bypasses


def test_audit_separates_verified_safe_from_inferred():
    audit = catalog.inventory_audit(
        ["skills_list", "skill_manage", "mcp__github__list_issues", "mcp__server__get_secret",
         "some_unknown_tool"])
    assert audit["total"] == 5
    assert "mcp__github__list_issues" in audit["inferred_bounded"]
    assert "mcp__server__get_secret" in audit["consequential"]
    assert "skills_list" in audit["verified_safe"]
    assert "some_unknown_tool" in audit["unclassified"]
    assert "mcp__github__list_issues" not in audit["verified_safe"]


def test_gate_failure_rule_key_is_scoped_and_one_shot():
    seen = []
    def sink(record):
        seen.append(record)
    def boom(*a, **k):
        raise RuntimeError("classifier exploded")
    chain = gate.instrument_pre_tool_chain(
        [("action_gate", boom, True)], sink)
    first = chain(tool_name="terminal", arguments={"command": "rm -rf build"})
    second = chain(tool_name="terminal", arguments={"command": "rm -rf build"})
    assert first["action"] == "approve" and second["action"] == "approve"
    k1, k2 = first["rule_key"], second["rule_key"]
    assert k1 != k2, "failure approval keys must not be reusable across calls"
    assert "gate-failure" in k1


def test_gate_failure_key_does_not_collide_with_action_keys():
    def boom(*a, **k):
        raise RuntimeError("x")
    chain = gate.instrument_pre_tool_chain([("action_gate", boom, True)], lambda r: None)
    out = chain(tool_name="terminal", arguments={"command": "echo hi"})
    assert not out["rule_key"].startswith("nerve:scoped:terminal:echo"), out["rule_key"]


# --- Phase 2 autonomy policy -------------------------------------------------

def _ask_only(monkeypatch):
    monkeypatch.setenv("HERMES_NERVE_GATE_MODE", "ask-only")
    gate._configured_mode = None


def test_routine_local_command_runs_without_a_prompt(monkeypatch, tmp_path):
    _ask_only(monkeypatch)
    monkeypatch.setenv("HERMES_NERVE_GATE_EVENTS", str(tmp_path / "gate.jsonl"))
    called = {"n": 0}
    def _never(**kw):
        called["n"] += 1
        raise AssertionError("bounded routine work must not consult the model")
    monkeypatch.setattr(gate, "evaluate_tool_call", _never)
    for command in ["pytest -q", "python -m pytest tests/ -q", "make build",
                    "ruff check .", "git commit -m wip"]:
        assert gate.pre_tool_call("terminal", {"command": command}, "t") is None, command
    assert called["n"] == 0


def test_unbounded_commands_still_escalate_in_ask_only(monkeypatch, tmp_path):
    _ask_only(monkeypatch)
    monkeypatch.setenv("HERMES_NERVE_GATE_EVENTS", str(tmp_path / "gate.jsonl"))
    monkeypatch.setattr(gate, "evaluate_tool_call",
                        lambda **kw: (_ for _ in ()).throw(TimeoutError("no provider")))
    for command in ["kubectl apply -f prod.yaml", "ssh prod-host deploy",
                    "git push origin main", "docker run --rm img", "rm -rf /"]:
        decision = gate.pre_tool_call("terminal", {"command": command}, "t")
        assert decision is not None and decision["action"] in {"approve", "block"}, command


def test_project_edit_is_routine_outside_the_prompt_path(tmp_path):
    target = tmp_path / "proj" / "src.py"
    target.parent.mkdir()
    assessed = risk.assess_tool_call("write_file", {"path": str(target), "content": "x = 1"})
    assert assessed.category == risk.PROJECT and assessed.auto_allow


def test_consequence_message_is_short_and_concrete():
    assessed = risk.RiskAssessment(
        risk.CONSEQUENTIAL, "terminal", action_label="Destructive delete",
        summary="rm -rf build", why="it removes a directory tree",
        impact="Permanently deletes the build directory and its artifacts",
        display="rm -rf build")
    message = gate.format_consequence_message(assessed)
    assert "Do this ->" in message and "Skip ->" in message
    assert "%" not in message and "confidence" not in message.lower()
    assert len(message.splitlines()) <= 5
