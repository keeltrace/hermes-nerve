from __future__ import annotations

from typing import Any


def usage_parts(usage: Any) -> dict[str, int]:
    u = usage if isinstance(usage, dict) else {}
    def first(*names: str) -> int:
        for name in names:
            try:
                if u.get(name) is not None:
                    return max(0, int(u.get(name) or 0))
            except (TypeError, ValueError):
                pass
        return 0
    inp = first("input_tokens", "prompt_tokens")
    out = first("output_tokens", "completion_tokens")
    reasoning = first("reasoning_tokens")
    cache = first("cache_read_tokens", "cached_tokens")
    declared = first("total_tokens")
    accounted = max(declared, inp + out + reasoning)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "reasoning_tokens": reasoning,
        "cache_read_tokens": cache,
        "total_tokens": declared,
        "accounted_tokens": accounted,
    }


def estimate_tokens_avoided(control: str, remaining_tokens: int) -> int:
    remaining = max(0, int(remaining_tokens))
    factor = {"BLOCK": 0.80, "REPLAN": 0.50, "WATCH": 0.15, "CONTINUE": 0.0}.get(str(control).upper(), 0.0)
    return int(remaining * factor)
