"""Operator University tests (N-36..N-38): manifest completeness, real labs,
progress privacy, scored rubric grading, capstone independence and containment.

Regression contract (v0.4.0 security review):
- the verifier never creates required deliverables;
- a fresh fixture FAILS, a genuinely completed fixture PASSES, and removing a
  required artifact FAILS;
- verification is refused outside the Nerve disposable capstone root;
- single-word answers cannot pass multi-group rubrics;
- labs record completion only after deterministic checks pass.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hermes_nerve import university as uni  # noqa: E402


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    yield


def _complete_capstone(fixture: Path) -> None:
    org = fixture / "org"
    dl = org / "deliverable"
    (dl / "README.md").write_text("x" * 220, encoding="utf-8")
    (dl / "checkpoint-capsule.json").write_text(json.dumps(
        {"verified_criteria": ["deliverable built"], "failed_paths": [],
         "next_frontier": "none"}), encoding="utf-8")
    (dl / "experience-notes.json").write_text(json.dumps(
        {"verified": [{"claim": "capstone produced", "evidence": "checkpoint-capsule.json"}]}),
        encoding="utf-8")
    (org / "roles.json").write_text(json.dumps(
        [{"name": "implementer", "owns": ["code"], "evidence": ["t1"]},
         {"name": "reviewer", "owns": ["review"], "evidence": ["t2"]}]), encoding="utf-8")
    (org / "dod.json").write_text(json.dumps(
        {"criteria": [{"criterion": "tests", "status": "pass"},
                      {"criterion": "docs", "status": "fail"},
                      {"criterion": "lint", "status": "pass"}]}), encoding="utf-8")
    (org / "route-policy.json").write_text(json.dumps(
        {"provider": "jev-1.14.0", "fallback": "local-d1"}), encoding="utf-8")
    (org / "approval-log.json").write_text(json.dumps(
        [{"action": "git push origin main", "effect": "origin/main",
          "decision": "denied"}]), encoding="utf-8")


def test_manifest_has_nine_modules():
    m = uni.load_manifest()
    assert [e["id"] for e in m["modules"]] == [str(i) for i in range(1, 10)]


def test_every_module_complete_content_with_rubric_and_lab_task():
    for e in uni.load_manifest()["modules"]:
        mod = uni.load_module(e["id"])
        assert mod["lessons"] and all(l["body"].strip() for l in mod["lessons"])
        assert mod["lab"]["steps"]
        assert len(mod["check"]["rubric"]) >= 2, f"module {e['id']} rubric too weak"
        questions = mod["lab"]["task"]["questions"]
        assert len(questions) >= 2, f"module {e['id']} lab task too weak"
        for q in questions:
            assert q["any_of"] and q["q"].strip()


def test_single_word_cannot_pass_multi_group_rubric():
    ok, _q, missed = uni.grade_check_details("tester", "7", "risk")
    assert not ok and len(missed) >= 1


def test_full_answer_passes_and_pass_is_sticky():
    uni.grade_check_details("tester", "7", "risk")  # fail first
    ok, _q, missed = uni.grade_check_details(
        "tester", "7",
        "Consequential operations need approval; approvals bind to the exact "
        "effect; prompts must explain impact, not show a confidence number.")
    assert ok and missed == []
    ok3, _, _ = uni.grade_check_details("tester", "7", "no")  # failed retry
    assert not ok3
    assert uni.load_progress("tester")["modules"]["7"]["check_passed"] is True


def test_check_grading_rejects_wrong_answer():
    ok, q = uni.grade_check("tester", "1", "nerve owns everything")
    assert not ok and "completion" in q


def test_progress_is_profile_isolated():
    uni.record_lesson("alpha", "1", "What Hermes owns")
    assert uni.load_progress("alpha")["modules"]["1"]["lessons_read"] == ["What Hermes owns"]
    assert uni.load_progress("beta")["modules"] == {}
    files = list((Path(os.environ["HERMES_HOME"]) / "nerve" / "university").glob("progress-*.json"))
    assert len(files) == 1 and "alpha" in files[0].name


def test_lab_requires_valid_submission():
    r1 = uni.run_lab("1", "tester", submission={"nonsense": "x"})
    assert r1["passed"] is False
    assert uni.load_progress("tester")["modules"]["1"]["lab_done"] is False
    if not r1.get("fixture_retained"):
        assert not Path(r1["fixture"]).exists()  # disposable cleanup
    mod1 = uni.load_module("1")
    full = {"files": mod1["lab"]["executable"]["solution"]["files"],
            "q1": "hermes", "q2": "operator", "q3": "no"}
    r2 = uni.run_lab("1", "tester", submission=full)
    assert r2["passed"] is True
    assert all(c["passed"] for c in r2["checks"])
    assert uni.load_progress("tester")["modules"]["1"]["lab_done"] is True


def test_lab_all_nine_modules_gradeable():
    for e in uni.load_manifest()["modules"]:
        mod = uni.load_module(e["id"])
        answers = {q["id"]: q["any_of"][0] for q in mod["lab"]["task"]["questions"]}
        if mod["lab"].get("executable"):
            answers["files"] = mod["lab"]["executable"]["solution"]["files"]
        r = uni.run_lab(e["id"], "bulk", submission=answers)
        assert r["passed"] is True, (e["id"], r["checks"])


def test_executable_lab_rejects_wrong_artifact_and_executes_math():
    mod = uni.load_module("4")
    spec = mod["lab"]["executable"]
    good = spec["solution"]["files"]["route-plan.json"]
    answers = {q["id"]: q["any_of"][0] for q in mod["lab"]["task"]["questions"]}
    r_ok = uni.run_lab("4", "exec", submission={"files": {"route-plan.json": good}, **answers})
    assert r_ok["passed"] is True
    bad_doc = json.loads(good); bad_doc["total_tokens"] = 999999
    r_bad = uni.run_lab("4", "exec", submission={"files": {"route-plan.json": json.dumps(bad_doc)}, **answers})
    assert r_bad["passed"] is False
    assert any("computed total" in c.get("detail", "") for c in r_bad["checks"])


def test_capstone_fixture_creates_no_deliverables():
    fixture = uni.capstone_fixture()
    dl = fixture / "org" / "deliverable"
    assert dl.is_dir() and not any(dl.iterdir()), "verifier must not create deliverables"


def test_capstone_fresh_fixture_fails():
    fixture = uni.capstone_fixture()
    ev = uni.capstone_verify(fixture)
    assert ev["passed"] is False


def test_capstone_completed_fixture_passes_and_removal_fails():
    fixture = uni.capstone_fixture()
    _complete_capstone(fixture)
    ev = uni.capstone_verify(fixture)
    assert ev["passed"] is True, ev["checks"]
    (fixture / "org" / "approval-log.json").unlink()
    assert uni.capstone_verify(fixture)["passed"] is False


def test_capstone_rejects_live_home_forged_and_traversal():
    assert uni.capstone_verify(Path.home() / ".hermes")["passed"] is False
    assert "refused" in uni.capstone_verify(Path.home() / ".hermes")["note"]
    forged = Path(tempfile.mkdtemp(prefix="capstone-forged-"))
    assert uni.capstone_verify(forged)["passed"] is False
    assert uni.capstone_verify(Path(tempfile.gettempdir()) / ".." / "etc")["passed"] is False


def test_capstone_rejects_symlink_escape():
    root = uni._uni_root()
    link = Path(tempfile.mkdtemp(prefix="uni-capstone-", dir=str(root)))
    os.symlink(str(Path.home() / ".hermes" / "nerve"), str(link / "org"))
    ev = uni.capstone_verify(link)
    assert ev["passed"] is False


def test_module_completion_flow():
    for lesson in uni.load_module("9")["lessons"]:
        uni.record_lesson("cap", "9", lesson["title"])
    assert not uni.module_complete("cap", "9")
    mod = uni.load_module("9")
    answers = {q["id"]: q["any_of"][0] for q in mod["lab"]["task"]["questions"]}
    answers["files"] = mod["lab"]["executable"]["solution"]["files"]
    uni.run_lab("9", "cap", submission=answers)
    rubric_answer = " ".join(kw for g in mod["check"]["rubric"] for kw in g["any_of"])
    ok, _ = uni.grade_check("cap", "9", rubric_answer)
    assert ok
    assert uni.module_complete("cap", "9")
    assert not uni.course_complete("cap")


def test_reset_and_export():
    uni.record_lesson("r", "1", "L1")
    assert uni.load_progress("r")["modules"]
    assert uni.reset_progress("r")["reset"] is True
    assert uni.load_progress("r")["modules"] == {}
    exported = uni.export_progress("r")
    assert exported["exported_at"] is not None
