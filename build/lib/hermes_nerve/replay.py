"""Replay an explicit decision corpus against the configured Jev backend."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import DecisionEngine


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay Nerve JSONL evaluation cases")
    parser.add_argument("corpus", type=Path)
    args = parser.parse_args()
    engine = DecisionEngine()
    total = correct = 0
    for line in args.corpus.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        result = engine.decide(
            state=case["state"],
            instructions=case["instructions"],
            choices=case["choices"],
            criteria=case.get("criteria"),
            contract=case.get("contract", "replay/v1"),
        )
        total += 1
        ok = result.value == case.get("expected")
        correct += int(ok)
        print(json.dumps({"id": case.get("id"), "expected": case.get("expected"), "actual": result.value, "confidence": result.confidence, "ok": ok}))
    print(json.dumps({"cases": total, "correct": correct, "accuracy": (correct / total if total else None)}))
    return 0 if total and correct == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
