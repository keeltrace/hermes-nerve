"""Non-finite provider scores cannot reach approval/decision thresholds."""
from types import SimpleNamespace

import pytest
from hermes_nerve.client import JevError
from hermes_nerve.engine import DecisionEngine


@pytest.mark.parametrize("field", ["confidence", "probabilities"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_provider_numbers_are_rejected(field, value, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_NERVE_RECEIPTS", str(tmp_path / "receipts.jsonl"))
    answer = {"choice": "ALLOW", "confidence": 0.99,
              "probabilities": {"ALLOW": 0.99, "BLOCK": 0.01}}
    if field == "confidence":
        answer[field] = value
    else:
        answer[field]["ALLOW"] = value
    class OfflineProvider:
        def system_one(self, **kwargs):
            return SimpleNamespace(answers={"decision": answer}, model="offline", usage={},
                latency_ms=0, request_id="offline", provider="fixture", transport="fixture",
                live_provider_call=False)
    with pytest.raises(JevError, match="finite"):
        DecisionEngine(provider=OfflineProvider()).decide(state={}, instructions="fixture",
            choices=["ALLOW", "BLOCK"], contract="offline/nonfinite/v1")
