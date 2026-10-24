"""Real hierarchy layouts must be two-dimensional, nested, and repeatable."""
import math
import unittest
from hermes_nerve.observatory import quilt


def fixture(runs=141, agents=1):
    return {
        "fleet": {"hosts": [{"id": "j2", "hostname": "j2"}]},
        "harnesses": [{"source": "controller", "freshness": {"state": "stale"}}],
        "agents": [
            {"worker_id": f"worker-{i:02d}", "sources": ["controller"], "freshness": {"state": "unknown"}}
            for i in range(agents)
        ],
        "runs": [
            {"task_id": f"t-{i:04d}", "run_id": i, "worker_id": f"worker-{i % agents:02d}",
             "sources": ["controller"], "freshness": {"state": "stale"}}
            for i in range(runs)
        ],
    }


def all_nodes(root):
    result = []
    todo = [root]
    while todo:
        node = todo.pop()
        result.append(node)
        todo.extend(node["children"])
    return result


class SpatialAtlasTests(unittest.TestCase):
    def test_141_real_runs_are_2d_not_a_tall_column(self):
        root = quilt.build_tree(fixture())
        x, y, w, h = root["bounds"]
        self.assertGreater(w, 1000)
        self.assertGreater(h, 1000)
        self.assertLess(max(w / h, h / w), 2.15)
        agent = root["children"][0]["children"][0]["children"][0]
        positions = [(r["bounds"][0], r["bounds"][1]) for r in agent["children"]]
        self.assertEqual(len(positions), 141)
        self.assertGreaterEqual(len({x for x, _ in positions}), 8)
        self.assertGreaterEqual(len({y for _, y in positions}), 8)

    def test_every_child_is_inside_its_real_parent_and_siblings_never_overlap(self):
        root = quilt.build_tree(fixture(500, 20))
        self.assertEqual(len(all_nodes(root)), 1 + 1 + 1 + 20 + 500)
        for parent in all_nodes(root):
            px, py, pw, ph = parent["bounds"]
            self.assertTrue(all(math.isfinite(n) and n >= 0 for n in (px, py, pw, ph)))
            children = parent["children"]
            for c in children:
                x, y, w, h = c["bounds"]
                self.assertGreater(w, 0)
                self.assertGreater(h, 0)
                self.assertGreaterEqual(x + 0.0001, px)
                self.assertGreaterEqual(y + 0.0001, py)
                self.assertLessEqual(x + w, px + pw + 1e-7)
                self.assertLessEqual(y + h, py + ph + 1e-7)
            for i, a in enumerate(children):
                x, y, w, h = a["bounds"]
                for b in children[i + 1:]:
                    bx, by, bw, bh = b["bounds"]
                    overlap = min(x + w, bx + bw) - max(x, bx) > 1e-7
                    overlap &= min(y + h, by + bh) - max(y, by) > 1e-7
                    self.assertFalse(overlap, f"overlap: {a['id']}, {b['id']}")

    def test_snapshot_ids_and_geometry_are_deterministic(self):
        data = fixture(141, 4)
        a = quilt.to_wire(quilt.build_tree(data))
        b = quilt.to_wire(quilt.build_tree(data))
        self.assertEqual(a, b)

    def test_real_task_runs_form_stable_spatial_neighborhoods(self):
        data = fixture(140, 1)
        for i, run in enumerate(data["runs"]):
            run["task_id"] = "task:alpha" if i % 2 == 0 else "task:beta"
        root = quilt.build_tree(data)
        agent = root["children"][0]["children"][0]["children"][0]
        labels = [n["label"].split(" #")[0] for n in agent["children"]]
        self.assertEqual(labels, ["task:alpha"] * 70 + ["task:beta"] * 70)
        self.assertEqual(len({n["id"] for n in agent["children"]}), 140)

    def test_adaptive_grid_remains_reasonable_at_high_run_counts(self):
        root = quilt.build_tree(fixture(3000, 1))
        w, h = root["bounds"][2:]
        self.assertLess(max(w / h, h / w), 2.3)
        agent = root["children"][0]["children"][0]["children"][0]
        coords = [(n["bounds"][0], n["bounds"][1]) for n in agent["children"]]
        self.assertEqual(len(coords), 3000)
        self.assertGreater(len({x for x, _ in coords}), 25)
        self.assertGreater(len({y for _, y in coords}), 25)

    def test_no_fake_children_for_empty_sources(self):
        data = fixture(0, 0)
        q = quilt.build_tree(data)
        self.assertEqual(len(q["children"]), 1)
        source = q["children"][0]["children"][0]
        self.assertEqual(source["children"], [])
        self.assertGreater(q["bounds"][2], 0)


if __name__ == "__main__":
    unittest.main()
