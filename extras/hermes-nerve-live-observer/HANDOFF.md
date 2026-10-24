# Nerve live observer: integration handoff (STAGING, not installed)

Package: /home/user/.local/share/infinite-zoom-control-room/live-bridge-staging/hermes-nerve-live-observer
Status: staged only. No live install, no plugin config change, no profile change, no service restart.

## What it does
- Observe-only Hermes plugin. Hooks: on_session_start, on_session_end, post_tool_call, post_api_request, api_request_error, subagent_start, subagent_stop.
- Every callback accepts **kwargs, returns None, and swallows exceptions. No pre-hooks, no tool calls, no mutation of Hermes state.
- Off unless HERMES_NERVE_LIVE_ENABLED=1. When off, register() returns before creating any directory, file, thread or hook.
- Output: per-process events-<pid>-<gen>.jsonl plus lease-<pid>-<gen>.json. Schema nerve-live-event/v1 and nerve-live-lease/v1.
- Directory 0700, files 0600. Events capped at 2 MB with one .1 backup. Lines capped at 1 KB.
- Session and child IDs become HMAC-SHA256 truncated to 16 hex, keyed by a random 32-byte key stored as .hmac_key (0600) in the output dir.
- Heartbeat every 5 s (spec says <=10 s), written atomically via tmp plus os.replace. Uses a monotonic uptime.
- Lease classification: live, stale (>30 s old), dead (pid gone), exited, invalid.

## Allowlist (the only fields that can be emitted)
sess, child (hashed), tool (charset-checked name), model and provider (charset-checked, lowercase), status (enum), dur_ms, role (charset-checked), usage (numeric token counts only). Everything else is dropped, not truncated.

## Test status
Command: cd hermes-nerve-live-observer && python3 -B -W error::ResourceWarning -m unittest discover -s tests -v
Result: 36 tests, OK (PASS). Also verified: the off-path probe creates no output dir and starts no threads, and registers no hooks.

## Fixes made during testing
- Model regex was case-insensitive. A canary inside a model string leaked. Now lowercase-only.

## Integration handoff (for the later source integration, not done now)
1. Copy the package to the plugin dir only after explicit approval. Do not edit live Nerve worktree files.
2. Opt-in canary: set HERMES_NERVE_LIVE_ENABLED=1 and HERMES_NERVE_LIVE_DIR to a scratch dir in one profile. Confirm lease files appear at 5 s cadence, events carry hashed sess values, and nothing raw leaks (grep for a known test string).
3. Reader side (not built here): read_leases() is the read-only status function. Gate the UI on status=live and treat stale/dead as offline.
4. Duplicate-hook guard: register() is idempotent per process. Do not also load this plugin from a second path.
5. Rollback: unset HERMES_NERVE_LIVE_ENABLED, or remove the plugin dir. Existing output can be deleted by the owner.
