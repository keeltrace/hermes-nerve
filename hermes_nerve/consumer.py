"""Pure optional consumers. No store access, network calls or semantic selector.

Settings passed here are plugin-relative, not the global config. The host resolves
only its already-enabled, profile-scoped Nerve module through PluginManager.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from .selective_context import (
    ContextCandidate, ContextAdmissionController, SelectiveContextConfig,
    SurfaceConfig, estimate_tokens,
)


def settings_from_config(config, *, home=None):
    """Resolve live profile-scoped Selective Context settings.

    An enabled module applies selection by default. Explicit shadow/off modes
    remain available for debugging. Missing/invalid profile state retains the
    complete Hermes stock context instead of enabling a consumer.
    """
    from .config_resolver import resolve_config
    from .profiles import ProfileStateError

    off = {"selective_context_enabled": False}
    off.update({f"selective_context_{kind}_mode": "off"
                for kind in ("memory", "tools", "skills")})
    try:
        if not isinstance(config, Mapping):
            return off
        plugins = config.get("plugins") or {}
        if not isinstance(plugins, Mapping):
            return off
        entries = plugins.get("entries") or {}
        if not isinstance(entries, Mapping):
            return off
        entry = entries.get("nerve") or {}
        if not isinstance(entry, Mapping):
            return off
        old, new = entry.get("config", {}), entry.get("settings", {})
        if not isinstance(old, Mapping) or not isinstance(new, Mapping):
            return off
        settings = {**old, **new}
        resolved = resolve_config(
            lambda key, default=None: settings.get(key, default), home=home)
        # Same sidecar-advanced precedence as the registered runtime.
        settings.update({key: value for key, value in resolved.advanced.items()
                         if key.startswith("selective_context_")})
        active = (resolved.enabled("selective_context")
                  and resolved.get_config("selective_context_enabled", True) is True)
        settings["selective_context_enabled"] = active
        for kind in ("memory", "tools", "skills"):
            key = f"selective_context_{kind}_mode"
            settings[key] = (
                resolved.get_config(key, "apply")
                if active and resolved.enabled(f"selective_context_{kind}")
                else "off"
            )
        return settings
    except (KeyError, TypeError, ValueError, ProfileStateError):
        return off

def _controller(settings, kind):
    if not isinstance(settings, Mapping):
        raise ValueError('settings must be a mapping')
    enabled = settings.get('selective_context_enabled', True)
    mode = settings.get(f'selective_context_{kind}_mode', 'apply')
    if type(enabled) is not bool or mode not in ('off', 'shadow', 'apply'):
        raise ValueError('invalid mode or enable flag')
    defaults = {
        'memory': (24, 8, .28),
        'tools': (128, 16, .18),
        'skills': (128, 5, .22),
    }.get(kind)
    if defaults is None:
        raise ValueError('unknown selective-context surface')
    values = [settings.get(f'selective_context_{kind}_{key}', default)
              for key, default in zip(('max_candidates', 'max_admitted'), defaults[:2])]
    if any(type(v) is not int or not 0 <= v <= 100000 for v in values):
        raise ValueError('invalid admission bounds')
    # Fresh deterministic controller: no global/profile/task lease contamination.
    cfg = SelectiveContextConfig(enabled=enabled, lease_enabled=False, semantic_enabled=False)
    setattr(cfg, kind, SurfaceConfig(mode, *values, defaults[2]))
    return ContextAdmissionController(cfg)


_ENTRY = re.compile(r'^(?P<prefix>    - )(?P<name>[^:\n]+): (?P<desc>.*)$')


def select_skills_index(stock_index, goal, settings):
    """Demote only known index description lines; preserve every other byte/name.

    mandatory_skills/pinned_skills are explicit operator setting lists. Ambiguous
    entries (plugin namespaces, collision markers) are conservatively retained.
    No goal, off/shadow, invalid input or planner failure returns stock unchanged.
    """
    try:
        controller = _controller(settings, 'skills')
        if not isinstance(goal, str) or not goal.strip() or not isinstance(stock_index, str):
            return stock_index
        pinned = set()
        for key in ('selective_context_mandatory_skills', 'selective_context_pinned_skills'):
            names = settings.get(key, [])
            if not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names):
                return stock_index
            pinned.update(names)
        if stock_index.count('<available_skills>') != 1 or stock_index.count('</available_skills>') != 1:
            return stock_index
        before, body = stock_index.split('<available_skills>', 1)
        body, after = body.split('</available_skills>', 1)
        lines = body.splitlines(keepends=True)
        candidates, matches = [], {}
        for i, line in enumerate(lines):
            match = _ENTRY.fullmatch(line.rstrip('\r\n'))
            if not match:
                continue
            name, desc = match['name'], match['desc']
            cid = str(i)  # duplicate names in distinct categories remain distinct entries
            mandatory = name in pinned or name == 'hermes-agent' or desc.startswith('[')
            candidates.append(ContextCandidate('skill', cid, 'stock-skills-index', name, desc,
                                               estimate_tokens(line), mandatory=mandatory))
            matches[cid] = (i, name)
        result = controller.plan(goal=goal, candidates=candidates, kind='skill')
        if result['mode'] != 'apply' or result['fail_open']:
            return stock_index
        selected = set(result['effective_selected_ids'])
        for cid, (i, name) in matches.items():
            if cid not in selected:
                ending = '\r\n' if lines[i].endswith('\r\n') else '\n' if lines[i].endswith('\n') else ''
                lines[i] = f'    - {name}{ending}'
        return before + '<available_skills>' + ''.join(lines) + '</available_skills>' + after
    except Exception:
        return stock_index




def select_tools_context(tool_definitions, goal, settings):
    """Convert deferrable Hermes Tool Search schemas into Nerve tool candidates,
    run admission selection, and return proposed selected tool names with receipt.

    INPUT:
    - tool_definitions: list of dicts from Hermes tool_search.classify_tools()
      representing deferrable (non-core/direct) tools for the current session
    - goal: current user task goal string
    - settings: plugin-relative config dict from the Hermes profile

    OUTPUT:
    - selected_ids: list of candidate IDs that Nerve selected as HOT
    - effective_selected_ids: effective selected IDs (mode-dependent)
    - deferred_ids: IDs deferred by the admission controller
    - suppressed_ids: IDs suppressed by the admission controller
    - receipt: dict with mode, baseline_tokens, selected_tokens,
      effective_tokens, gross_tokens_saved, applied_tokens_saved, net_tokens_saved,
      and all decision-level metrics
    - fail_open: bool indicating if planner failed open

    Hermes remains the authority over which tools are deferrable. Nerve never sees
    core/direct tools. Fail-open preserves Hermes stock behavior.
    """
    try:
        from .selective_context import ContextCandidate, ContextAdmissionController, SurfaceConfig, estimate_tokens
        controller = _controller(settings, 'tools')
        # Convert deferrable schemas into Nerve tool candidates
        candidates = []
        for i, td in enumerate(tool_definitions):
            # Only process deferrable tools; core/direct are excluded by the caller
            fn = td.get('function', {}) if isinstance(td, Mapping) else {}
            name = fn.get('name') or td.get('name') or f'tool-{i}'
            desc = fn.get('description') or td.get('description') or ''
            schema = fn.get('parameters') or td.get('parameters') or '{}'
            # Estimate tokens from schema + description
            est_tokens = max(1, math.ceil(len(str(schema) + desc) / 4))
            # Build candidate with mandatory from settings if declared
            mandatory = bool(td.get('mandatory')) or bool(td.get('core'))
            candidates.append(ContextCandidate('tool', name, str(td.get('source', 'hermes')),
                                               name, desc, est_tokens,
                                               metadata={'toolset': td.get('toolset', ''), 'core': False},
                                               mandatory=mandatory))
        # Run admission selection
        result = controller.plan(goal=goal, candidates=candidates, kind='tool')
        # Extract dispositions
        selected_ids = result.get('selected_ids', [])
        deferred_ids = result.get('deferred_ids', [])
        suppressed_ids = result.get('suppressed_ids', [])
        receipt = {
            'mode': result.get('mode', 'shadow'),
            'baseline_tokens': result.get('baseline_tokens', 0),
            'selected_tokens': result.get('selected_tokens', 0),
            'effective_tokens': result.get('effective_tokens', 0),
            'gross_tokens_saved': result.get('gross_tokens_saved', 0),
            'applied_tokens_saved': result.get('applied_tokens_saved', 0),
            'net_tokens_saved': result.get('net_tokens_saved', 0),
            'fail_open': result.get('fail_open', True),
            'decisions': [{'candidate_id': d['candidate_id'],
                          'action': d['action'],
                          'reason_code': d.get('reason_code', ''),
                          'confidence': d.get('confidence', 0),
                          'score': d.get('score', 0)}
                         for d in result.get('decisions', [])],
            'selected_ids': selected_ids,
            'effective_selected_ids': result.get('effective_selected_ids', selected_ids),
            'deferred_ids': deferred_ids,
            'suppressed_ids': suppressed_ids,
        }
        # Always include fail_open from the result
        if not receipt['fail_open']:
            receipt['plans'] = result.get('plans', 1)
            receipt['candidates'] = len(candidates)
            receipt['admitted'] = len(selected_ids)
            receipt['deferred'] = len(deferred_ids)
            receipt['suppressed'] = len(suppressed_ids)
            receipt['baseline_tokens_delta'] = result.get('baseline_tokens', 0)
        return receipt
    except Exception:
        # Fail open: Hermes stock behavior - return empty/zero receipt
        return {
            'mode': 'off',
            'baseline_tokens': 0,
            'selected_tokens': 0,
            'effective_tokens': 0,
            'gross_tokens_saved': 0,
            'applied_tokens_saved': 0,
            'net_tokens_saved': 0,
            'fail_open': True,
            'decisions': [],
            'selected_ids': [],
            'effective_selected_ids': [],
            'deferred_ids': [],
            'suppressed_ids': [],
        }
MEMORY_SCHEMA = 'hermes-nerve-authorized-memory/v1'
_NEGATIVE = re.compile(r'\b(no|not|never|cannot|mustn.t|don.t|without|forbid|disallow)\b', re.I)


def select_memory_context(envelope, goal, settings, expected_scope):
    """Consume a structured registered-hook result, never parse provider text.

    Producer must authorize candidates BEFORE sending them. Exact scope/source
    identity is required. Bad/cross-scope entries fail closed; invalid config and
    planner errors retain the complete validated authorized subset. No dedup:
    every retained source ID, contradiction and different-authority byte survives.
    """
    if not isinstance(envelope, Mapping) or envelope.get('schema') != MEMORY_SCHEMA:
        raise ValueError('unknown structured memory schema')
    if envelope.get('scope') != expected_scope:
        raise ValueError('structured memory scope does not match active turn')
    raw = envelope.get('candidates')
    if not isinstance(raw, list):
        raise ValueError('structured candidates required')
    vetted, candidates = [], []
    for item in raw:
        if not isinstance(item, Mapping) or item.get('authorized') is not True or item.get('scope') != expected_scope:
            continue
        cid, source, text, trust = (item.get(k) for k in ('id', 'source', 'text', 'trust'))
        if (not isinstance(cid, str) or not cid or not isinstance(source, str) or not source
                or not isinstance(text, str) or type(trust) not in (int, float)
                or not math.isfinite(trust) or not 0 <= trust <= 1):
            continue

        # Preserve provider-declared mandatory/fresh/conflicting facts and all
        # negative assertions; do not infer trust, scope or policy from prose.
        mandatory = any(item.get(k) is True for k in ('mandatory', 'fresh', 'contradiction')) or bool(_NEGATIVE.search(text))
        vetted.append(item)
        candidates.append(ContextCandidate('memory', str(len(vetted) - 1), source, str(item.get('title', '')), text,
                            estimate_tokens(text), trust=trust, mandatory=mandatory))
    selected = {str(i) for i in range(len(vetted))}
    mode, reason = 'off', 'validated-stock'
    try:
        controller = _controller(settings, 'memory')
        if isinstance(goal, str) and goal.strip():
            plan = controller.plan(goal=goal, candidates=candidates, kind='memory')
            selected = set(plan['effective_selected_ids'])
            mode, reason = plan['mode'], 'planner-fail-open' if plan['fail_open'] else 'planned'
    except Exception:
        reason = 'invalid-settings-stock'
    effective = [item for i, item in enumerate(vetted) if str(i) in selected]
    # IDs/provenance emitted as literal data, not instruction text.
    import json
    text = '\n\n'.join(json.dumps({'id': i['id'], 'source': i['source'], 'trust': i['trust'],
                                  'scope': i['scope'], 'text': i['text']}, ensure_ascii=False)
                         for i in effective)
    return text, {'schema': MEMORY_SCHEMA, 'mode': mode, 'reason': reason,
                  'authorized_ids': [i['id'] for i in vetted], 'selected_ids': [i['id'] for i in effective]}

def experience_context_hook(**kwargs):
    """Offer this Hermes profile's verified experience to the structured hook.

    The active Hermes host validates the exact home/session/user scope before
    applying memory selection. No arbitrary conversation text or cross-profile
    memory is searched, and no provider call or storage mutation is needed.
    """
    goal = kwargs.get("user_message")
    if not isinstance(goal, str) or not goal.strip():
        return None
    try:
        from . import experience
        from .paths import hermes_home

        if not experience.enabled():
            return None
        sender = str(kwargs.get("sender_id") or "")
        scope = {
            "home": str(hermes_home()),
            "session": str(kwargs.get("session_id") or ""),
            "user": sender,
        }
        # Scope-aware retrieval refuses repository/user mismatches; only
        # verified or disproven claims with evidence are prompt candidates.
        rows = experience.default_store().retrieve(
            goal, scope={"user": sender} if sender else {},
            limit=24, max_tokens=1200, record=False)
        candidates = []
        for row in rows:
            claim = row.get("claim") or {}
            if claim.get("claim_type") not in ("verified", "disproven"):
                continue
            statement = claim.get("statement")
            claim_id = claim.get("claim_id")
            if not isinstance(statement, str) or not statement.strip() or not claim_id:
                continue
            confidence = claim.get("confidence")
            if type(confidence) not in (int, float) or not math.isfinite(confidence):
                continue
            candidates.append({
                "id": str(claim_id),
                "source": "nerve:verified-experience",
                "title": str(claim.get("subject") or "")[:160],
                "text": statement[:2400],
                "trust": max(0.0, min(1.0, float(confidence))),
                "authorized": True,
                "scope": scope,
                "mandatory": claim.get("claim_type") == "disproven",
            })
        return {"context": {"schema": MEMORY_SCHEMA, "scope": scope,
                            "candidates": candidates}} if candidates else None
    except Exception:
        # A memory index error must never interrupt the Hermes turn.
        return None