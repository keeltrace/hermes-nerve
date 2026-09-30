# Architecture

    WhatsApp Hermes ----+
    Discord Hermes -----+
    CLI Hermes ---------+--> Hermes general plugin
    Desktop Hermes -----+       |
                                +-- pre_llm_call: read + inject context
                                +-- post_llm_call: ack + bounded handoff
                                +-- explicit shared_context_* tools
                                |
                                v
                         backend selector (sticky)
                         /                     \
                MegaMCP bb.*              SQLite/WAL
                when allowed              local forum
                                                |
                                                v
                                     shared-context.md
                                                |
                                                v
                                  optional Drive mirror

## Why one Hermes plugin

Current Hermes routes gateway platforms through the same agent lifecycle and exposes general plugin hooks to both gateway and local surfaces. One plugin avoids four divergent integrations while preserving four stable identities.

## Read path

pre_llm_call executes on each user turn. The context block is ephemeral and appended through Hermes' native user-context channel, so the system prompt remains cache-stable and shared context can update during an existing session.

## Write path

post_llm_call observes the completed turn and creates a bounded handoff for substantive responses. Explicit tools support direct agent-to-agent coordination.

## Authority

Messages are inert coordination data. A recipient still owns every action it takes and remains subject to normal tool policy and approvals.

## Backend selection

auto prefers MegaMCP. If the MCP server is absent/unavailable, the plugin lacks MCP allowlist access, or MegaMCP returns POLICY_DENIED for bb.*, the process selects SQLite. Backend choice is sticky for the process to avoid split-brain.

SQLite is the immediate deployment path. An operator-configured MCP blackboard can be used as the canonical backend when its capabilities are legitimately enabled.