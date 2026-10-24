import types

import pytest

from hermes_nerve import client, engine


def _decision_provider(answer_payload):
    class P:
        provider_kind = "openrouter"
        model = "typesafe/jev-1.14"
        transport = "probe"
        live_provider_call = True

        def system_one(self, *, state, questions, model=None):
            return types.SimpleNamespace(
                answers=answer_payload,
                model="typesafe/jev-1.14",
                latency_ms=1.0,
                usage={},
                request_id="r",
                provider="probe",
                transport="probe",
                live_provider_call=True,
            )

    return P()


def _assess_provider(answers):
    class P:
        provider_kind = "openrouter"
        model = "typesafe/jev-1.14"
        transport = "probe"
        live_provider_call = True

        def system_one(self, *, state, questions, model=None):
            return types.SimpleNamespace(
                answers=answers,
                model="typesafe/jev-1.14",
                latency_ms=1.0,
                usage={},
                request_id="r",
                provider="probe",
                transport="probe",
                live_provider_call=True,
            )

    return P()


@pytest.mark.parametrize(
    "payload",
    [
        {"other": {}},
        {"decision": {}},
        {"decision": {"choice": "ACCEPT", "probabilities": {"ACCEPT": "high"}}},
        {"decision": {"choice": "ACCEPT", "confidence": "very"}},
        "not-a-dict",
    ],
)
def test_malformed_provider_answers_raise_provider_error(payload):
    with pytest.raises(client.JevError):
        engine.DecisionEngine(provider=_decision_provider(payload)).decide(
            state={}, instructions="x", choices=["ACCEPT", "BLOCK"],
            criteria={"ACCEPT": None, "BLOCK": None}, contract="probe/v1",
        )


def test_malformed_assess_provider_answers_raise_provider_error():
    with pytest.raises(client.JevError):
        engine.DecisionEngine(provider=_assess_provider("not-a-dict")).assess(
            state={}, questions={"q": {"type": "noul", "instructions": "x"}},
            contract="probe/v1",
        )


def test_out_of_contract_choice_preserves_value_error():
    with pytest.raises(ValueError):
        engine.DecisionEngine(provider=_decision_provider({"decision": {"choice": "MAYBE"}})).decide(
            state={}, instructions="x", choices=["ACCEPT", "BLOCK"],
            criteria={"ACCEPT": None, "BLOCK": None}, contract="probe/v1",
        )


def test_valid_decision_returns():
    result = engine.DecisionEngine(provider=_decision_provider({"decision": {
        "choice": "ACCEPT", "confidence": 0.95,
        "probabilities": {"ACCEPT": 0.95, "BLOCK": 0.05},
    }})).decide(
        state={}, instructions="x", choices=["ACCEPT", "BLOCK"],
        criteria={"ACCEPT": None, "BLOCK": None}, contract="probe/v1",
    )
    assert result.value == "ACCEPT"


def test_valid_assess_preserves_typed_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_NERVE_RECEIPTS", str(tmp_path / "r.jsonl"))
    result = engine.DecisionEngine(provider=_assess_provider({
        "urgent": {"type": "noul", "noul": 0.97},
        "team": {"type": "choice", "choice": "technical", "confidence": 0.9},
    })).assess(
        state={},
        questions={
            "urgent": {"type": "noul", "instructions": "x"},
            "team": {"type": "choice", "instructions": "x", "criteria": {"technical": None, "billing": None}},
        },
        contract="probe/v1",
    )
    assert result["answers"]["urgent"]["noul"] == 0.97
    assert result["answers"]["team"]["choice"] == "technical"