import json
import pytest

from hermes_nerve.consumer import (
    MEMORY_SCHEMA,
    _controller,
    select_memory_context,
    select_skills_index,
    select_tools_context,
)


def td(name, desc):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_tools_controller_uses_tool_surface_defaults():
    c = _controller({"selective_context_tools_mode": "apply"}, "tools")
    assert c.config.tools.mode == "apply"
    assert c.config.tools.max_candidates == 128
    assert c.config.tools.max_admitted == 16
    assert c.config.tools.min_score == pytest.approx(0.18)


def test_tools_consumer_real_relevance_changes_by_goal():
    defs = [
        td("github_pull_request_review", "Review GitHub pull requests diffs code changes"),
        td("gmail_search", "Search email Gmail inbox messages from Alice"),
        td("calendar_search", "Search calendar meetings events"),
    ]
    settings = {
        "selective_context_enabled": True,
        "selective_context_tools_mode": "apply",
        "selective_context_tools_max_admitted": 1,
    }
    github = select_tools_context(defs, "review a GitHub pull request", settings)
    email = select_tools_context(defs, "find emails from Alice", settings)
    assert not github["fail_open"] and not email["fail_open"]
    assert github["effective_selected_ids"] == ["github_pull_request_review"]
    assert email["effective_selected_ids"] == ["gmail_search"]


def test_tools_consumer_invalid_settings_fails_open():
    result = select_tools_context(
        [td("gmail_search", "Search email")],
        "find email",
        {"selective_context_tools_mode": "invalid"},
    )
    assert result["mode"] == "off"
    assert result["fail_open"] is True
    assert result["effective_selected_ids"] == []


def test_memory_consumer_apply_selects_relevant_authorized_candidate():
    scope = "session:test"
    env = {
        "schema": MEMORY_SCHEMA,
        "scope": scope,
        "candidates": [
            {
                "id": "release",
                "source": "memory",
                "text": "Nerve release blocker qualification and regression testing",
                "trust": 1.0,
                "scope": scope,
                "authorized": True,
            },
            {
                "id": "van",
                "source": "memory",
                "text": "green van maintenance tire pressure",
                "trust": 0.8,
                "scope": scope,
                "authorized": True,
            },
        ],
    }
    text, receipt = select_memory_context(
        env,
        "review Nerve release blockers",
        {
            "selective_context_memory_mode": "apply",
            "selective_context_memory_max_admitted": 1,
        },
        scope,
    )
    assert receipt["mode"] == "apply"
    assert receipt["selected_ids"] == ["release"]
    assert '"id": "release"' in text
    assert '"id": "van"' not in text


def test_skills_consumer_apply_demotes_irrelevant_description_without_losing_name():
    stock = (
        "<available_skills>\n"
        "    - github-review: Review GitHub pull requests diffs code changes\n"
        "    - email-triage: Search and organize email inbox messages\n"
        "</available_skills>"
    )
    out = select_skills_index(
        stock,
        "review a GitHub pull request",
        {
            "selective_context_skills_mode": "apply",
            "selective_context_skills_max_admitted": 1,
        },
    )
    assert "github-review: Review GitHub pull requests diffs code changes" in out
    assert "    - email-triage\n" in out
    assert "email-triage: Search and organize" not in out


def _tool_def(name, desc):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _tool_settings(max_admitted=16):
    return {
        "selective_context_enabled": True,
        "selective_context_tools_mode": "apply",
        "selective_context_tools_max_candidates": 128,
        "selective_context_tools_max_admitted": max_admitted,
    }


def test_tool_precision_obvious_single_domain_stays_compact():
    defs = [
        _tool_def("nerve_stats", "Return Nerve statistics and metrics"),
        _tool_def("nerve_rank", "Rank Nerve candidates by relevance"),
        _tool_def("shared_context_health", "Check shared context service health"),
        _tool_def("desktop_project", "Create switch or list desktop projects"),
    ]
    result = select_tools_context(defs, "Nerve statistics", _tool_settings())
    assert result["selected_ids"] == ["nerve_stats"]


def test_tool_precision_named_absent_domain_does_not_match_generic_health():
    defs = [
        _tool_def("shared_context_health", "Check shared context service health"),
        _tool_def("drive_preview", "Preview a file from Google Drive"),
    ]
    result = select_tools_context(defs, "check Paperclip health", _tool_settings())
    assert result["selected_ids"] == []


def test_tool_precision_multitool_request_keeps_both_specific_capabilities():
    defs = [
        _tool_def("resource_budget_status", "Inspect resource budget limits and remaining capacity"),
        _tool_def("hermesx_cpu_pools", "Inspect CPU pools and processor allocation"),
        _tool_def("shared_context_health", "Check shared context service health"),
    ]
    result = select_tools_context(defs, "check resource budget and CPU pools", _tool_settings())
    assert set(result["selected_ids"]) == {"resource_budget_status", "hermesx_cpu_pools"}


def test_tool_precision_no_tool_request_can_return_empty_hot_set():
    defs = [
        _tool_def("nerve_context_explain", "Explain Nerve context selection decisions"),
        _tool_def("process_manage", "Poll wait on or kill background processes"),
    ]
    result = select_tools_context(defs, "explain what recursion is", _tool_settings())
    assert result["selected_ids"] == []


def test_tool_precision_semantic_paperclip_aliases_use_one_hot_slot():
    defs = [
        _tool_def("mcp__megamcp__paperclip_health", "Check live Paperclip API health"),
        _tool_def("mcp__paperclip__paperclip_health", "Check live Paperclip API health"),
        _tool_def("shared_context_health", "Check shared context service health"),
    ]
    result = select_tools_context(defs, "check Paperclip health", _tool_settings())
    aliases = [n for n in result["selected_ids"] if n.endswith("paperclip_health")]
    assert len(aliases) == 1
    decisions = {d["candidate_id"]: d for d in result["decisions"]}
    other = ({"mcp__megamcp__paperclip_health", "mcp__paperclip__paperclip_health"} - set(aliases)).pop()
    assert decisions[other]["action"] == "defer"
    assert decisions[other]["reason_code"] == "SEMANTIC_ALIAS"


def test_tool_precision_ignores_response_formatting_noise():
    defs = [
        _tool_def("nerve_stats", "Return bounded local Nerve Reflex statistics telemetry"),
        _tool_def("nerve_decide", "Make one bounded typed decision through the configured Reflex backend"),
    ]
    result = select_tools_context(
        defs,
        "Check Nerve statistics using the appropriate tool. Reply with one sentence.",
        _tool_settings(),
    )
    assert result["selected_ids"] == ["nerve_stats"]


def test_tool_precision_same_domain_action_noise_does_not_fill_hot():
    defs = [
        _tool_def("shared_context_health", "Check shared context service health"),
        _tool_def("shared_context_inbox", "Read shared context inbox messages"),
        _tool_def("shared_context_ack", "Acknowledge a shared context inbox message"),
    ]
    result = select_tools_context(defs, "Check shared context health and the shared context inbox. Read only; summarize concisely.", _tool_settings())
    assert set(result["selected_ids"]) == {"shared_context_health", "shared_context_inbox"}
