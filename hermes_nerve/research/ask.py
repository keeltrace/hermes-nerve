"""DRK entry point: python3 -m hermes_nerve.research.ask "question" [--url Name=URL ...]
Runs the grounded pipeline: candidates -> live open-now verification -> referee
-> bunny synthesis over the registry -> citation lint. Receipts printed."""
from __future__ import annotations
import argparse, json, sys, threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .budget import BBQ_BUDGET, BudgetGovernor
from .grounded_lane import grounded_open_now
from .ledger import Ledger
from .lint import citation_lint, lint_claims
from .providers import bunny_call
from .referee import referee
from .reputation import reputation_claims

DEFAULT_CANDIDATES = {  # the validation fixture
    "Truth BBQ": "https://truthbbq.com/pages/locations",
    "The Pit Room": "https://thepitroomhouston.cafecityguide.website",
}

def main():
    # convenience: if provider creds aren't exported, load the Hermes env file
    # (same keys: MUNA_API_BASE, MUNA_API_KEY, NOUS_API_TOKEN)
    import os
    if not os.environ.get("NOUS_API_TOKEN") or not os.environ.get("MUNA_API_KEY"):
        env_file = Path.home() / ".hermes/profiles/freebrain/.env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--url", action="append", default=[], help="Name=URL (repeatable)")
    a = ap.parse_args()
    cands = dict(x.split("=", 1) for x in a.url) or DEFAULT_CANDIDATES
    ledger = Ledger()
    run_id = ledger.start_run(a.question)
    now = datetime.now(ZoneInfo("America/Chicago"))
    gov = BudgetGovernor(BBQ_BUDGET)
    open_claims = {}
    def v(n, u):
        c, _ = grounded_open_now(ledger, run_id, n, u, now)
        open_claims[n] = [c]
    th = threading.Thread(target=lambda: [v(n, u) for n, u in cands.items()]); th.start()
    rep = reputation_claims(ledger, run_id, list(cands), now, gov)
    th.join()
    surv, contra = [], []
    for n in cands:
        d = referee(n, open_claims.get(n, []) + ([rep[n]] if n in rep else []))
        ledger.referee_decision(run_id, n, d.passed, d.reasons)
        (surv if d.passed else contra).append(n)
        print(f"  referee: {n:16s} {'PASS' if d.passed else 'REJECT'}")
    registry = ledger.run_claims(run_id)
    reg_lines = [f"[{c['status'].upper()}|{'BLOCKING' if c['blocking'] else 'soft'}] {c['text']} ({c['candidate']})" for c in registry]
    prompt = (f"QUESTION: {a.question}\n\nVERIFIED CLAIM REGISTRY:\n" + "\n".join(reg_lines)
              + f"\n\nSelectable (passed all blocking gates): {', '.join(surv) or 'NONE'}.\n"
                "Answer in under 100 words: one pick, why, one caveat. Unverified info must be hedged.")
    final = bunny_call("stealth/space-bunny-alpha", prompt)
    ok, viols = citation_lint(final, surv, contra)
    ok2, v2 = lint_claims(final, registry); viols += v2; ok = ok and ok2
    if not ok:
        final = bunny_call("stealth/space-bunny-alpha", prompt +
            "\n\nLINT VIOLATIONS: " + "; ".join(viols) + "\nRewrite: hedge or drop unverified assertions.")
        ok, viols = citation_lint(final, surv, contra)
        ok2, v2 = lint_claims(final, registry); viols += v2; ok = ok and ok2
    print(f"lint: {'PASS' if ok else 'FAIL'}\n\n{final}")
    print(f"\nreceipts: {Path(ledger.db.execute('SELECT 1').fetchone() and 'sqlite ledger')}, run {run_id}")
    ledger.close()

if __name__ == "__main__":
    main()
