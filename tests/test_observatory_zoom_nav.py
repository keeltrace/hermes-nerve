"""Frame navigation, semantic LOD, culling, search and paging for the observatory quilt.

Pure-Python checks with no browser, network or clock. The JS mirror is covered by
test_observatory_quilt_ui.py, which runs the embedded code under Node.
"""

import math
import unittest

from hermes_nerve.observatory import camera as cam_mod
from hermes_nerve.observatory import quilt
from hermes_nerve.observatory.camera import Camera, Frame


def wire(node_id, kind, bounds, children=(), state="fresh", label=None):
    return {"id": node_id, "kind": kind, "label": label or node_id, "state": state,
            "bounds": list(bounds), "children": list(children)}


def sample_tree():
    run_a = wire("run:a", "run", (10, 10, 20, 20))
    run_b = wire("run:b", "run", (400, 400, 20, 20), state="stale")
    agent = wire("agent:w1", "agent", (0, 0, 500, 500), [run_a, run_b])
    src = wire("source:claude", "source", (0, 0, 600, 600), [agent])
    host = wire("host:local", "host", (0, 0, 1000, 700), [src])
    return wire("fleet", "fleet", (0, 0, 0, 0), [host])


class LocalBoundsTests(unittest.TestCase):
    def test_converts_global_to_frame_local(self):
        f = Frame(100.0, 50.0, 4.0)
        self.assertEqual(cam_mod.local_bounds((140, 90, 8, 4), f), (10.0, 10.0, 2.0, 1.0))

    def test_returns_none_for_invalid_input_and_never_raises(self):
        f = Frame(0.0, 0.0, 1.0)
        bad = [
            (float("nan"), 0, 1, 1), (0, float("inf"), 1, 1), (0, 0, 0, 1), (0, 0, 1, -1),
            ("1", 0, 1, 1), None, (0, 0, 1),
        ]
        for b in bad:
            self.assertIsNone(cam_mod.local_bounds(b, f), b)
        self.assertIsNone(cam_mod.local_bounds((0, 0, 1, 1), None))

    def test_frame_rejects_non_finite_origin_at_construction(self):
        with self.assertRaises(ValueError):
            Frame(float("nan"), 0, 1)

    def test_does_not_mutate_input(self):
        b = [1.0, 2.0, 3.0, 4.0]
        cam_mod.local_bounds(b, Frame(0.0, 0.0, 2.0))
        self.assertEqual(b, [1.0, 2.0, 3.0, 4.0])


class LodTests(unittest.TestCase):
    def test_levels_follow_pixel_edges(self):
        cases = {0.0: 0, 23.9: 0, 24.0: 1, 79.9: 1, 80.0: 2, 160.0: 3, 320.0: 4, 600.0: 5, 1e6: 5}
        for px, lvl in cases.items():
            self.assertEqual(cam_mod.lod_level(px), lvl, px)

    def test_non_finite_or_non_numeric_is_level_zero(self):
        for px in (float("nan"), float("inf"), -float("inf"), None, "600"):
            self.assertEqual(cam_mod.lod_level(px), 0, px)


class FrameStackTests(unittest.TestCase):
    def test_resolve_keeps_valid_prefix_and_drops_missing_ids(self):
        root = sample_tree()
        self.assertEqual(quilt.resolve_stack(root, ["host:local", "source:claude", "agent:w1"]),
                         ["fleet", "host:local", "source:claude", "agent:w1"])
        # A vanished middle node stops the walk at its nearest surviving ancestor.
        self.assertEqual(quilt.resolve_stack(root, ["host:local", "source:gone", "agent:w1"]),
                         ["fleet", "host:local"])

    def test_resolve_refuses_non_child_jumps(self):
        root = sample_tree()
        self.assertEqual(quilt.resolve_stack(root, ["agent:w1"]), ["fleet"])

    def test_resolve_handles_bad_input_without_raising(self):
        self.assertEqual(quilt.resolve_stack({}, ["x"]), [])
        self.assertEqual(quilt.resolve_stack(sample_tree(), None), ["fleet"])
        self.assertEqual(quilt.resolve_stack(sample_tree(), [None, "host:local"]), ["fleet"])

    def test_find_node_returns_none_for_missing(self):
        root = sample_tree()
        self.assertIsNone(quilt.find_node(root, "run:nope"))
        self.assertEqual(quilt.find_node(root, "run:a")["label"], "run:a")

    def test_duplicate_ids_are_refused(self):
        dup = wire("fleet", "fleet", (0, 0, 0, 0), [wire("x", "host", (0, 0, 1, 1)), wire("x", "host", (0, 0, 1, 1))])
        with self.assertRaises(ValueError):
            quilt.index_wire(dup)


class CullingTests(unittest.TestCase):
    def test_only_intersecting_nodes_are_returned(self):
        root = sample_tree()
        host = root["children"][0]
        out = quilt.cull_wire(host, (0, 0, 100, 100), traverse_budget=100, render_budget=100)
        ids = [n["id"] for n in out["nodes"]]
        self.assertIn("source:claude", ids)
        self.assertIn("run:a", ids)
        self.assertNotIn("run:b", ids)
        self.assertFalse(out["truncated"])

    def test_render_budget_reports_truncation(self):
        root = sample_tree()
        out = quilt.cull_wire(root, (0, 0, 1000, 1000), traverse_budget=100, render_budget=2)
        self.assertEqual(len(out["nodes"]), 2)
        self.assertTrue(out["truncated"])

    def test_traverse_budget_reports_truncation(self):
        root = sample_tree()
        out = quilt.cull_wire(root, (0, 0, 1000, 1000), traverse_budget=1, render_budget=100)
        self.assertEqual(out["examined"], 1)
        self.assertTrue(out["truncated"])

    def test_negative_budgets_are_refused(self):
        with self.assertRaises(ValueError):
            quilt.cull_wire(sample_tree(), (0, 0, 1, 1), traverse_budget=-1, render_budget=1)


class SearchAndPagingTests(unittest.TestCase):
    def test_search_matches_id_label_state_case_insensitively(self):
        root = sample_tree()
        self.assertEqual([n["id"] for n in quilt.search_wire(root, "STALE")], ["run:b"])
        self.assertEqual([n["id"] for n in quilt.search_wire(root, "agent:W1")], ["agent:w1"])

    def test_empty_query_matches_nothing(self):
        self.assertEqual(quilt.search_wire(sample_tree(), "   "), [])

    def test_paging_clamps_and_reports_totals(self):
        items = list(range(5))
        p = quilt.page_items(items, 9, 2)
        self.assertEqual(p, {"items": [4], "page": 2, "pages": 3, "total": 5})
        self.assertEqual(quilt.page_items([], 0, 3)["pages"], 1)
        with self.assertRaises(ValueError):
            quilt.page_items(items, 0, 0)


class TwentyAgentFixtureTests(unittest.TestCase):
    """20 real-shaped agents: every one must have an indicator reachable in the tree."""

    def test_each_agent_has_a_reachable_indicator(self):
        agents = [{"worker_id": f"w{i:02d}", "sources": ["claude"], "freshness": {"state": "fresh"}} for i in range(20)]
        runs = [{"task_id": "t", "run_id": i, "worker_id": f"w{i:02d}", "sources": ["claude"], "freshness": {"state": "fresh"}} for i in range(20)]
        snapshot = {
            "fleet": {"hosts": [{"id": "local", "hostname": "j2"}], "note": "n"},
            "harnesses": [{"source": "claude", "freshness": {"state": "fresh"}}],
            "agents": agents,
            "runs": runs,
        }
        root = quilt.to_wire(quilt.build_tree(snapshot))
        found = quilt.search_wire(root, "agent")
        agent_nodes = [n for n in found if n["kind"] == "agent"]
        self.assertEqual(len(agent_nodes), 20)
        for n in agent_nodes:
            self.assertTrue(all(math.isfinite(v) for v in n["bounds"]))
            self.assertGreater(n["bounds"][2], 0)


if __name__ == "__main__":
    unittest.main()
