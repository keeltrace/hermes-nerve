# Next Investigations

## 1. Correlate raw nervous controls to exact next actions

Highest value. Read:

- `~/.hermes/profiles/<profile>/jev/nervous-events.jsonl`
- `~/.hermes/profiles/<profile>/jev/decision-outcomes.jsonl`
- `~/.hermes/profiles/<profile>/jev/receipts.jsonl`

Build a timestamp/event-ID timeline:

`tool result -> nervous event -> Jev control -> lease invalidation -> next model/tool action`

For each of the 15 `REPLAN` controls, determine whether the next material action changed semantically.

## 2. Prove/falsify pre-tool gate lifecycle failure

Run a fresh session with five controlled actions:

1. read-only file read
2. harmless terminal read
3. temp-file write
4. second temp-file write
5. temp-file mutation after a failure

Capture gate stats before and after each action. Repeat across one forced compression.

## 3. Add repeated-failure fingerprinting test

Use a deterministic failing command and assert:

- provider call occurs initially
- identical repeats are deduplicated
- by repetition 3, control forces replan/escalate
- identical action cannot run 33 times

## 4. Test context rehydration deliberately

Pin a unique durable fact, force compression, then require that fact for a later decision. Verify `rehydrations > 0` and trace the recovered evidence.

## 5. Redesign stats interface

Add compact/section/delta/limit modes and verify a normal stats call stays small enough to avoid main-model context poisoning.

## 6. Clarify metric scopes

For every stats subsection, label scope explicitly:

- lifetime/profile
- current process
- current session
- current turn
- rolling recent window

This is required before tuning thresholds because current counters are easy to misread.
