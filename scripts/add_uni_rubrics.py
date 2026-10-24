#!/usr/bin/env python3
"""Inject scored check rubrics and structured lab tasks into the 9 modules."""
import json
from pathlib import Path

CD = Path("/home/user/Documents/LargeProjects/nerve-v0.4.0/hermes_nerve/university/content")

RUBRICS = {
 "1": [
  {"id": "hermes_owns_completion", "any_of": ["hermes"]},
  {"id": "nerve_advises_not_executes", "any_of": ["nerve", "supervise", "supervision"]},
  {"id": "evidence_beats_assertion", "any_of": ["evidence", "verified", "proof"]}],
 "2": [
  {"id": "profiles_names", "any_of": ["operator", "fat cat", "lean", "marie kondo", "legacy", "profile"]},
  {"id": "precedence", "any_of": ["profile.json", "sidecar", "override", "precedence"]},
  {"id": "backup_rollback", "any_of": ["backup", "rollback", "restore", "snapshot"]}],
 "3": [
  {"id": "scoped_roles", "any_of": ["role", "owner", "scope"]},
  {"id": "isolation", "any_of": ["worktree", "workspace", "isolat", "separate"]},
  {"id": "evidence_handoff", "any_of": ["handoff", "evidence", "acceptance", "gate"]}],
 "4": [
  {"id": "hosted_vs_local", "any_of": ["jev", "d1", "laya", "openjev", "local", "hosted"]},
  {"id": "shadow_not_savings", "any_of": ["shadow", "not billed", "comparison", "paired"]},
  {"id": "outage_fallback", "any_of": ["fallback", "outage", "health", "fail"]}],
 "5": [
  {"id": "locked_dod", "any_of": ["dod", "definition of done", "criteria", "locked"]},
  {"id": "checkpoint_frontier", "any_of": ["checkpoint", "frontier", "resume", "capsule"]},
  {"id": "verified_not_claimed", "any_of": ["evidence", "verified", "test", "proof"]}],
 "6": [
  {"id": "selective_admission", "any_of": ["pin", "admit", "defer", "suppress", "admission", "selective"]},
  {"id": "bus_scope_isolation", "any_of": ["scope", "isolat", "profile", "authoriz"]},
  {"id": "bus_down_fallback", "any_of": ["fallback", "unavailable", "disabled", "bus-down", "bus down"]}],
 "7": [
  {"id": "risk_classes", "any_of": ["routine", "consequential", "prohibited", "uncertain", "risk"]},
  {"id": "effect_bound", "any_of": ["effect", "bound", "identity", "scope", "nonce"]},
  {"id": "useful_explanation", "any_of": ["explain", "why", "impact", "alternative", "confidence"]}],
 "8": [
  {"id": "evidence_ledger", "any_of": ["evidence", "ledger", "reproduc", "receipt"]},
  {"id": "premature_block", "any_of": ["premature", "surrender", "blocked", "alternatives", "recover"]},
  {"id": "health_vs_decision", "any_of": ["health", "decision", "endpoint", "real", "inference"]}],
 "9": [
  {"id": "disposable_env", "any_of": ["disposable", "isolated", "fixture", "sandbox"]},
  {"id": "verified_artifacts", "any_of": ["artifact", "verif", "evidence", "dod"]},
  {"id": "truthful_reporting", "any_of": ["truth", "honest", "limitation", "report", "rollback"]}],
}

LABS = {
 "1": [("q1", "Who owns canonical task completion? (hermes/nerve/operator)", ["hermes"],
        "Hermes executes and owns canonical lifecycle; Nerve supervises."),
       ("q2", "Who approves consequential tool actions? (hermes/nerve/operator)", ["operator"],
        "The operator owns policy and consequential decisions."),
       ("q3", "May Never bypass Hermes safeguards? (yes/no)", ["no"],
        "Nerve advises and verifies but never bypasses Hermes security.")],
 "2": [("q1", "Which file overrides config.yaml for profile settings?", ["profile.json", "sidecar"],
        "The profile sidecar at $HERMES_HOME/nerve/profile.json takes precedence."),
       ("q2", "Which profile is default for NEW installs? (operator/legacy)", ["operator"],
        "New setups default to Operator; existing Legacy stays Legacy."),
       ("q3", "What must exist before changing a profile? (backup/rollback/either)", ["backup", "rollback", "snapshot"],
        "A restorable backup/rollback point is required before profile changes.")],
 "3": [("q1", "May two workers own the same mutable artifact? (yes/no)", ["no"],
        "No two workers may own the same mutable artifact."),
       ("q2", "Name one isolation mechanism for parallel workers.", ["worktree", "workspace", "isolat"],
        "Worktrees or separate isolated workspaces prevent shared-tree races."),
       ("q3", "What must each delegated task end with? (evidence/none)", ["evidence", "acceptance", "gate", "verif"],
        "Each task needs a tangible output plus a verification gate.")],
 "4": [("q1", "In shadow mode, is the paired comparison billed as a decision? (yes/no)", ["no"],
        "Shadow comparisons are observations, not billed decision savings."),
       ("q2", "What provenance marks a local D1 answer?", ["local_only", "local only", "localonly"],
        "Local calls carry LOCAL_ONLY provenance."),
       ("q3", "Does an HTTP health check prove inference works? (yes/no)", ["no"],
        "Health is not a typed inference validation.")],
 "5": [("q1", "May a stale PASS be reused after a file changes? (yes/no)", ["no"],
        "Stale PASS must be refused; evidence must be fresh."),
       ("q2", "On resume, which criteria are re-run? (all/unverified)", ["unverified"],
        "Resume only the unverified frontier, not completed criteria."),
       ("q3", "Is an agent's 'done' message proof of completion? (yes/no)", ["no"],
        "Claims are not proof; measured acceptance criteria decide.")],
 "6": [("q1", "Which admission states does Selective Context use? (pin/admit/defer/suppress)", ["pin", "admit", "defer", "suppress"],
        "PIN/ADMIT/DEFER/SUPPRESS gate memory, tools and skills."),
       ("q2", "May profile C read a message addressed to profile B? (yes/no)", ["no"],
        "Cross-profile reads are denied without authorization."),
       ("q3", "What happens to Nerve core when the bus is down? (crash/fallback)", ["fallback", "unavailable", "disabled", "continues"],
        "A disabled/unavailable bus falls back safely; core is unaffected.")],
 "7": [("q1", "Which class requires explicit human approval? (routine/consequential)", ["consequential"],
        "Consequential operations need explained, effect-scoped approval."),
       ("q2", "Does the same approval key cover a changed push ref? (yes/no)", ["no"],
        "Approvals bind to the exact effect; changed effects need new approval."),
       ("q3", "Is 'p(ALLOW) 0.82 < 0.90' an acceptable approval reason? (yes/no)", ["no"],
        "Confidence numbers are never the explanation; explain action/effect/risk.")],
 "8": [("q1", "A research answer is plausible but stale. What is required?", ["evidence", "live", "fresh", "grounding"],
        "External live evidence and a factual referee are required."),
       ("q2", "A worker claims blocked before testing alternatives. Verdict? (escalate/continue)", ["continue", "refute"],
        "Bounded useful recovery remains: continue or refute the false block."),
       ("q3", "A model endpoint is healthy but never exercised for decisions. Proven?", ["no"],
        "Untested decision paths are not proven by health alone.")],
 "9": [("q1", "Where must the capstone organization be built? (disposable/live)", ["disposable", "fixture", "sandbox", "isolated"],
        "The capstone runs only in a disposable environment."),
       ("q2", "Who creates the capstone deliverables? (learner/verifier)", ["learner"],
        "The learner produces artifacts; the verifier only inspects them."),
       ("q3", "Should results include limitations truthfully? (yes/no)", ["yes"],
        "Truthful reporting of result and limitations is mandatory.")],
}

for mid in [str(i) for i in range(1, 10)]:
    p = CD / f"module{mid}.json"
    mod = json.loads(p.read_text(encoding="utf-8"))
    mod["check"]["rubric"] = RUBRICS[mid]
    task_q = [{"id": qid, "q": q, "any_of": anys, "why": why} for qid, q, anys, why in LABS[mid]]
    mod["lab"]["task"] = {
        "intro": "Answer the scenario questions; the engine verifies them deterministically "
                 "in a disposable fixture. Your answers are never executed as commands.",
        "questions": task_q}
    p.write_text(json.dumps(mod, indent=1) + "\n", encoding="utf-8")
    print(f"module{mid}: rubric groups={len(RUBRICS[mid])} lab questions={len(task_q)}")
print("DONE")
