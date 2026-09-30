#!/usr/bin/env python3
"""Patch one Hermes profile's Nerve Reflex settings reproducibly.

Writes plugins.entries.nerve.settings: Hermes reads a plugin's settings only from its
own entry (settings, then the older config subtree), and Nerve's plugin id is "nerve".
Only --backend and the options you pass are written. An omitted option keeps whatever
value Hermes currently resolves for it, including one in the config fallback; Nerve
applies its built-in defaults and HERMES_REFLEX_* environment fallbacks at load time.
Earlier versions of this script wrote plugins.entries.hermes-nerve, which Hermes never
reads; such an entry is reported and left untouched.
"""
from __future__ import annotations
import argparse, os
from pathlib import Path
try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML is required for this operator script (run with the Hermes venv Python)") from exc

PLUGIN_ID='nerve'
LEGACY_ENTRY='hermes-nerve'
# setting: argparse destination. Options default to None, so omitted ones are never written.
OPTIONS={
    'reflex_shadow_backend':'shadow_backend',
    'reflex_laya_base_url':'laya_base_url',
    'reflex_laya_model':'laya_model',
    'reflex_openjev_base_url':'openjev_base_url',
    'reflex_openjev_model':'openjev_model',
    'reflex_openjev_expected_identity':'openjev_expected_identity',
    'work_reviewer':'orchestrator_reviewer',
}


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument('profile')
    p.add_argument('--backend',choices=['jev','laya','openjev','shadow'],required=True)
    p.add_argument('--shadow-backend',choices=['laya','openjev'])
    p.add_argument('--laya-base-url')
    p.add_argument('--laya-model')
    p.add_argument('--openjev-base-url')
    p.add_argument('--openjev-model')
    p.add_argument('--openjev-expected-identity')
    p.add_argument('--shadow-sync',action=argparse.BooleanOptionalAction,help='Make shadow calls synchronous for benchmark reproducibility; --no-shadow-sync makes them asynchronous')
    p.add_argument('--orchestrator-reviewer',help='Profile that owns final Nerve budget review authority')
    a=p.parse_args()
    home=Path(os.environ.get('HERMES_HOME') or Path.home()/'.hermes')
    path=home/'profiles'/a.profile/'config.yaml'
    if not path.exists(): raise SystemExit(f"profile config not found: {path}")
    data=yaml.safe_load(path.read_text()) or {}
    plugins=data.setdefault('plugins',{})
    entries=plugins.setdefault('entries',{})
    entry=entries.setdefault(PLUGIN_ID,{})
    settings=entry.setdefault('settings',{})
    changes={'reflex_backend':a.backend}
    for setting,dest in OPTIONS.items():
        value=getattr(a,dest)
        if value is not None:
            changes[setting]=value
    if a.shadow_sync is not None:
        changes['reflex_shadow_async']=not a.shadow_sync
    settings.update(changes)
    path.write_text(yaml.safe_dump(data,sort_keys=False))
    if LEGACY_ENTRY in entries:
        print(f"note: plugins.entries.{LEGACY_ENTRY} is ignored by Hermes and was left unchanged; move any settings you still need to plugins.entries.{PLUGIN_ID}.settings, then remove it")
    print(f"configured profile={a.profile} backend={a.backend} wrote=[{', '.join(sorted(changes))}] path={path}")
    return 0
if __name__=='__main__': raise SystemExit(main())
