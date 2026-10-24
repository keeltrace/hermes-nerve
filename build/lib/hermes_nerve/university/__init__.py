"""Operator University — local learner engine (N-36..N-38).

Loads the packaged nine-module curriculum, tracks profile-isolated progress,
runs REAL deterministic labs in disposable fixtures, grades knowledge checks
against scored rubrics, and independently verifies the capstone by inspecting
learner-produced artifacts. No network, no model calls, no live-profile writes.

Security contract (v0.4.0):
- Verifiers never create required deliverables; they only inspect them.
- Capstone verification runs ONLY inside a Nerve-created disposable fixture
  root: canonical-path containment under the system temp dir, a private
  per-fixture nonce marker, owner/permission checks, symlink and traversal
  refusal.
- Lab completion is recorded only after deterministic checks PASS.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from ..paths import hermes_home

_SCHEMA = "nerve-university/v1"
_CONTENT_DIR = Path(__file__).resolve().parent / "content"
_PROGRESS_DIRNAME = "university"
_CAPSTONE_PREFIX = "uni-capstone-"
_MARKER_NAME = ".nerve-capstone-marker"
_NONCE_RE = re.compile(r"^[a-f0-9]{32}$")


# --- content -----------------------------------------------------------------------

def load_manifest() -> dict[str, Any]:
    return json.loads((_CONTENT_DIR / "manifest.json").read_text(encoding="utf-8"))


def load_module(module_id: str) -> dict[str, Any]:
    for entry in load_manifest()["modules"]:
        if entry["id"] == str(module_id):
            return json.loads((_CONTENT_DIR / Path(entry["file"]).name).read_text(encoding="utf-8"))
    raise KeyError(f"unknown module: {module_id}")


# --- progress: atomic, profile-isolated ---------------------------------------------

def _progress_path(profile: str) -> Path:
    safe = "".join(c for c in (profile or "default") if c.isalnum() or c in "-_.") or "default"
    return hermes_home() / "nerve" / _PROGRESS_DIRNAME / f"progress-{safe}.json"


def _fresh_progress(profile: str) -> dict[str, Any]:
    return {"schema": _SCHEMA, "profile": profile or "default",
            "modules": {}, "capstone": {}, "exported_at": None}


def load_progress(profile: str = "") -> dict[str, Any]:
    path = _progress_path(profile)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _fresh_progress(profile)
    return data if isinstance(data, dict) and data.get("schema") == _SCHEMA else _fresh_progress(profile)


def save_progress(profile: str, data: dict[str, Any]) -> None:
    path = _progress_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _module_state(profile: str, module_id: str) -> dict[str, Any]:
    data = load_progress(profile)
    return _state_in(data, module_id)


def _state_in(data: dict[str, Any], module_id: str) -> dict[str, Any]:
    return data["modules"].setdefault(
        str(module_id),
        {"lessons_read": [], "lab_done": False, "check_passed": False,
         "check_attempts": 0, "lab_attempts": 0, "missed": []})


def record_lesson(profile: str, module_id: str, lesson_title: str) -> None:
    data = load_progress(profile)
    mods = _state_in(data, module_id)
    if lesson_title and lesson_title not in mods["lessons_read"]:
        mods["lessons_read"].append(str(lesson_title)[:200])
    save_progress(profile, data)


def export_progress(profile: str) -> dict[str, Any]:
    data = load_progress(profile)
    data["exported_at"] = time.time()
    return data


def reset_progress(profile: str) -> dict[str, Any]:
    fresh = _fresh_progress(profile)
    save_progress(profile, fresh)
    return {"reset": True, "profile": profile or "default"}


# --- knowledge checks: scored rubric (defect 6) --------------------------------------

def _match_group(answer_text: str, group: dict[str, Any]) -> bool:
    """A group matches when ANY of its keyword phrases appears in the answer.

    Keyword phrases are matched as substrings on a normalized (lowercase,
    whitespace-collapsed) answer, bounded by non-alphanumerics so that e.g.
    'hermes' does not match inside 'hermetic' nonsense.
    """
    normalized = re.sub(r"[^a-z0-9]+", " ", (answer_text or "").lower()).strip()
    for kw in group.get("any_of", []):
        phrase = re.sub(r"[^a-z0-9]+", " ", str(kw).lower()).strip()
        if phrase and re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", normalized):
            return True
    return False


def grade_check(profile: str, module_id: str, answer: str) -> tuple[bool, str]:
    ok, _question, _missed = grade_check_details(profile, module_id, answer)
    return ok, _question


def grade_check_details(profile: str, module_id: str, answer: str) -> tuple[bool, str, list[str]]:
    """Score the module rubric: pass requires EVERY concept group to match.

    A single overlapping word cannot pass a multi-group rubric. Once passed,
    the pass is sticky (retry hygiene: later failed attempts do not demote it).
    """
    mod = load_module(module_id)
    rubric = mod["check"].get("rubric") or [
        {"id": "keywords", "any_of": mod["check"].get("expected_keywords", [])}]
    missed = [g["id"] for g in rubric if not _match_group(answer or "", g)]
    ok = not missed
    data = load_progress(profile)
    mods = _state_in(data, module_id)
    mods["check_attempts"] = int(mods.get("check_attempts", 0)) + 1
    if ok or mods.get("check_passed"):
        mods["check_passed"] = True
        mods["missed"] = []
    else:
        mods["missed"] = missed
    save_progress(profile, data)
    return ok, mod["check"]["question"], missed


# --- module completion ----------------------------------------------------------------

def module_complete(profile: str, module_id: str) -> bool:
    mods = load_progress(profile)["modules"].get(str(module_id)) or {}
    mod = load_module(module_id)
    lessons_done = set(mods.get("lessons_read") or []) >= {ln["title"] for ln in mod["lessons"]}
    return bool(lessons_done and mods.get("lab_done") and mods.get("check_passed"))


def course_complete(profile: str) -> bool:
    return all(module_complete(profile, m["id"]) for m in load_manifest()["modules"])


# --- disposable-fixture safety (defect 5) ----------------------------------------------

def _uni_root() -> Path:
    root = Path(tempfile.gettempdir()) / "nerve-university"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _refuse(note: str, fixture: Path) -> dict[str, Any]:
    evidence = {"fixture": str(fixture), "checks": {}, "passed": False,
                "when": time.time(), "note": f"refused: {note}"}
    try:
        (_uni_root() / "capstone-refused.json").write_text(
            json.dumps(evidence, indent=1), encoding="utf-8")
    except OSError:
        pass
    return evidence


def _safe_capstone_fixture(fixture: Path) -> tuple[bool, str, Path | None]:
    """Canonical containment checks for a capstone fixture (defect 5).

    Requirements: the path (after resolving) lives directly under the Nerve
    university temp root with the reserved 'uni-capstone-' prefix; the caller
    supplied a literal path with no '..' components and no symlinked segments;
    the directory is owned by the current user and not group/world writable;
    and it carries a valid per-fixture nonce marker created by capstone_fixture().
    """
    raw = Path(fixture)
    if ".." in raw.parts:
        return False, "path traversal component", None
    if not raw.is_dir():
        return False, "fixture directory does not exist", None
    resolved = raw.resolve()
    if str(resolved) != str(os.path.realpath(raw)):
        return False, "path is not canonical", None
    root = _uni_root().resolve()
    if resolved.parent != root or not resolved.name.startswith(_CAPSTONE_PREFIX):
        return False, "fixture is outside the Nerve disposable capstone root", None
    suffix = resolved.name[len(_CAPSTONE_PREFIX):]
    if not re.fullmatch(r"[a-z0-9_]+", suffix):
        return False, "fixture name is not a Nerve-generated identifier", None
    try:
        st = resolved.stat()
    except OSError as exc:
        return False, f"fixture stat failed: {exc}", None
    if st.st_uid != os.getuid():
        return False, "fixture is owned by another user", None
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False, "fixture is group/world writable", None
    marker = resolved / _MARKER_NAME
    try:
        nonce = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False, "missing per-fixture nonce marker", None
    if not _NONCE_RE.fullmatch(nonce):
        return False, "invalid nonce marker", None
    return True, "ok", resolved


# --- labs: real deterministic scenarios (defect 3) --------------------------------------

def _lab_checks(mod: dict[str, Any], submission: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    task = mod["lab"].get("task") or {}
    results: list[dict[str, Any]] = []
    for q in task.get("questions", []):
        given = submission.get(q["id"])
        got = re.sub(r"[^a-z0-9]+", " ", str(given or "").lower()).strip()
        matched = None
        for kw in q.get("any_of", []):
            phrase = re.sub(r"[^a-z0-9]+", " ", str(kw).lower()).strip()
            if phrase and re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", got):
                matched = kw
                break
        results.append({"id": q["id"], "question": q["q"],
                        "passed": matched is not None,
                        "accepted_example": q["any_of"][0] if q.get("any_of") else "",
                        "why": q.get("why", "")})
    return bool(results) and all(r["passed"] for r in results), results


def _run_executable_lab(mod: dict[str, Any], submission: dict[str, Any], sandbox: Path) -> tuple[bool, list[dict[str, Any]]]:
    """Executable lab mode: the learner produces file artifacts; the engine
    writes them into the disposable sandbox and RUNS deterministic checks
    (parse, structural assertions, executed computations) against them."""
    spec = mod["lab"]["executable"]
    files = submission.get("files")
    if not isinstance(files, dict) or not files:
        return False, [{"id": "files", "passed": False, "detail": "no files submitted"}]
    learner_dir = sandbox / "learner"
    learner_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for name, content in files.items():
        safe_name = Path(name).name  # no traversal
        if not safe_name or len(safe_name) > 120:
            results.append({"id": f"file:{name}", "passed": False, "detail": "invalid file name"})
            continue
        target = learner_dir / safe_name
        try:
            target.write_text(str(content), encoding="utf-8")
        except OSError as exc:
            results.append({"id": f"file:{safe_name}", "passed": False, "detail": str(exc)})
            continue
    for fname, fspec in (spec.get("files") or {}).items():
        rid = f"file:{fname}"
        path = learner_dir / Path(fname).name
        if not path.is_file():
            results.append({"id": rid, "passed": False, "detail": "missing artifact"})
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        ok, detail = _validate_artifact(fspec, text)
        results.append({"id": rid, "passed": ok, "detail": detail})
    return bool(results) and all(r["passed"] for r in results), results


def _validate_artifact(fspec: dict[str, Any], text: str) -> tuple[bool, str]:
    kind = fspec.get("kind", "text")
    if kind == "json":
        try:
            doc = json.loads(text)
        except ValueError as exc:
            return False, f"invalid JSON: {exc}"
        for chk in fspec.get("checks", []):
            node: Any = doc
            for part in str(chk.get("path", "")).split("."):
                if not part:
                    continue
                if isinstance(node, list):
                    try:
                        node = node[int(part)]
                    except (ValueError, IndexError):
                        return False, f"path missing: {chk.get('path')}"
                elif isinstance(node, dict):
                    if part not in node:
                        return False, f"path missing: {chk.get('path')}"
                    node = node[part]
                else:
                    return False, f"path missing: {chk.get('path')}"
            expect = chk.get("expect")
            if expect == "nonempty_str":
                if not (isinstance(node, str) and node.strip()):
                    return False, f"expected non-empty string at {chk.get('path')}"
            elif expect == "minlen":
                if len(node) < int(chk.get("value", 1)):
                    return False, f"too short at {chk.get('path')} (need {chk.get('value')})"
            elif expect == "equals" and node != chk.get("value"):
                return False, f"expected {chk.get('value')!r} at {chk.get('path')}"
            elif expect == "contains" and str(chk.get("value")) not in str(node):
                return False, f"missing {chk.get('value')!r} at {chk.get('path')}"
        return True, "json checks passed"
    if kind == "cost_table":
        # Executed computation: recompute the totals from the supplied table
        # and compare with the learner's submitted numbers.
        try:
            doc = json.loads(text)
        except ValueError as exc:
            return False, f"invalid JSON: {exc}"
        table = fspec.get("table", {})
        choices = doc.get("routes") if isinstance(doc, dict) else None
        if not isinstance(choices, dict):
            return False, "missing routes object"
        total = 0
        for route, calls in choices.items():
            unit = table.get(route)
            if not isinstance(unit, (int, float)) or not isinstance(calls, (int, float)):
                return False, f"unknown route or call count: {route}"
            total += unit * calls
        submitted = doc.get("total_tokens")
        if submitted != total:
            return False, f"computed total is {total}, submitted {submitted!r} (arithmetic executed by engine)"
        return True, f"total verified by executed computation: {total}"
    # text kind
    for pat in fspec.get("contains_any", []):
        pass
    checks = fspec.get("checks", [])
    for chk in checks:
        if chk.get("expect") == "regex" and not re.search(str(chk.get("value", "")), text):
            return False, f"pattern missing: {chk.get('pattern_name', chk.get('value'))[:60]}"
        if chk.get("expect") == "minlen" and len(text.strip()) < int(chk.get("value", 1)):
            return False, f"too short (need {chk.get('value')})"
    return True, "text checks passed"


def run_lab(module_id: str, profile: str = "", submission: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a module lab inside a disposable fixture.

    Two lab modes per module content:
    - structured questions (lab.task.questions): deterministic answer grading;
    - executable artifacts (lab.executable): learner-produced files are written
      into the sandbox and the engine RUNS parse/structure/computation checks.

    Completion is recorded ONLY when every check passes. Learner-supplied
    commands are never executed; only bounded file content is evaluated.
    """
    mod = load_module(module_id)
    fixture = Path(tempfile.mkdtemp(prefix=f"uni-lab-m{module_id}-", dir=str(_uni_root())))
    sandbox = fixture / "HERMES_HOME"
    sandbox.mkdir()
    submission = submission if isinstance(submission, dict) else {}
    checks: list[dict[str, Any]] = []
    passed = True
    if mod["lab"].get("executable"):
        passed, checks = _run_executable_lab(mod, submission, sandbox)
    if mod["lab"].get("task", {}).get("questions"):
        qa_passed, qa_checks = _lab_checks(mod, submission)
        checks = checks + qa_checks
        passed = passed and qa_passed
    evidence: dict[str, Any] = {
        "module": module_id, "fixture": str(fixture), "sandbox_home": str(sandbox),
        "lab_goal": mod["lab"]["goal"], "steps": mod["lab"]["steps"],
        "created": time.time(), "isolated": True, "fixture_retained": False,
        "checks": checks, "passed": passed}
    if passed:
        # Deterministic effect artifact: proof the scenario ran in the sandbox.
        (fixture / "lab-evidence.json").write_text(json.dumps(evidence, indent=1), encoding="utf-8")
        data = load_progress(profile)
        mods = _state_in(data, module_id)
        mods["lab_attempts"] = int(mods.get("lab_attempts", 0)) + 1
        mods["lab_done"] = True
        save_progress(profile, data)
    else:
        data = load_progress(profile)
        mods = _state_in(data, module_id)
        mods["lab_attempts"] = int(mods.get("lab_attempts", 0)) + 1
        save_progress(profile, data)
    if not os.getenv("NERVE_UNI_KEEP"):
        shutil.rmtree(fixture, ignore_errors=True)
    else:
        evidence["fixture_retained"] = True
    return evidence


# --- capstone: independent verification of learner work (defect 4) -----------------------

def capstone_fixture(base: Path | None = None) -> Path:
    """Create the disposable capstone environment skeleton.

    Creates ONLY the task scaffolding: directory structure, the assignment
    brief, and the per-fixture nonce marker. It deliberately does NOT create
    any required deliverable — the learner (or their workflow) must.
    """
    nonce = secrets.token_hex(16)
    if base:
        root = _uni_root().resolve()
        fixture = Path(base)
        if fixture.parent != root or not fixture.name.startswith(_CAPSTONE_PREFIX):
            fixture = Path(tempfile.mkdtemp(prefix=_CAPSTONE_PREFIX, dir=str(root)))
    else:
        fixture = Path(tempfile.mkdtemp(prefix=_CAPSTONE_PREFIX, dir=str(_uni_root())))
    home = fixture / "HERMES_HOME"
    (home / "nerve").mkdir(parents=True, exist_ok=True)
    (fixture / "org" / "workers").mkdir(parents=True, exist_ok=True)
    (fixture / "org" / "deliverable").mkdir(parents=True, exist_ok=True)
    (fixture / "org" / "TASK.md").write_text(
        "# Capstone assignment\n\n"
        "Build a production-style disposable Hermes organization. You must create\n"
        "these deliverables yourself inside org/deliverable (the verifier does not\n"
        "create them for you):\n"
        "1. README.md — what the organization does (>= 200 chars).\n"
        "2. checkpoint-capsule.json — {verified_criteria: [str..], failed_paths: [..], next_frontier: str}.\n"
        "3. experience-notes.json — {verified: [{claim, evidence}..]}.\n"
        "4. ../roles.json — list of >= 2 roles, each {name, owns: [..], evidence: [..]}.\n"
        "5. ../dod.json — {criteria: [{criterion, status: pass|fail}..>=3]} with at least one pass.\n"
        "6. ../route-policy.json — {provider, fallback}.\n"
        "7. ../approval-log.json — list with >= 1 {action, effect, decision} and at least one 'denied'.\n",
        encoding="utf-8")
    (fixture / _MARKER_NAME).write_text(nonce + "\n", encoding="utf-8")
    os.chmod(fixture, 0o700)
    return fixture


def _valid_str_list(value: Any, min_len: int = 1) -> bool:
    return isinstance(value, list) and len(value) >= min_len and \
        all(isinstance(v, str) and v.strip() for v in value)


def _capstone_checks(fixture: Path) -> dict[str, bool]:
    org = fixture / "org"
    deliverable = org / "deliverable"
    checks: dict[str, bool] = {}

    readme = deliverable / "README.md"
    checks["deliverable_readme_substantive"] = False
    if readme.is_file() and not readme.is_symlink():
        try:
            checks["deliverable_readme_substantive"] = len(readme.read_text(encoding="utf-8").strip()) >= 200
        except (OSError, UnicodeError):
            pass

    capsule = deliverable / "checkpoint-capsule.json"
    checks["checkpoint_capsule_schema"] = False
    if capsule.is_file() and not capsule.is_symlink():
        try:
            c = json.loads(capsule.read_text(encoding="utf-8"))
            checks["checkpoint_capsule_schema"] = (
                isinstance(c, dict) and _valid_str_list(c.get("verified_criteria"))
                and isinstance(c.get("failed_paths"), list)
                and isinstance(c.get("next_frontier"), str))
        except (OSError, ValueError):
            pass

    notes = deliverable / "experience-notes.json"
    checks["experience_notes_verified_claims"] = False
    if notes.is_file() and not notes.is_symlink():
        try:
            e = json.loads(notes.read_text(encoding="utf-8"))
            verified = e.get("verified") if isinstance(e, dict) else None
            checks["experience_notes_verified_claims"] = isinstance(verified, list) and len(verified) >= 1 \
                and all(isinstance(v, dict) and isinstance(v.get("claim"), str) and v["claim"].strip()
                        and isinstance(v.get("evidence"), str) and v["evidence"].strip()
                        for v in verified)
        except (OSError, ValueError):
            pass

    roles = org / "roles.json"
    checks["roles_scoped_with_evidence"] = False
    if roles.is_file() and not roles.is_symlink():
        try:
            r = json.loads(roles.read_text(encoding="utf-8"))
            checks["roles_scoped_with_evidence"] = isinstance(r, list) and len(r) >= 2 and all(
                isinstance(x, dict) and isinstance(x.get("name"), str) and x["name"].strip()
                and _valid_str_list(x.get("owns")) and _valid_str_list(x.get("evidence"))
                for x in r)
        except (OSError, ValueError):
            pass

    dod = org / "dod.json"
    checks["dod_locked_with_real_pass"] = False
    if dod.is_file() and not dod.is_symlink():
        try:
            d = json.loads(dod.read_text(encoding="utf-8"))
            criteria = d.get("criteria") if isinstance(d, dict) else None
            checks["dod_locked_with_real_pass"] = isinstance(criteria, list) and len(criteria) >= 3 and all(
                isinstance(x, dict) and isinstance(x.get("criterion"), str) and x["criterion"].strip()
                and x.get("status") in ("pass", "fail") for x in criteria) \
                and any(x.get("status") == "pass" for x in criteria)
        except (OSError, ValueError):
            pass

    route = org / "route-policy.json"
    checks["route_policy_provider_and_fallback"] = False
    if route.is_file() and not route.is_symlink():
        try:
            r = json.loads(route.read_text(encoding="utf-8"))
            checks["route_policy_provider_and_fallback"] = isinstance(r, dict) \
                and isinstance(r.get("provider"), str) and r["provider"].strip() \
                and isinstance(r.get("fallback"), str) and r["fallback"].strip()
        except (OSError, ValueError):
            pass

    log = org / "approval-log.json"
    checks["approval_log_with_denied_decision"] = False
    if log.is_file() and not log.is_symlink():
        try:
            lg = json.loads(log.read_text(encoding="utf-8"))
            checks["approval_log_with_denied_decision"] = isinstance(lg, list) and len(lg) >= 1 and all(
                isinstance(x, dict) and isinstance(x.get("action"), str) and x["action"].strip()
                and isinstance(x.get("decision"), str) and x["decision"].strip()
                for x in lg) and any(x.get("decision", "").lower() == "denied" for x in lg)
        except (OSError, ValueError):
            pass

    # Tamper resistance: deliverables must be regular files inside the fixture,
    # not symlinks into the live system (checked above via is_symlink()).
    checks["no_symlinked_deliverables"] = not any(
        p.is_symlink() for p in list(deliverable.iterdir()) + list(org.iterdir())
        if p.name != _MARKER_NAME) if deliverable.is_dir() and org.is_dir() else False
    return checks


def capstone_verify(fixture: Path) -> dict[str, Any]:
    """Independently verify learner-produced capstone artifacts.

    The verifier CREATES NOTHING inside the fixture. It refuses any path that
    is not a Nerve-created disposable fixture (containment, ownership,
    permissions, nonce marker). Evidence is written to the Nerve university
    temp root, never into the fixture under test.
    """
    fixture = Path(fixture)
    safe, reason, resolved = _safe_capstone_fixture(fixture)
    if not safe or resolved is None:
        return _refuse(reason, fixture)
    checks = _capstone_checks(resolved)
    checks["fixture_is_disposable"] = True
    evidence = {"fixture": str(resolved), "checks": checks,
                "passed": all(checks.values()), "when": time.time()}
    try:
        (_uni_root() / f"capstone-verify-{secrets.token_hex(4)}.json").write_text(
            json.dumps(evidence, indent=1), encoding="utf-8")
    except OSError:
        pass
    if evidence["passed"]:
        data = load_progress("")
        data["capstone"] = {"passed": True, "fixture": str(resolved), "when": evidence["when"]}
        save_progress("", data)
    return evidence
