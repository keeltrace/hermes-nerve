"""Capture a REAL, isolated Hermes session with Nerve ON/OFF/SHADOW.

Uses existing subscription credentials in process only, synthetic fixture
context, native plugin discovery, native tool/skill assembly and the real
provider transport. No monkeypatches, response stubs or production restarts.
Run with the installed Hermes interpreter. Outputs contain fixture context
and request bodies, NEVER authorization headers or credentials.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("apply", "off", "shadow"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    import hashlib
    source_paths = ("__init__.py", "hermes_nerve/context_integration.py", "hermes_nerve/consumer.py",
                    "hermes_nerve/selective_context.py", "hermes_nerve/progress_integrity.py",
                    "scripts/verify_selective_context_session.py")
    def source_identity():
        return {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in source_paths}
    source_before = source_identity()
    # Resolve authorized OAuth while the caller's home is still active. Do not
    # copy auth files, .env, config, private memories or sessions into fixtures.
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(requested="openai-codex", target_model="gpt-6.1-sol")
    if runtime.get("provider") != "openai-codex" or runtime.get("source", "").startswith("env"):
        raise RuntimeError("Qualification requires existing Codex subscription OAuth")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    home = output / "home"
    home.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_DUMP_REQUESTS"] = "1"
    # The session can see only this deliberately synthetic project.
    os.chdir(home)
    import yaml
    from hermes_nerve.modules import MODULES
    active = {"selective_context", "selective_context_memory", "selective_context_tools",
              "selective_context_skills", "verified_experience"}
    settings = {"nerve_profile": "custom", "nerve_modules": {n: n in active for n in MODULES},
                "selective_context_memory_max_admitted": 1,
                "selective_context_tools_max_admitted": 1,
                "selective_context_skills_max_admitted": 1}
    settings.update({f"selective_context_{kind}_mode": args.mode for kind in ("memory", "tools", "skills")})
    config = {"model": {"default": "gpt-6.1-sol", "provider": "openai-codex"},
              "plugins": {"enabled": ["nerve"], "entries": {"nerve": {"settings": settings}}},
              "tool_search": {"mode": "on"}, "memory": {"memory_enabled": False, "user_profile_enabled": False}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    (home / "plugins").mkdir()
    (home / "plugins" / "nerve").symlink_to(root, target_is_directory=True)
    for name, desc in (("github-review", "Review GitHub pull request code"),
                       ("recipes", "Cook dinner from recipes")):
        directory = home / "skills" / "fixture" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nSynthetic qualification skill: {name}.\n")
    # Import the core only AFTER switching to the isolated home.
    from run_agent import AIAgent
    from hermes_cli.plugins import get_plugin_manager
    manager = get_plugin_manager()
    manager.discover_and_load()
    from tools.registry import registry
    for name, description in (("fixture_github_review", "Review GitHub pull request code"),
                              ("fixture_recipes", "Cook dinner from recipes")):
        registry.register(name=name, toolset="mcp-qualification", schema={
            "name": name, "description": description, "parameters": {"type": "object", "properties": {}}},
            handler=lambda arguments, **kwargs: json.dumps({"fixture": True}))
    # Seed the SAME module instance that native plugin registration configured.
    candidates = [m for n, m in sys.modules.items()
                  if n.endswith(".hermes_nerve.experience") and getattr(m, "__file__", "")
                  and Path(str(m.__file__)).resolve().is_relative_to(root)]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one discovered Nerve experience module, got {len(candidates)}; "
                           f"home={manager.home_path}; plugins={[(p.manifest.name,p.error) for p in manager._plugins.values()]}; "
                           f"loaded={[n for n in sys.modules if n.endswith('.experience') and 'nerve' in n]}")
    store = candidates[0].default_store()
    for subject, statement, kind in (
        ("GitHub review", "GitHub pull request code review qualification memory sentinel: ORCHID-47", "verified"),
        ("GitHub review archive", "Recipe archives contain dinner menus sentinel: RECIPE-92", "verified"),
        ("GitHub review rule", "Never publish GitHub pull request reviews without approval sentinel: PINNED-18", "disproven")):
        store.create_claim(subject=subject, statement=statement, claim_type=kind,
                           evidence_ids=("synthetic-session-fixture",), confidence=.99,
                           scope={"user": "qualification-user"}, admit=False)
    captured = []
    # An observer is evidence-only: it returns nothing and cannot change the
    # outgoing request or replace the actual provider response.
    def observe(**kwargs):
        captured.append({"session_id": kwargs.get("session_id"), "request": kwargs.get("request"),
                         "api_mode": kwargs.get("api_mode"), "middleware_trace": kwargs.get("middleware_trace")})
        (output / "requests.json").write_text(json.dumps(captured, indent=2, default=str))
    from hermes_cli.plugins import PluginContext, PluginManifest
    evidence_ctx = PluginContext(PluginManifest(name="selective-context-evidence"), manager)
    evidence_ctx.register_hook("pre_api_request", observe)
    execution_records = []
    def capture_transport(*, request, next_call, **kwargs):
        from copy import deepcopy
        snapshot = deepcopy(request)
        snapshot.pop("extra_headers", None)
        record = {"request": snapshot, "provider_returned": False}
        execution_records.append(record)
        target = output / "execution-requests.json"
        target.write_text(json.dumps(execution_records, indent=2, default=str))
        response = next_call(request)
        record["provider_returned"] = True
        record["response_id"] = getattr(response, "id", None)
        target.write_text(json.dumps(execution_records, indent=2, default=str))
        return response
    evidence_ctx.register_middleware("llm_execution", capture_transport)
    agent = AIAgent(model="gpt-6.1-sol", provider=runtime["provider"], api_mode=runtime["api_mode"],
                    base_url=runtime["base_url"], api_key=runtime["api_key"],
                    max_iterations=3, max_tokens=128, quiet_mode=True,
                    enabled_toolsets=["terminal", "skills", "mcp-qualification"],
                    skip_context_files=True, skip_memory=True, skip_background_review=True,
                    platform="cli", user_id="qualification-user", cwd=str(home),
                    session_id=f"selective-context-{args.mode}")
    result = agent.run_conversation(
        "Qualification topic: Review GitHub pull request code. Do not use any tools or take any actions. "
        "Reply exactly OK. Mandatory session instruction: do not publish anything.")
    # No fabricated response: preserve actual run outcome and identifiers.
    safe = {key: value for key, value in result.items() if key in (
        "final_response", "response", "completed", "failed", "error", "api_calls", "api_call_count",
        "usage", "session_id", "iterations", "turn_exit_reason", "messages")}
    (output / "result.json").write_text(json.dumps(safe, indent=2, default=str))
    source_after = source_identity()
    (output / "source-receipt.json").write_text(json.dumps({
        "source_root": str(root), "before": source_before, "after": source_after,
        "unchanged": source_before == source_after,
        "native_experience_module": str(Path(str(candidates[0].__file__)).resolve()),
        "model": result.get("served_model") or result.get("model"),
        "provider": runtime["provider"],
        "request_sha256": hashlib.sha256((output / "execution-requests.json").read_bytes()).hexdigest(),
    }, indent=2))
    if source_before != source_after:
        raise RuntimeError("Source changed during live-session qualification")
    print(json.dumps({"mode": args.mode, "captures": len(captured), "result_keys": list(result),
                      "output": str(output)}, indent=2))
    if not captured or not execution_records or not all(r["provider_returned"] for r in execution_records):
        raise RuntimeError("No successful real provider-bound execution captured")
    if result.get("failed") or not result.get("completed"):
        raise RuntimeError("Real Hermes session did not complete normally")


if __name__ == "__main__":
    main()
