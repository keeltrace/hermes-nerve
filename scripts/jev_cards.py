#!/usr/bin/env python3
"""Jev card reader — the human judge's one-command view of worker blocks.

Usage:
    python3 jev_cards.py                 # latest cards across all tasks
    python3 jev_cards.py <task_id>       # cards for one task
    python3 jev_cards.py --stats         # today's verdict ledger
"""
import json
import os
import sqlite3
import sys

def _db_candidates() -> list[str]:
    """Supervision DB locations, most specific first. Covers dedicated worker
    profiles (profiles/<name>/nerve/) and the default profile layout."""
    hermes = os.path.expanduser("~/.hermes")
    return [
        os.environ.get("NERVE_SUPERVISION_DB", ""),
        os.path.join(hermes, "nerve", "work-supervision.sqlite3"),
        os.path.join(hermes, "profiles", "worker", "nerve", "work-supervision.sqlite3"),
    ]


def _open_db() -> sqlite3.Connection:
    last_err: Exception | None = None
    for path in _db_candidates():
        if not path or not os.path.isfile(path):
            continue
        try:
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:  # unreadable/corrupt -> try next candidate
            last_err = exc
    raise SystemExit(
        "no supervision DB found (set NERVE_SUPERVISION_DB or run inside a nerve worker profile)"
    ) from last_err


def main() -> int:
    con = _open_db()
    con.row_factory = sqlite3.Row
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    if "--stats" in sys.argv:
        rows = con.execute(
            """SELECT task_id, COUNT(*) n,
                      SUM(CASE WHEN json_extract(payload_json,'$.verdict')='ESCALATE' THEN 1 ELSE 0 END) esc,
                      SUM(CASE WHEN json_extract(payload_json,'$.verdict')='CONTINUE' THEN 1 ELSE 0 END) cont,
                      SUM(CASE WHEN json_extract(payload_json,'$.verdict')='REFUTE' THEN 1 ELSE 0 END) ref
               FROM supervision_diagnostics
               WHERE kind='jev_block_review_card'
               GROUP BY task_id ORDER BY rowid DESC LIMIT 15"""
        ).fetchall()
        print(f"{'task':<14} {'cards':>5} {'ESCALATE':>9} {'CONTINUE':>9} {'REFUTE':>7}")
        for r in rows:
            print(f"{r['task_id']:<14} {r['n']:>5} {r['esc'] or 0:>9} {r['cont'] or 0:>9} {r['ref'] or 0:>7}")
        return 0

    if args:
        rows = con.execute(
            """SELECT created_at, payload_json FROM supervision_diagnostics
               WHERE kind='jev_block_review_card' AND task_id=?
               ORDER BY rowid""",
            (args[0],),
        ).fetchall()
    else:
        rows = con.execute(
            """SELECT created_at, payload_json FROM supervision_diagnostics
               WHERE kind='jev_block_review_card' ORDER BY rowid DESC LIMIT 5"""
        ).fetchall()

    if not rows:
        print("no cards found")
        return 0

    for r in rows:
        d = json.loads(r["payload_json"])
        print(f"== {r['created_at'][:19]}  {d.get('task_id')}  verdict: {d.get('verdict')}")
        print(f"   variants: {d.get('variants_tried')}  budget: {d.get('budget_fraction_consumed')}"
              f"  refuted: {len(d.get('refuted_claims') or [])}"
              f"  flags: {d.get('semantic_laziness_flags') or []}")
        block = str(d.get("block_reason") or "").strip().replace("\n", " ")
        print(f"   says: {block[:220]}{'...' if len(block) > 220 else ''}")
        fb = d.get("judge_feedback")
        if fb:
            print(f"   judge feedback: {str(fb)[:220]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
