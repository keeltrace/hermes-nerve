"""Provider bindings for research lanes. Credentials come from the environment:
MUNA_API_BASE, MUNA_API_KEY, NOUS_API_TOKEN. Never hardcode machine paths."""
from __future__ import annotations
import json, os, urllib.request

def _env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise RuntimeError(f"missing env {name} for research lane")
    return v

def muna_call(model: str, prompt: str, max_tokens: int = 400) -> str:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0.4}).encode()
    req = urllib.request.Request(_env("MUNA_API_BASE").rstrip("/") + "/chat/completions",
        data=body, headers={"Authorization": f"Bearer {_env('MUNA_API_KEY')}",
                            "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()

def bunny_call(model: str, prompt: str, max_tokens: int = 1400, effort: str = "medium") -> str:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0.6, "reasoning_effort": effort}).encode()
    req = urllib.request.Request("https://inference-api.nousresearch.com/v1/chat/completions",
        data=body, headers={"Authorization": f"Bearer {_env('NOUS_API_TOKEN')}",
                            "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"] or ""
