from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

from . import assistant
from .config_resolver import resolve_config
from .integrations import shared_context
from .modules import MODULES
from .profiles import clear_profile, load_profile, restore_profile_state, save_profile, snapshot_profile_state

_ORDER = ("fat_cat", "operator", "lean", "marie_kondo")
_CLI_PROFILES = _ORDER + ("legacy",)
_DISPLAY = {"legacy": "Legacy", "custom": "Full Configuration", "fat_cat": "Fat Cat", "operator": "Operator", "lean": "Lean", "marie_kondo": "Marie Kondo"}
_DESC = {
    "fat_cat": "Max assistant quality; spend more tokens for capability.",
    "operator": "Direct Hermes power use: tools, coding, agents, context.",
    "lean": "Factory-first nervous system with token-heavy extras off.",
    "marie_kondo": "Minimum Nerve: only features that clearly earn their cost.",
}


def _doc(profile, overrides=None, advanced=None):
    return {
        "version": 1,
        "nerve_profile": profile,
        "nerve_modules": dict(overrides or {}),
        "advanced": dict(advanced or {}),
    }


def _advanced_catalog():
    """Return manifest-declared advanced keys/types; fail loudly on schema drift."""
    manifest = Path(__file__).resolve().parents[1] / "plugin.yaml"
    pattern = re.compile(r"^  ([a-zA-Z0-9_]+): \{type: ([a-z]+), default: (.+?)(?:, description: .*)?\}$")
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Cannot read Nerve plugin manifest: {manifest}") from exc
    catalog = {}
    in_schema = False
    for line in lines:
        if line == "config_schema:":
            in_schema = True
            continue
        if in_schema and line and not line.startswith("  "):
            break
        if not in_schema or not line.strip():
            continue
        match = pattern.match(line)
        if not match:
            raise RuntimeError(f"Unsupported config_schema line in plugin.yaml: {line}")
        key, kind, default = match.groups()
        if key in {"nerve_profile", "nerve_modules"}:
            continue
        catalog[key] = {"type": kind, "default": default.strip()}
    if not catalog:
        raise RuntimeError("Nerve plugin manifest contains no advanced config_schema entries")
    return catalog


def _parse_advanced_value(kind, raw):
    text = str(raw).strip()
    if kind == "bool":
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "on", "y"}: return True
        if lowered in {"0", "false", "no", "off", "n"}: return False
        raise ValueError("expected boolean")
    if kind == "int": return int(text)
    if kind == "float": return float(text)
    if kind == "dict":
        value = ast.literal_eval(text)
        if not isinstance(value, dict): raise ValueError("expected dict")
        return value
    return text


def _edit_advanced(existing=None):
    advanced = dict(existing or {})
    catalog = _advanced_catalog()
    if input("Advanced configuration? [y/N]: ").strip().lower() not in {"y", "yes"}:
        return advanced
    print("Enter an advanced setting name to edit. Blank saves and exits.")
    print("Use 'list' to show available setting names; use 'clear <name>' to remove an override.")
    while True:
        raw = input("advanced> ").strip()
        if not raw:
            return advanced
        if raw == "list":
            print("\n".join(sorted(catalog)))
            continue
        if raw.startswith("clear "):
            advanced.pop(raw[6:].strip(), None)
            continue
        if raw not in catalog:
            print(f"Unknown setting: {raw}", file=sys.stderr)
            continue
        current = advanced.get(raw, catalog[raw]["default"])
        value = input(f"{raw} [{current}]: ").strip()
        if not value:
            continue
        try:
            advanced[raw] = _parse_advanced_value(catalog[raw]["type"], value)
        except (ValueError, SyntaxError) as exc:
            print(f"Invalid value for {raw}: {exc}", file=sys.stderr)


def _current():
    return resolve_config()


def _show(r):
    print(f"Profile: {_DISPLAY.get(r.profile, r.profile)}")
    for mid, spec in MODULES.items():
        print(f"[{'ON ' if r.enabled(mid) else 'OFF'}] {spec.name:<18} {spec.description}")


def _save(profile, overrides=None, *, reset=False, advanced=None):
    snapshot = snapshot_profile_state()
    current = load_profile()
    if overrides is None and not reset and current and current.get("nerve_profile") == profile:
        overrides = dict(current.get("nerve_modules") or {})
    if advanced is None and not reset and current and current.get("nerve_profile") == profile:
        advanced = dict(current.get("advanced") or {})
    doc = _doc(profile, overrides, advanced)
    try:
        path = save_profile(doc)
        resolved = resolve_config(profile=doc)
        shared = shared_context.reconcile_enabled(resolved.enabled("shared_context"))
    except Exception:
        # Profile persistence and external reconciliation are one setup
        # transaction. Restore the exact primary/backup bytes after any
        # failure that occurs once the attempted update begins.
        restore_profile_state(snapshot)
        raise
    if shared.get("warning"):
        print("Shared Context: " + shared["warning"], file=sys.stderr)
    # Profile state and explicit operator Assistant disable are separate authorities.
    # Only --assistant-install/--assistant-disable may persist that operator override.
    return path


def _interactive():
    cur = _current()
    print("NERVE SETUP\n")
    print("1. Full Configuration - Pick every Nerve module and advanced option yourself.")
    for i, profile in enumerate(_ORDER, 2):
        print(f"{i}. {_DISPLAY[profile]:<18} - {_DESC[profile]}")
    try:
        raw = input("\nSelect [1-5]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return 1
    if raw == "1":
        modules = dict(cur.modules)
        for mid, spec in MODULES.items():
            val = input(f"{spec.name} [{'Y/n' if modules[mid] else 'y/N'}] {spec.description} ").strip().lower()
            if val in {"y", "yes", "on", "1"}:
                modules[mid] = True
            elif val in {"n", "no", "off", "0"}:
                modules[mid] = False
        existing = load_profile()
        current_advanced = dict(existing.get("advanced") or {}) if existing else {}
        advanced = _edit_advanced(current_advanced)
        path = _save("custom", modules, advanced=advanced)
        print(f"Saved {path}")
        return 0
    if raw not in {"2", "3", "4", "5"}:
        print("Invalid selection", file=sys.stderr)
        return 2
    profile = _ORDER[int(raw) - 2]
    proposed = resolve_config(profile=_doc(profile))
    _show(proposed)
    if input("Apply? [y/N]: ").strip().lower() not in {"y", "yes"}:
        return 1
    path = _save(profile)
    print(f"Saved {path}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nerve")
    sub = parser.add_subparsers(dest="command")
    setup = sub.add_parser("setup")
    setup.add_argument("--show", action="store_true")
    setup.add_argument("--profile", choices=_CLI_PROFILES)
    setup.add_argument("--reset", choices=_CLI_PROFILES)
    setup.add_argument("--explain", action="store_true")
    setup.add_argument("--install-shared-context", metavar="SOURCE")
    setup.add_argument("--replace-shared-context", action="store_true")
    setup.add_argument("--assistant-install", action="store_true")
    setup.add_argument("--assistant-disable", action="store_true")
    args = parser.parse_args(argv)
    if args.command != "setup":
        parser.print_help(); return 2
    if args.profile and args.reset:
        setup.error("--profile and --reset are mutually exclusive")
    if args.replace_shared_context and not args.install_shared_context:
        setup.error("--replace-shared-context requires --install-shared-context SOURCE")
    if args.assistant_install and args.assistant_disable:
        setup.error("--assistant-install and --assistant-disable are mutually exclusive")
    if args.install_shared_context:
        status = shared_context.install_from_source(args.install_shared_context, replace=args.replace_shared_context)
        print(f"Shared Context installed: {status['path']}")
        print(f"Doctor: {status['doctor_command']}")
        if _current().enabled("shared_context"):
            shared_context.reconcile_enabled(True)
            print("Shared Context enabled for the current Nerve profile.")
        return 0
    if args.assistant_install:
        print(assistant.install()); return 0
    if args.assistant_disable:
        print(assistant.disable()); return 0
    if args.show and args.profile:
        _show(resolve_config(profile=_doc(args.profile)))
        return 0
    if args.show or args.explain:
        current = _current(); _show(current)
        if args.explain:
            if current.enabled("shared_context"):
                print("\nShared Context: " + shared_context.explain())
            else:
                print("\nShared Context: OFF (opt-in; install HermesContextBus from an explicit local source before enabling).")
        return 0
    target = args.profile or args.reset
    if target:
        path = _save(target, reset=bool(args.reset))
        # Show the document that was actually persisted. Re-selecting the same
        # profile intentionally preserves module/advanced overrides, so showing
        # a fresh canonical preset here would misreport the effective state.
        saved = load_profile()
        _show(resolve_config(profile=saved or _doc(target)))
        print(f"Saved {path}")
        return 0
    if not sys.stdin.isatty():
        setup.error("interactive setup requires a TTY; use --profile")
    return _interactive()


if __name__ == "__main__":
    raise SystemExit(main())
