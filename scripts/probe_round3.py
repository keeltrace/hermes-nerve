"""Round-3 defect probes: current behavior for the three blockers + read_file."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_nerve import risk, gate  # noqa: E402

WD = Path.cwd()
PROBES = [
    ("terminal", {"command": "cat id_ed25519", "workdir": "/home/user/.ssh"}),
    ("terminal", {"command": "cat sshd_config", "workdir": "/etc/ssh"}),
    ("terminal", {"command": "cat /etc/ssh/sshd_config", "workdir": str(WD)}),
    ("terminal", {"command": "cat /home/user/.ssh/id_ed25519", "workdir": str(WD)}),
    ("terminal", {"command": "git stash clear", "workdir": str(WD)}),
    ("terminal", {"command": "git stash drop", "workdir": str(WD)}),
    ("terminal", {"command": "git branch -D feature-x", "workdir": str(WD)}),
    ("terminal", {"command": "git config --global user.email op@example.com", "workdir": str(WD)}),
    ("terminal", {"command": "git config --global --get user.email", "workdir": str(WD)}),
    ("read_file", {"path": "/home/user/.ssh/id_ed25519"}),
    ("read_file", {"path": "/etc/ssh/sshd_config"}),
    ("read_file", {"path": str(WD / "README.md")}),
]
for tool, args in PROBES:
    b = gate.bypass_reason(tool, args)
    a = risk.assess_tool_call(tool, args)
    label = args.get("command") or args.get("path")
    print(f"{tool:9} bypass={str(b):18} {a.category:24} auto={str(a.auto_allow):5} {str(label)[:52]}")
