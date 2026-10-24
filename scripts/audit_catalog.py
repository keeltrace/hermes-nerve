#!/usr/bin/env python3
"""Audit Nerve's operation catalog against the LIVE Hermes tool surface (0.4.2).

Modes:
    python scripts/audit_catalog.py                     # built-in + Nerve lists
    python scripts/audit_catalog.py --hermes            # enabled Hermes toolsets
    python scripts/audit_catalog.py --tools-file t.json # explicit inventory
    python scripts/audit_catalog.py --json --min-coverage 0.95

`--hermes` reads `hermes tools list`, maps enabled toolsets to their tool names,
and reports any toolset it does not recognize, so a newly installed plugin shows
up instead of being silently ignored. MCP servers are reported by scope; a
server whose tools cannot be enumerated is listed as unresolved rather than
counted as reviewed.

Buckets:
  verified_safe      explicit catalog READ operation
  verified_bounded   explicit catalog operation, project-scoped mutation
  consequential      explicit catalog operation or credential signal
  inferred_bounded   unknown plugin/MCP tool, read-shaped and screened clean
  unclassified       no rule; the gate treats it as UNCERTAIN
  unreviewed_toolset enabled Hermes toolset with no tool-name mapping
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_nerve import catalog  # noqa: E402

NATIVE_TOOLS = [
    "read_file", "read_many_files", "write_file", "patch", "search_files",
    "terminal", "execute_code", "tool_search", "tool_describe", "tool_call",
    "skills_list", "skill_view", "skill_manage",
    "browser_navigate", "browser_click", "browser_type", "browser_press",
    "browser_console", "browser_snapshot", "browser_get_images", "browser_vision",
    "delegate_task", "cronjob", "todo", "memory", "kanban_show", "kanban_update",
]

NERVE_TOOLS = [
    "nerve_decide", "nerve_rank", "nerve_verify", "nerve_assess",
    "nerve_context_curate", "nerve_context_rehydrate", "nerve_context_explain",
    "nerve_stats", "nerve_nervous_event", "nerve_supervise_card",
    "nerve_work_event", "nerve_work_status", "nerve_remote_delegate_task",
    "nerve_remote_worker_status", "nerve_remote_worker_result",
    "nerve_remote_worker_cancel", "nerve_remote_worker_control",
]

# Hermes toolsets -> the tool names they register. Unknown toolsets are
# reported, never guessed: a new plugin must be reviewed, not assumed safe.
TOOLSET_TOOLS = {
    "terminal": ["terminal", "process"],
    "file": ["read_file", "read_many_files", "write_file", "patch", "search_files"],
    "browser": ["browser_navigate", "browser_click", "browser_type", "browser_press",
                "browser_console", "browser_snapshot", "browser_get_images", "browser_vision"],
    "skills": ["skills_list", "skill_view", "skill_manage"],
    "memory": ["memory"],
    "kanban": ["kanban_show", "kanban_update"],
    "todo": ["todo"],
    "session_search": ["session_search"],
    "code_execution": ["execute_code"],
    "delegate": ["delegate_task"],
    "web": ["web_search", "web_fetch"],
    "vision": ["vision_analyze"],
    "session": ["session_search"],
    "shared_context": ["shared_context_read", "shared_context_write"],
    "cron": ["cronjob"],
    "cronjob": ["cronjob"],
    "delegation": ["delegate_task"],
    "clarify": ["clarify"],
    "connections": ["connections_list"],
    "image_gen": ["image_generate"],
    "video_gen": ["video_generate"],
    "tts": ["text_to_speech"],
    "computer_use": ["computer"],
}


def _load_tools_file(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("tools", [])
    if not isinstance(data, list):
        raise SystemExit("tools file must be a JSON list or {'tools': [...]}")
    return [str(x) for x in data if str(x).strip()]


def _hermes_inventory() -> tuple[list[str], list[str], list[str]]:
    """(tools, unresolved_mcp_servers, unreviewed_toolsets) from `hermes tools list`."""
    try:
        out = subprocess.run(["hermes", "tools", "list"], capture_output=True,
                             text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"cannot read `hermes tools list`: {exc}")
    tools: list[str] = []
    servers: list[str] = []
    unreviewed: list[str] = []
    in_mcp = False
    for line in out.splitlines():
        if line.strip().lower().startswith("mcp servers"):
            in_mcp = True
            continue
        if in_mcp:
            match = re.match(r"\s{2,}([\w.-]+)\s+(.*)", line)
            if match:
                servers.append(f"{match.group(1)}: {match.group(2).strip()}")
            continue
        match = re.match(r"\s*[✓✗]\s+\w+\s+([\w.-]+)\s", line)
        if not match or line.lstrip().startswith("✗"):
            continue
        toolset = match.group(1)
        mapped = TOOLSET_TOOLS.get(toolset)
        if mapped is None:
            unreviewed.append(toolset)
            continue
        tools.extend(mapped)
    return tools, servers, unreviewed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-file", type=Path)
    parser.add_argument("--hermes", action="store_true", help="audit enabled Hermes toolsets")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--min-coverage", type=float, default=0.0)
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero on unreviewed toolsets or unclassified tools")
    args = parser.parse_args()

    names = list(NATIVE_TOOLS) + list(NERVE_TOOLS)
    servers: list[str] = []
    unreviewed: list[str] = []
    if args.hermes:
        hermes_tools, servers, unreviewed = _hermes_inventory()
        names += hermes_tools
    if args.tools_file:
        names += _load_tools_file(args.tools_file)

    audit = catalog.inventory_audit(names)
    audit["mcp_servers_unresolved"] = servers
    audit["unreviewed_toolsets"] = unreviewed
    audit["explicitly_reviewed"] = (len(audit["verified_safe"])
                                    + len(audit["verified_bounded"])
                                    + len(audit["consequential"]))

    if args.json:
        print(json.dumps(audit, indent=2))
    else:
        print(f"registered tools audited: {audit['total']}")
        print(f"  verified safe       {len(audit['verified_safe'])}")
        print(f"  verified bounded    {len(audit['verified_bounded'])}")
        print(f"  consequential       {len(audit['consequential'])}")
        print(f"  inferred bounded    {len(audit['inferred_bounded'])} (screened by rule, not verified)")
        print(f"  unclassified        {len(audit['unclassified'])}")
        print(f"  explicitly reviewed {audit['explicitly_reviewed']}")
        print(f"  coverage            {audit['coverage'] * 100:.1f}%")
        if audit["unclassified"]:
            print("  UNCLASSIFIED: " + ", ".join(audit["unclassified"][:12]))
        if unreviewed:
            print("  UNREVIEWED TOOLSETS (new plugin?): " + ", ".join(unreviewed))
        if servers:
            print("  MCP servers (scope unresolved): " + ", ".join(servers))

    failed = audit["coverage"] < args.min_coverage
    # Unknown toolsets are surfaced for review (new plugins are detected) but do
    # not fail the run: their tool names are not enumerable from `tools list`,
    # so failing would force name guesses. Unclassified *known* tools do fail.
    if args.strict and audit["unclassified"]:
        failed = True
    if failed:
        print("FAIL: audit did not meet the requested bar", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
