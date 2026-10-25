import pytest
from hermes_nerve.work.hooks import _native_dispatch_succeeded
from hermes_nerve.work.block_review import review_block, MAX_BLOCK_REVIEWS_PER_RUN


def test_native_dispatch_negated_completed_is_false():
    for text in ("native tool reported: Task not completed", "native tool reported: not completed",
                 "completed with 2 errors", "completed in 5s with 1 failure"):
        ok, _ = _native_dispatch_succeeded(text)
        assert ok is False, text


def test_native_dispatch_canonical_success_still_true():
    for result in ({"ok": True}, '{"ok": true}', "status=done", "status: done"):
        ok, _ = _native_dispatch_succeeded(result)
        assert ok is True, result


def test_block_review_two_strike_persists(tmp_path):
    # minimal fake store/dummy
    class Store:
        def __init__(self):
            self.diags = []
        def latest_diagnostic(self, task_id=None, run_id=None, kind=None):
            for d in reversed(self.diags):
                if d["kind"] == kind:
                    return d
            return None
        def add_diagnostic(self, **kw):
            self.diags.append(kw)
        def evidence(self, *a, **k):
            return []
    class Sup:
        store = None
        def active_contract(self, tid):
            return None
    store = Store()
    sup = Sup()
    sup.store = store
    from types import SimpleNamespace
    identity = SimpleNamespace(task_id="t", run_id=1)
    # first two blocks should be CONTINUE with increasing count
    for i in range(MAX_BLOCK_REVIEWS_PER_RUN):
        r = review_block(sup, identity, reason="lazy")
        # simulate hook persistence
        store.add_diagnostic(task_id=identity.task_id, run_id=identity.run_id,
                            kind="nerve_block_review_ran",
                            payload={"verdict": r.verdict, "reasons": r.reasons,
                                     "review_count": r.prior_reviews + 1})
    # next review should escalate
    r = review_block(sup, identity, reason="lazy")
    assert r.verdict == "ESCALATE", r.verdict
