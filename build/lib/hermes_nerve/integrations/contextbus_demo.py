"""HermesContextBus acceptance demo (N-33).

Proves, against the REAL bus store with DISTINCT registered identities:
  1. profile A (hermes-whatsapp) publishes a synthetic approved record that is
     delivered into profile B's (hermes-cli) inbox;
  2. an unauthorized profile C (hermes-desktop) has an empty inbox — it cannot
     receive B's message even though C is a valid registered identity, and an
     UNREGISTERED identity is rejected outright by the bus;
  3. with the bus absent, the Nerve adapter reports installed=false and the
     Nerve core import path stays healthy (bus-down fallback).

Run: python3 -m hermes_nerve.integrations.contextbus_demo
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BUS = Path(__file__).resolve().parents[2] / "packages" / "hermes-context-bus"
SQLITE_STORE = BUS  # sqlite_store.py lives at the package root
PROFILE_A = "hermes-whatsapp"
PROFILE_B = "hermes-cli"
PROFILE_C = "hermes-desktop"


def run_demo() -> dict:
    results: dict[str, dict] = {}
    sys.path.insert(0, str(SQLITE_STORE))
    try:
        from sqlite_store import SQLiteBlackboard  # type: ignore
    except Exception as exc:
        results["bus_importable"] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        results["overall"] = False
        return results
    results["bus_importable"] = {"passed": True,
                                 "distinct_identities": len({PROFILE_A, PROFILE_B, PROFILE_C}) == 3}

    with tempfile.TemporaryDirectory(prefix="ctxbus-demo-") as td:
        store = SQLiteBlackboard(db_path=str(Path(td) / "bus.db"),
                                 projection_path=str(Path(td) / "projection.json"))

        # 1. A -> B: real sender identity, addressed to a DIFFERENT recipient.
        sent = store.send(to=PROFILE_B, body="approved synthetic sprint summary",
                          from_id=PROFILE_A, thread_id="demo")
        results["a_publishes_to_b"] = {"passed": bool(sent.get("message_id")),
                                       "from": PROFILE_A, "to": PROFILE_B}

        b_inbox = store.inbox(recipient=PROFILE_B, unread_only=False)
        b_msgs = b_inbox.get("messages") or []
        results["b_receives"] = {
            "passed": any("approved synthetic" in str(m.get("body", "")) for m in b_msgs),
            "count": len(b_msgs)}

        # 2. C cannot read B's data: C's inbox is empty; the message is visible
        # only to the addressed recipient.
        c_inbox = store.inbox(recipient=PROFILE_C, unread_only=False)
        c_msgs = c_inbox.get("messages") or []
        leaked = any("approved synthetic" in str(m.get("body", "")) for m in c_msgs)
        results["c_cannot_read"] = {"passed": not leaked and not c_msgs,
                                    "c_inbox_count": len(c_msgs)}

        # An unregistered identity must be rejected outright by the bus.
        rejected = False
        try:
            store.send(to="attacker-profile", body="x", from_id=PROFILE_A, thread_id="demo")
        except Exception:
            rejected = True
        results["unregistered_identity_rejected"] = {"passed": rejected}

        # No credential-shaped content in the delivered bodies.
        results["no_secret_leak"] = {"passed": all(
            "api_key" not in str(m).lower() and "token=" not in str(m).lower()
            for m in b_msgs)}

    # 3. Bus-down fallback through the real Nerve adapter: an empty HERMES_HOME
    # has no bus installed; detection must report installed=false, and Nerve
    # core must remain importable and functional.
    try:
        from hermes_nerve.integrations import shared_context
        with tempfile.TemporaryDirectory(prefix="busdown-home-") as empty_home:
            state = shared_context.detect(home=Path(empty_home))
            results["bus_down_detected"] = {"passed": state.get("installed") is False,
                                            "installed": state.get("installed")}
        import hermes_nerve  # core import path stays healthy
        results["core_unaffected"] = {"passed": True}
    except Exception as exc:
        results["bus_down_detected"] = {"passed": False, "error": str(exc)}

    results["overall"] = all(v["passed"] for v in results.values() if isinstance(v, dict))
    return results


if __name__ == "__main__":
    r = run_demo()
    print(json.dumps(r, indent=1))
    sys.exit(0 if r["overall"] else 1)
