"""Round-4 defect probes: current behavior for the five authorization-integrity defects."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_nerve import risk, gate  # noqa: E402

WD = Path.cwd()
HOME = Path.home()

CASES = [
    # D1: path-qualified executables with trusted basenames
    ("terminal", {"command": "./ls -la", "workdir": str(WD)}),
    ("terminal", {"command": "./cat /home/user/.ssh/id_ed25519", "workdir": str(WD)}),
    ("terminal", {"command": "/tmp/pytest -q", "workdir": str(WD)}),
    ("terminal", {"command": "/bin/cat /home/user/.aws/credentials", "workdir": str(WD)}),
    # D2: nested sensitive-home + env reads
    ("terminal", {"command": f"cat {HOME}/.config/gh/hosts.yml", "workdir": str(WD)}),
    ("terminal", {"command": f"ls {HOME}/.docker", "workdir": str(WD)}),
    ("terminal", {"command": "cat .env", "workdir": str(WD)}),
    # D3/D4: push variants and unresolved effects
    ("terminal", {"command": "git push --all origin", "workdir": str(WD)}),
    ("terminal", {"command": "git push --mirror", "workdir": str(WD)}),
    ("terminal", {"command": "curl -X POST -d '{}' https://api.example.com/deploy", "workdir": str(WD)}),
    ("terminal", {"command": "curl -X POST -d '{}' https://api.example.com/deploy", "workdir": str(WD)}),
]
print("--- D1/D2 classification ---")
for tool, args in CASES[:7]:
    a = risk.assess_tool_call(tool, args)
    print(f"{str(args.get('command'))[:52]:54} -> {a.category:22} auto={a.auto_allow}")

print("--- D3/D4 gate keys: identical calls repeated ---")
import tempfile, os  # noqa: E402
os.environ["HERMES_NERVE_GATE_EVENTS"] = tempfile.mkstemp(suffix=".jsonl")[1]
gate.configure(mode="enforce", min_confidence=0.80, min_allow_probability=0.90,
               scope="selective", risk_preflight=True, learned_preferences=False,
               preference_min_samples=2)


class Esc:
    def decide(self, **kw):
        from hermes_nerve.engine import DecisionResult
        return DecisionResult(value="APPROVAL", confidence=0.9,
                              probabilities={"APPROVAL": 0.8, "ALLOW": 0.1, "BLOCK": 0.1},
                              model="p", latency_ms=1.0, contract="x")


gate.evaluate_tool_call.__kwdefaults__["engine_factory"] = Esc
print("--- D3/D4 gate keys: identical calls repeated ---")
for cmd in ("git push origin main", "git push --all origin", "git push --mirror",
            "curl -X POST -d '{}' https://api.example.com/deploy"):
    r1 = gate.pre_tool_call("terminal", {"command": cmd, "workdir": str(WD)},
                            task_id=None, tool_call_id="p1")
    r2 = gate.pre_tool_call("terminal", {"command": cmd, "workdir": str(WD)},
                            task_id=None, tool_call_id="p2")
    k1 = r1["rule_key"] if r1 else None
    k2 = r2["rule_key"] if r2 else None
    rel = "SAME (reusable)" if k1 == k2 else "differs"
    print(f"{cmd[:48]:50} k1={str(k1)[-16:]} k2={str(k2)[-16:]} {rel}")

print("--- D5 read_file multi-path shape ---")
sens = [f"/home/user/.ssh/key{i}" for i in range(10)]
a1 = risk.assess_tool_call("read_file", {"path": sens})
a2 = risk.assess_tool_call("read_file", {"path": sens[:9] + ["/home/user/.ssh/OTHER"]})
print("categories:", a1.category, a2.category)
print("shape paths stored:", len(a1.shape.get("paths", [])), "of", len(sens))
print("shapes differ (9 shared + different 10th):", a1.shape != a2.shape)
