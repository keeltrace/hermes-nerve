# HermesContextBus

HermesContextBus is an optional Hermes plugin for durable shared context and agent-to-agent coordination across Hermes surfaces.

It provides:

- bounded shared context injection before model turns;
- durable handoffs after substantial completed turns;
- direct messages, inboxes, threads, and acknowledgements;
- a local SQLite/WAL backend that works without external services;
- an optional MCP blackboard backend when the operator has configured one;
- a human-readable Markdown projection of local shared state.

## Safety model

Shared-context content is coordination data only. It does not grant tool permission, create standing authorization, or bypass Hermes policy and approval boundaries.

## Install

From this checkout:

```bash
nerve setup --install-shared-context /path/to/hermes-context-bus
# or install directly:
python3 scripts/install_hermes_plugin.py
hermes plugins doctor ~/.hermes/plugins/hermes-context-bus --ci
```

The default `auto` backend requires no credential or external service: if the configured MCP blackboard is unavailable or denied, the plugin uses local SQLite/WAL storage under `~/.hermes/shared-context/`.

To force the local backend, set the plugin setting `backend: sqlite`. To use an MCP blackboard, configure that server in Hermes separately and set `backend: megamcp` (or leave `auto`). This package does not ship private MCP launcher configuration.

## Nerve compatibility

This preview declares plugin version `0.2.0` and the `shared_context_health` tool expected by Nerve's Shared Context adapter.

## Verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The tests cover protocol behavior, identity mapping, Hermes lifecycle hooks, direct messaging, backend failover, persistence, immutable history, atomic projection, concurrent writers, installer behavior, and the public health-tool contract.

## Publication note

This tree is the sanitized public-review surface. Machine-specific deployment notes, private repository references, local launcher paths, credentials, and maintainer-only instructions are intentionally excluded.