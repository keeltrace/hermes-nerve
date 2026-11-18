"""ESCALATE parking and human-gate classification (card t_88972356, part B).

Two behaviours are pinned here:

* an ESCALATE completion verdict with unmet criteria parks the card
  ``needs_input`` instead of leaving it ``running`` for the dispatcher to
  re-dispatch into the same verdict;
* a card whose title/body is a money / vendor / product-direction decision is
  classified and parked before a DoD contract is bound.

No test writes a real board: the adapter's ``_module`` is replaced with a fake
``kanban_db`` that records ``block_task`` calls. New symbols are reached through
their modules at call time, so the "unchanged behaviour" tests in this file
also run (and pass) against the pre-change tree.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from hermes_nerve.work import autobind, hooks, runtime
from hermes_nerve.work import supervisor as supervisor_mod
from hermes_nerve.work.kanban_adapter import CanonicalKanbanAdapter
from hermes_nerve.work.models import CompletionVerdict, RunIdentity
from hermes_nerve.work.supervisor import CardSupervisor

TASK_ID = "t_park"
RUN_ID = 7
REASON = "Reflex batched completion verdict {value}."
MISSING = ("DOD-01", "DOD-02")


class Accept:
    value = "ACCEPT"
    confidence = 1.0
    receipt_id = "local"


class FakeBoard:
    """Stand-in for ``hermes_cli.kanban_db``: records, never touches a board."""

    def __init__(self, result=True, raises: Exception | None = None):
        self.result = result
        self.raises = raises
        self.calls: list[tuple[str, dict]] = []
        self.events: list[str] = []
        self.conn = object()

    @contextlib.contextmanager
    def connect_closing(self):
        self.events.append("open")
        try:
            yield self.conn
        finally:
            self.events.append("close")

    def block_task(self, conn, task_id, **kwargs):
        assert conn is self.conn
        self.calls.append((task_id, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


def _install(monkeypatch, board: FakeBoard) -> FakeBoard:
    monkeypatch.setattr(CanonicalKanbanAdapter, "_module", lambda self: board)
    return board


@pytest.fixture
def board(monkeypatch):
    return _install(monkeypatch, FakeBoard())


def _verdict(value: str, missing=MISSING) -> CompletionVerdict:
    return CompletionVerdict(
        allow=False, value=value, confidence=0.4,
        reason=REASON.format(value=value), receipt_id="r-1", missing_criteria=missing,
    )


@pytest.fixture
def run(tmp_path, monkeypatch):
    """A bound, enabled supervised run with no controller dispatcher."""
    runtime.set_tool_dispatcher(None)
    runtime.set_supervisor_for_tests(None, enabled_value=False)
    runtime.configure(enabled=True, mode="advisory", store_path=str(tmp_path / "work.db"))
    sup = CardSupervisor(store_path=tmp_path / "work.db", mode="advisory")
    bound = sup.bind_contract(
        task_id=TASK_ID,
        goal="finish safely",
        criteria=[
            {"id": "DOD-01", "description": "first thing is done", "estimated_tokens": 1000},
            {"id": "DOD-02", "description": "second thing is done", "estimated_tokens": 1000},
        ],
        reserve_tokens=1000,
        preflight_result=Accept(),
    )
    ident = RunIdentity(TASK_ID, RUN_ID, bound["contract"]["contract_hash"], "claim", "worker")
    sup.bind_run(ident)
    runtime.set_supervisor_for_tests(sup, enabled_value=True)
    for key, value in {
        "HERMES_KANBAN_TASK": TASK_ID,
        "HERMES_KANBAN_TASK_ID": TASK_ID,
        "HERMES_KANBAN_RUN_ID": str(RUN_ID),
        "HERMES_KANBAN_CLAIM_LOCK": "claim",
        "HERMES_KANBAN_CLAIM_IDENTITY": "claim",
        "HERMES_KANBAN_WORKER_ID": "worker",
        "HERMES_NERVE_DOD_HASH": ident.contract_hash,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(hooks, "_owns_kanban_terminal_authority", lambda: True)
    yield SimpleNamespace(sup=sup, ident=ident)
    runtime.set_tool_dispatcher(None)
    runtime.set_supervisor_for_tests(None, enabled_value=False)


def _complete_gate(run, monkeypatch, verdict):
    monkeypatch.setattr(run.sup, "verify_completion", lambda identity, proposal=None: verdict)
    return hooks.pre_tool_call("kanban_complete", {"summary": "done"}, task_id=TASK_ID, session_id="s")


def _pre_verify(run, monkeypatch, verdict):
    monkeypatch.setattr(run.sup, "verify_completion", lambda identity, proposal=None: verdict)
    return hooks.pre_verify(task_id=TASK_ID, session_id="s", final_response="done")


# --------------------------- 1-4: reason builder ---------------------------

def test_park_reason_is_empty_with_zero_criteria():
    assert supervisor_mod.escalation_park_reason(_verdict("ESCALATE", ()), TASK_ID) == ""


def test_park_reason_names_both_criteria_on_one_line():
    reason = supervisor_mod.escalation_park_reason(_verdict("ESCALATE"), TASK_ID)
    assert reason == (
        "NERVE-ESCALATE-PARK: completion escalated with 2 unmet criteria (DOD-01, DOD-02) "
        "— no further run until a human resolves them. Unblock: hermes kanban unblock t_park"
    )
    assert reason.startswith("NERVE-ESCALATE-PARK:")
    assert "\n" not in reason and "\r" not in reason


def test_park_reason_names_only_first_three_of_seven():
    ids = tuple(f"DOD-{n:02d}" for n in range(1, 8))
    reason = supervisor_mod.escalation_park_reason(_verdict("ESCALATE", ids), TASK_ID)
    assert "with 7 unmet criteria (DOD-01, DOD-02, DOD-03, …)" in reason
    assert "DOD-04" not in reason
    assert reason.endswith("Unblock: hermes kanban unblock t_park")


def test_park_reason_tolerates_missing_criteria_none():
    verdict = SimpleNamespace(allow=False, value="ESCALATE", missing_criteria=None)
    assert supervisor_mod.escalation_park_reason(verdict, TASK_ID) == ""
    assert supervisor_mod.escalation_park_reason(SimpleNamespace(), TASK_ID) == ""


def test_park_reason_truncates_each_criterion_and_stays_single_line():
    long_id = "A" * 200
    reason = supervisor_mod.escalation_park_reason(_verdict("ESCALATE", (long_id, "two\nlines")), TASK_ID)
    assert "(" + "A" * 60 + ", two lines)" in reason
    assert "A" * 61 not in reason
    assert "\n" not in reason


def test_park_reason_is_reachable_from_the_supervisor_instance(run):
    verdict = _verdict("ESCALATE")
    assert run.sup.escalation_park_reason(verdict, TASK_ID) == supervisor_mod.escalation_park_reason(verdict, TASK_ID)


# --------------------------- adapter.block ---------------------------

def test_adapter_block_reports_success_and_closes_connection(board):
    out = CanonicalKanbanAdapter().block(task_id="x", kind="needs_input", reason="why", expected_run_id=3)
    assert out == (True, None)
    assert board.calls == [("x", {"kind": "needs_input", "reason": "why", "expected_run_id": 3})]
    assert board.events == ["open", "close"]


def test_adapter_block_refusal_returns_false_with_detail_and_never_raises(monkeypatch):
    _install(monkeypatch, FakeBoard(result=False))
    ok, detail = CanonicalKanbanAdapter().block(task_id="x", kind="needs_input", reason="why")
    assert ok is False
    assert isinstance(detail, str) and detail


def test_adapter_block_without_run_fence_passes_none(board):
    CanonicalKanbanAdapter().block(task_id="x", kind="needs_input", reason="why")
    assert board.calls[0][1]["expected_run_id"] is None


def test_adapter_block_closes_connection_when_the_board_raises(monkeypatch):
    fake = _install(monkeypatch, FakeBoard(raises=RuntimeError("board locked")))
    with pytest.raises(RuntimeError, match="board locked"):
        CanonicalKanbanAdapter().block(task_id="x", kind="needs_input", reason="why", expected_run_id=3)
    assert fake.events == ["open", "close"]


# --------------------------- 5-9: hook park ---------------------------

def test_escalate_with_missing_criteria_parks_at_the_complete_gate(run, board, monkeypatch):
    response = _complete_gate(run, monkeypatch, _verdict("ESCALATE"))
    assert len(board.calls) == 1
    task_id, kwargs = board.calls[0]
    assert task_id == TASK_ID
    assert kwargs["kind"] == "needs_input"
    assert kwargs["expected_run_id"] == run.ident.run_id == RUN_ID
    assert kwargs["reason"].startswith("NERVE-ESCALATE-PARK:")
    assert response == {
        "action": "block",
        "message": "Reflex batched completion verdict ESCALATE. Missing: DOD-01, DOD-02 Parked needs_input.",
        "rule_key": "jev:work-completion",
    }


def test_escalate_with_missing_criteria_parks_at_pre_verify(run, board, monkeypatch):
    response = _pre_verify(run, monkeypatch, _verdict("ESCALATE"))
    assert len(board.calls) == 1
    assert board.calls[0][1]["kind"] == "needs_input"
    assert board.calls[0][1]["expected_run_id"] == RUN_ID
    assert response == {
        "action": "continue",
        "message": "Nerve completion gate: Reflex batched completion verdict ESCALATE. "
                   "Missing: DOD-01, DOD-02 Parked needs_input.",
    }


def test_escalate_with_empty_missing_does_not_park_and_message_is_unchanged(run, board, monkeypatch):
    assert _complete_gate(run, monkeypatch, _verdict("ESCALATE", ())) == {
        "action": "block",
        "message": "Reflex batched completion verdict ESCALATE.",
        "rule_key": "jev:work-completion",
    }
    assert _pre_verify(run, monkeypatch, _verdict("ESCALATE", ())) == {
        "action": "continue",
        "message": "Nerve completion gate: Reflex batched completion verdict ESCALATE.",
    }
    assert board.calls == []


@pytest.mark.parametrize("value", ["RETRY", "REPLAN", "AMEND", "NEEDS_EVIDENCE"])
def test_non_escalate_verdicts_are_unchanged_at_the_complete_gate(run, board, monkeypatch, value):
    # Literal pinned from the pre-change tree: this test passes unmodified at 76320ec.
    assert _complete_gate(run, monkeypatch, _verdict(value)) == {
        "action": "block",
        "message": f"Reflex batched completion verdict {value}. Missing: DOD-01, DOD-02",
        "rule_key": "jev:work-completion",
    }
    assert board.calls == []
    assert board.events == []


@pytest.mark.parametrize("value", ["RETRY", "REPLAN", "AMEND", "NEEDS_EVIDENCE"])
def test_non_escalate_verdicts_are_unchanged_at_pre_verify(run, board, monkeypatch, value):
    # Literal pinned from the pre-change tree: this test passes unmodified at 76320ec.
    assert _pre_verify(run, monkeypatch, _verdict(value)) == {
        "action": "continue",
        "message": f"Nerve completion gate: Reflex batched completion verdict {value}. Missing: DOD-01, DOD-02",
    }
    assert board.calls == []
    assert board.events == []


def test_park_failure_never_changes_the_tool_call_verdict(run, monkeypatch):
    fake = _install(monkeypatch, FakeBoard(raises=RuntimeError("board locked")))
    response = _complete_gate(run, monkeypatch, _verdict("ESCALATE"))
    assert len(fake.calls) == 1
    assert response["action"] == "block"
    assert response["rule_key"] == "jev:work-completion"
    assert response["message"] == (
        "Reflex batched completion verdict ESCALATE. Missing: DOD-01, DOD-02"
        " (park failed: RuntimeError: board locked)"
    )


def test_park_refusal_is_reported_and_keeps_the_block(run, monkeypatch):
    _install(monkeypatch, FakeBoard(result=False))
    response = _complete_gate(run, monkeypatch, _verdict("ESCALATE"))
    assert response["action"] == "block"
    assert response["rule_key"] == "jev:work-completion"
    assert " (park refused: " in response["message"]
    assert "Parked needs_input." not in response["message"]


def test_park_helper_is_a_noop_without_a_run_identity(run, board):
    verdict = _verdict("ESCALATE")
    assert hooks._park_escalated_card(run.sup, None, verdict) is None
    assert hooks._park_escalated_card(run.sup, SimpleNamespace(task_id=TASK_ID, run_id=None), verdict) is None
    assert hooks._park_escalated_card(run.sup, SimpleNamespace(task_id=TASK_ID), verdict) is None
    allowed = CompletionVerdict(allow=True, value="ESCALATE", confidence=1.0, reason="x", missing_criteria=MISSING)
    assert hooks._park_escalated_card(run.sup, run.ident, allowed) is None
    assert board.calls == []


# --------------------------- 10: classifier ---------------------------

@pytest.mark.parametrize(
    "title, body, gated, classes",
    [
        ("pay the March vendor invoice", "", True, ("money", "vendor")),
        ("Refactor the parser", "fix a null deref", False, ()),
        ("", "Choose pricing tier for launch", True, ("money",)),
        ("Decide the Q1 ROADMAP", "", True, ("product_direction",)),
    ],
)
def test_classify_human_gated_table(title, body, gated, classes):
    gate = autobind.classify_human_gated(title=title, body=body)
    assert gate.gated is gated
    assert gate.classes == classes
    assert gate.classes == tuple(sorted(gate.classes))
    assert bool(gate.matched) is gated


def test_classify_human_gated_reports_the_literal_needles_and_is_deterministic():
    first = autobind.classify_human_gated(title="pay the March vendor invoice", body="")
    assert first.matched == ("invoice", "vendor")
    assert first == autobind.classify_human_gated(title="pay the March vendor invoice", body="")
    assert autobind.classify_human_gated() == autobind.HumanGate(gated=False, classes=(), matched=())
    assert "money" in autobind.classify_human_gated(body="Choose pricing tier for launch").classes


# --------------------------- 11-12: build_criteria tagging ---------------------------

DOD_BODY = "Do the thing.\n\n## Acceptance criteria\n- first thing is done\n- second thing is done\n"
PRE_CHANGE_CRITERIA = (
    [
        {"id": "DOD-01", "description": "first thing is done", "required": True, "weight": 1.0, "estimated_tokens": 29750},
        {"id": "DOD-02", "description": "second thing is done", "required": True, "weight": 1.0, "estimated_tokens": 29750},
    ],
    10500,
)


def test_build_criteria_default_output_is_unchanged():
    # Literal pinned from the pre-change tree: this test passes unmodified at 76320ec.
    assert autobind.build_criteria(DOD_BODY, total_budget=70000) == PRE_CHANGE_CRITERIA


def test_build_criteria_without_a_gate_adds_no_keys():
    ungated = autobind.HumanGate(gated=False, classes=(), matched=())
    for gate in (None, ungated):
        out = autobind.build_criteria(DOD_BODY, total_budget=70000, human_gate=gate)
        assert out == PRE_CHANGE_CRITERIA
        for criterion in out[0]:
            assert list(criterion) == ["id", "description", "required", "weight", "estimated_tokens"]
            assert "human_gated" not in criterion


def test_build_criteria_stamps_every_criterion_when_gated():
    gate = autobind.HumanGate(gated=True, classes=("money", "vendor"), matched=("invoice", "vendor"))
    criteria, reserve = autobind.build_criteria(DOD_BODY, total_budget=70000, human_gate=gate)
    assert reserve == PRE_CHANGE_CRITERIA[1]
    assert len(criteria) == 2
    for criterion, before in zip(criteria, PRE_CHANGE_CRITERIA[0]):
        assert criterion["human_gated"] is True
        assert criterion["human_gate_classes"] == ["money", "vendor"]
        assert {k: v for k, v in criterion.items() if not k.startswith("human_gate")} == before


# --------------------------- 13-14: ensure_kanban_binding ---------------------------

def _task(title: str, body: str):
    return SimpleNamespace(id="t_gate", title=title, body=body, current_run_id=42, claim_lock="claim-42")


@pytest.fixture
def binding(tmp_path, monkeypatch):
    runtime.set_supervisor_for_tests(None, enabled_value=False)
    runtime.configure(enabled=True, store_path=str(tmp_path / "work.db"), provider_decisions_enabled=False)
    sup = runtime.supervisor()
    for key, value in {
        "HERMES_KANBAN_TASK": "t_gate",
        "HERMES_KANBAN_TASK_ID": "t_gate",
        "HERMES_KANBAN_RUN_ID": "42",
        "HERMES_KANBAN_CLAIM_LOCK": "claim-42",
        "HERMES_KANBAN_WORKSPACE": str(tmp_path),
        "HERMES_NERVE_DOD_HASH": "",
    }.items():
        monkeypatch.setenv(key, value)
    binds: list[dict] = []
    real_bind = sup.bind_contract

    def recording_bind(**kwargs):
        binds.append(kwargs)
        return real_bind(**kwargs)

    monkeypatch.setattr(sup, "bind_contract", recording_bind)

    def use(task):
        monkeypatch.setattr(autobind, "_load_canonical_task", lambda tid: task)

    yield SimpleNamespace(sup=sup, binds=binds, use=use)
    runtime.set_supervisor_for_tests(None, enabled_value=False)


def _diagnostics(sup, kind: str) -> int:
    with sup.store.read() as con:
        return con.execute("SELECT count(*) FROM supervision_diagnostics WHERE kind=?", (kind,)).fetchone()[0]


def test_ensure_binding_is_unchanged_for_an_ungated_card(binding, board):
    binding.use(_task("Refactor the parser", DOD_BODY))
    ident = autobind.ensure_kanban_binding(binding.sup, session_id="s")
    assert ident is not None
    assert (ident.task_id, ident.run_id, ident.claim_identity) == ("t_gate", 42, "claim-42")
    assert len(binding.binds) == 1
    assert binding.binds[0]["goal"] == "Refactor the parser"
    assert binding.sup.active_contract("t_gate") is not None
    assert board.calls == []
    assert _diagnostics(binding.sup, "autobind_human_gated") == 0
