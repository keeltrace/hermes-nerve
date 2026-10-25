"""Round-4 authorization-integrity regression tests.

D1  path-qualified executables (./ls, /tmp/pytest) never trusted by basename
D2  nested sensitive-home detection (~/.config/gh/hosts.yml, ~/.docker) and
    project .env read handling
D3  git push effect identity: --all/--tags/--mirror, multiple refspecs,
    remote URL changes
D4  unresolved consequential effects -> non-reusable keys (repeated POSTs)
D5  multi-file authorization: every relevant file argument in the key (>8)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hermes_nerve import gate, risk  # noqa: E402

WD = str(REPO)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    owner = tmp_path.with_name(tmp_path.name + "-owner")
    owner.mkdir()
    monkeypatch.setenv("HOME", str(owner))
    monkeypatch.setattr(risk, "_HOME", owner)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "temporary"))
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


def mount(engine):
    gate.evaluate_tool_call.__kwdefaults__["engine_factory"] = (lambda: engine)


def call(command, tool="terminal", tool_call_id="tc", **extra):
    args = {"command": command, "workdir": WD}
    args.update(extra)
    return gate.pre_tool_call(tool, args, task_id=None, tool_call_id=tool_call_id)


# ---------------------------------------------------------------------------
# D1: path-qualified executables
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "./ls -la",
    "/tmp/pytest -q",
    "timeout 10 ./cat README.md",
    "env /tmp/xargs -0 echo",
])
def test_d1_path_qualified_executables_never_trusted(command):
    a = risk.assess_tool_call("terminal", {"command": command, "workdir": WD})
    assert a.category == risk.UNCERTAIN, \
        f"{command!r} trusted by basename -> {a.category} (must be model band)"
    mount(_Escalate())
    result = call(command)
    assert result is not None and result["action"] == "approve", command


@pytest.mark.parametrize("command", [
    "ls -la",
    "pytest tests/ -x -q",
    "cat README.md",
])
def test_d1_bare_trusted_commands_still_routine(command):
    mount(_Escalate())
    assert call(command) is None, f"bare command regressed: {command!r}"


# ---------------------------------------------------------------------------
# D2: nested sensitive-home + env handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "~/.config/gh/hosts.yml",
    "~/.config/gcloud/credentials.db",
    "~/.docker/config.json",
    "~/.gnupg/secring.gpg",
])
def test_d2_nested_sensitive_home_paths_escalate(path):
    path = os.path.expanduser(path)
    a = risk.assess_tool_call("terminal", {"command": f"cat {path}", "workdir": WD})
    assert a.category == risk.CONSEQUENTIAL, f"cat {path} -> {a.category}"


def test_d2_docker_dir_is_sensitive_scope():
    a = risk.classify_path(Path(os.path.expanduser("~/.docker/config.json")), Path(WD))
    assert a == "sensitive-home"


def test_d2_project_env_read_escalates(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env").write_text("API_KEY=secret\n")
    a = risk.assess_tool_call("terminal", {"command": "cat .env", "workdir": str(proj)})
    assert a.category == risk.CONSEQUENTIAL, "reading a project .env exposes secrets"
    mount(_Escalate())
    result = gate.pre_tool_call("terminal", {"command": "cat .env",
                                             "workdir": str(proj)}, task_id=None)
    assert result is not None and result["action"] == "approve"


def test_d2_project_env_write_stays_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    a = risk.assess_tool_call("write_file",
                              {"path": str(proj / ".env"), "content": "A=1\n"},
                              workdir=Path(str(proj)))
    assert a.category in (risk.PROJECT,), "writing a project .env regressed"


def test_d2_non_env_project_reads_still_safe(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "notes.md").write_text("x")
    assert risk.assess_tool_call("terminal", {"command": "cat notes.md",
                                              "workdir": str(proj)}).category == risk.SAFE


# ---------------------------------------------------------------------------
# D3: git push effect identity variants
# ---------------------------------------------------------------------------

@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A real temp git repo; auto-rollback via tmp_path."""
    import subprocess
    r = tmp_path / "repo"
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "f.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "a"], check=True, env=env)
    return r


def _gate_key(command: str, workdir) -> str | None:
    mount(_Escalate())
    result = gate.pre_tool_call("terminal", {"command": command, "workdir": str(workdir)},
                                task_id=None, tool_call_id="d3")
    assert result is not None and result["action"] == "approve"
    return result["rule_key"]


def test_d3_push_all_binds_to_all_branch_refs(repo):
    k1 = _gate_key("git push --all origin", repo)
    import subprocess
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "extra"], check=True)
    (repo / "g.txt").write_text("b\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "b"], check=True, env=env)
    k2 = _gate_key("git push --all origin", repo)
    assert k1 != k2, "--all approval must not authorize a different ref set"


def test_d3_push_mirror_and_tags_are_distinct_effects(repo):
    k_mirror = _gate_key("git push --mirror", repo)
    import subprocess
    subprocess.run(["git", "-C", str(repo), "tag", "v1"], check=True)
    k_tags_after = _gate_key("git push --mirror", repo)
    assert k_mirror != k_tags_after, "new tag changes a --mirror publication"


def test_d3_multiple_refspecs_bound_individually(repo):
    import subprocess
    subprocess.run(["git", "-C", str(repo), "branch", "b2"], check=True)
    k1 = _gate_key("git push origin main b2", repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "b2"])
    (repo / "h.txt").write_text("c\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "c"], check=True, env=env)
    k2 = _gate_key("git push origin main b2", repo)
    assert k1 != k2, "changing ONE of several refspecs must change the key"


def test_d3_remote_url_change_rebinds(repo, monkeypatch):
    k1 = _gate_key("git push origin main", repo)
    monkeypatch.setattr(risk, "resolve_remote_url", lambda w, r: "https://evil.example/x.git")
    k2 = _gate_key("git push origin main", repo)
    assert k1 != k2, "same commits to a DIFFERENT remote URL must re-approve"


def test_d3_same_effect_stable(repo):
    k1 = _gate_key("git push --all origin", repo)
    k2 = _gate_key("git push --all origin", repo)
    assert k1 == k2, "identical effect must keep a stable key"


# ---------------------------------------------------------------------------
# D4: unresolvable consequential effects -> non-reusable keys
# ---------------------------------------------------------------------------

def test_d4_repeated_api_post_reapproves():
    cmd = "curl -X POST -d '{\"build\":1}' https://api.example.com/deploy"
    k1 = _gate_key(cmd, WD)
    k2 = _gate_key(cmd, WD)
    assert k1 is not None and k2 is not None
    assert k1 != k2, "repeated POST must not run on a stored key"


def test_d4_ssh_and_chmod_non_reusable():
    for cmd in ("ssh deploy@prod-1 'systemctl restart app'",
                "chmod 777 /home/shared/tool"):
        k1 = _gate_key(cmd, WD)
        k2 = _gate_key(cmd, WD)
        assert k1 != k2, f"{cmd!r} key reusable"


def test_d4_write_effect_bound_stable():
    """Securely identifiable effects stay bound (stable keys), not nonced."""
    target = "/home/rw-b4/settings.yaml"  # outside project -> consequential
    mount(_Escalate())
    r1 = gate.pre_tool_call("write_file", {"path": target, "content": "same\n"},
                            task_id=None, tool_call_id="w1")
    r2 = gate.pre_tool_call("write_file", {"path": target, "content": "same\n"},
                            task_id=None, tool_call_id="w2")
    assert r1 is not None and r2 is not None
    assert r1["rule_key"] == r2["rule_key"], "identical write effect should stay stable"
    r3 = gate.pre_tool_call("write_file", {"path": target, "content": "other\n"},
                            task_id=None, tool_call_id="w3")
    assert r3["rule_key"] != r1["rule_key"], "different content must re-approve"


# ---------------------------------------------------------------------------
# D5: multi-file authorization (>8 paths)
# ---------------------------------------------------------------------------

def test_d5_all_paths_bound_in_key(tmp_path):
    ten = [str(Path.home() / ".ssh" / f"key{i}") for i in range(10)]
    other = ten[:9] + [str(Path.home() / ".ssh" / "UNRELATED")]
    mount(_Escalate())
    r1 = gate.pre_tool_call("read_file", {"path": list(ten)}, task_id=None, tool_call_id="m1")
    r2 = gate.pre_tool_call("read_file", {"path": list(other)}, task_id=None, tool_call_id="m2")
    assert r1 is not None and r2 is not None
    assert r1["action"] == "approve" and r2["action"] == "approve"
    assert r1["rule_key"] != r2["rule_key"], \
        "approving 10-path read must not authorize a different 10th file"


def test_d5_identical_multi_path_stable():
    ten = [str(Path.home() / ".ssh" / f"key{i}") for i in range(10)]
    mount(_Escalate())
    r1 = gate.pre_tool_call("read_file", {"path": list(ten)}, task_id=None, tool_call_id="m3")
    r2 = gate.pre_tool_call("read_file", {"path": list(ten)}, task_id=None, tool_call_id="m4")
    assert r1["rule_key"] == r2["rule_key"]


def test_d5_terminal_many_files_bound(tmp_path):
    """Terminal reads with many targets bind every target (argv is complete)."""
    files = [tmp_path / f"sensitive_{i}.pem" for i in range(12)]
    for f in files:
        f.write_text("k")
    cmd = "cat " + " ".join(str(f) for f in files)
    mount(_Escalate())
    r1 = gate.pre_tool_call("terminal", {"command": cmd, "workdir": str(tmp_path)},
                            task_id=None)
    assert r1 is not None and r1["action"] == "approve"
    files2 = files[:11] + [tmp_path / "sensitive_11.pem.replaced"]
    files2[-1].write_text("k")
    cmd2 = "cat " + " ".join(str(f) for f in files2)
    r2 = gate.pre_tool_call("terminal", {"command": cmd2, "workdir": str(tmp_path)},
                            task_id=None)
    assert r2 is not None and r2["action"] == "approve"
    assert r1["rule_key"] != r2["rule_key"], "12-file read key must bind every file"


# ---------------------------------------------------------------------------
# routine work must stay automated
# ---------------------------------------------------------------------------

def test_routine_still_flows():
    mount(_Escalate())
    for cmd in ("ls -la", "git status", "git log --oneline -3", "pytest tests/ -x -q",
                "rg 'def decide' hermes_nerve/", "du -sh ."):
        assert call(cmd) is None, f"routine command interrupted: {cmd!r}"
