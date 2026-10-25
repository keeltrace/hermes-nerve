# HermesContextBus v0.2 specification

Status: IMPLEMENTED / LIVE DEPLOYMENT UNVERIFIED

## Mission

Provide continual, transparent shared context and agent-to-agent coordination across Discord Hermes, WhatsApp Hermes, command-line Hermes, and desktop Hermes.

## Stable identities

- hermes-whatsapp
- hermes-discord
- hermes-cli
- hermes-desktop

## Storage authority

One backend is authoritative for a running Hermes process:

1. MegaMCP bb.* is preferred when available and authorized.
2. SQLite/WAL at ~/.hermes/shared-context/context.db is the automatic fallback.

Auto mode is sticky after backend selection to prevent one process from silently splitting writes between stores.

The Markdown projection and optional Google Drive mirror are read-oriented projections only.

## Per-turn behavior

Before each model turn on a recognized surface, the plugin MUST:

1. identify the stable surface ID;
2. read recent shared-board entries;
3. read that surface's unread inbox;
4. inject a bounded context block through Hermes' ephemeral pre_llm_call context channel;
5. state that shared content is coordination data rather than authority.

After a completed model turn, the plugin MUST:

1. acknowledge only inbox messages injected into that completed turn;
2. for a substantive response, append a bounded handoff with provenance;
3. fail open if the context transport is unavailable so ordinary Hermes use continues.

## Explicit coordination tools

The plugin exposes:

- shared_context_post
- shared_context_send
- shared_context_inbox
- shared_context_thread
- shared_context_ack

Sender identity is derived from the current session/surface, not supplied by model arguments.

## Local forum invariants

The SQLite backend MUST:

- use WAL and a bounded busy timeout for multi-process access;
- keep board entries immutable;
- preserve messages across restart;
- support direct inboxes, thread IDs, and acknowledgement;
- atomically replace the Markdown projection using unique temporary files;
- preserve all successful concurrent writes.

## MegaMCP boundary

The plugin uses Hermes PluginContext.call_mcp rather than opening a parallel raw connection.

MegaMCP blackboard access is not assumed. A POLICY_DENIED or unavailable MCP path in auto mode selects SQLite fallback. Security policy is not modified by this project.

## Acceptance criteria

Implementation-level acceptance:
1. stable IDs map correctly for WhatsApp, Discord, CLI/TUI, and Desktop;
2. each recognized surface receives per-turn shared context;
3. direct messages use a stable sender and recipient identity;
4. threads and acknowledgements work;
5. completed substantive turns create bounded handoffs;
6. persistence survives store reopen;
7. board entries cannot be updated/deleted;
8. concurrent writers preserve all writes;
9. Markdown projection remains atomic under concurrent writers;
10. MegaMCP policy denial cleanly falls back to the same local forum.

Live-deployment acceptance remains separate: all four real running surfaces must demonstrate an end-to-end round trip after installation.