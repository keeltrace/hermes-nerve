import math
import unittest

from hermes_nerve.observatory import quilt
from hermes_nerve.observatory.camera import intersects


def snap(hosts=1, sources=(), agents=(), runs=()):
    return {
        "fleet": {"hosts": [{"id": "local", "hostname": "j2"} for _ in range(hosts)], "note": "n"},
        "harnesses": [{"source": s, "freshness": {"state": "fresh"}} for s in sources],
        "agents": list(agents),
        "runs": list(runs),
    }


def inside(child, parent, tol=1e-6):
    cx, cy, cw, ch = child
    px, py, pw, ph = parent
    return cx >= px - tol and cy >= py - tol and cx + cw <= px + pw + tol and cy + ch <= py + ph + tol


class LayoutTests(unittest.TestCase):
    def test_empty_snapshot_yields_only_the_fleet_root(self):
        root = quilt.build_tree(snap(hosts=0))
        self.assertEqual(root["id"], "fleet")
        self.assertEqual(root["children"], [])

    def test_no_descendants_are_invented(self):
        # One host, no sources/agents/runs: the host exists, nothing beneath it.
        root = quilt.build_tree(snap(hosts=1))
        host = root["children"][0]
        self.assertEqual(host["id"], "host:local")
        self.assertEqual(host["children"], [])

    def test_hierarchy_mirrors_real_records_only(self):
        s = snap(
            sources=["claude"],
            agents=[{"worker_id": "w1", "sources": ["claude"], "freshness": {"state": "stale"}}],
            runs=[{"task_id": "t1", "run_id": 7, "worker_id": "w1", "sources": ["claude"], "freshness": {"state": "fresh"}}],
        )
        root = quilt.build_tree(s)
        host = root["children"][0]
        src = host["children"][0]
        agent = src["children"][0]
        run = agent["children"][0]
        self.assertEqual([src["id"], agent["id"], run["id"]],
                         ["source:local:claude", "agent:local:claude:w1", "run:local:claude:w1:t1#7"])
        self.assertEqual(agent["state"], "stale")
        self.assertEqual(run["state"], "fresh")

    def test_unknown_freshness_is_unknown_not_fresh(self):
        s = snap(sources=["x"])
        s["harnesses"][0]["freshness"] = None
        root = quilt.build_tree(s)
        src = root["children"][0]["children"][0]
        self.assertEqual(src["state"], quilt.UNKNOWN)

    def test_ids_are_stable_across_rebuilds(self):
        s = snap(sources=["a", "b"], agents=[{"worker_id": "w", "sources": ["a"], "freshness": None}])
        first = quilt.index_by_id(quilt.build_tree(s))
        second = quilt.index_by_id(quilt.build_tree(s))
        self.assertEqual(sorted(first), sorted(second))
        for k in first:
            self.assertEqual(first[k]["bounds"], second[k]["bounds"], k)

    def test_duplicate_ids_are_refused(self):
        root = quilt.build_tree(snap(hosts=1))
        root["children"].append(dict(root["children"][0]))
        with self.assertRaises(ValueError):
            quilt.index_by_id(root)

    def test_children_are_inside_their_parent_bounds(self):
        s = snap(
            sources=["a", "b", "c"],
            agents=[{"worker_id": "w", "sources": ["a"], "freshness": None}],
            runs=[{"task_id": "t", "run_id": i, "worker_id": "w", "sources": ["a"], "freshness": None} for i in range(3)],
        )
        index = quilt.index_by_id(quilt.build_tree(s))
        for node in index.values():
            for child in node["children"]:
                if node["bounds"][2] > 0:
                    self.assertTrue(inside(child["bounds"], node["bounds"]), (node["id"], child["id"]))

    def test_visible_nodes_follow_the_camera_rect(self):
        s = snap(sources=["a", "b"])
        root = quilt.build_tree(s)
        host = root["children"][0]
        # A rect covering the host shows the fleet root (it has bounds), the host
        # and its two sources; nothing outside the host is returned.
        rect = host["bounds"]
        names = sorted(n["id"] for n in quilt.visible_nodes(root, rect, intersects))
        self.assertEqual(names, ["fleet", "host:local", "source:local:a", "source:local:b"])


class MultiHostRegressionTests(unittest.TestCase):
    """Repeated sources/agents/runs across several hosts must stay unique and contained."""

    def _stress(self):
        hosts = [{"id": "h%d" % i, "hostname": "x"} for i in range(3)]
        srcs = ["claude", "codex", "hermes"]
        agents = [{"worker_id": "w%d" % i, "sources": srcs, "freshness": {"state": "fresh"}} for i in range(4)]
        runs = [{"task_id": "t%d" % i, "run_id": i, "worker_id": "w%d" % (i % 4),
                 "sources": srcs, "freshness": {"state": "stale"}} for i in range(9)]
        return {
            "fleet": {"hosts": hosts, "note": "n"},
            "harnesses": [{"source": x, "freshness": {"state": "fresh"}} for x in srcs],
            "agents": agents,
            "runs": runs,
        }

    def test_repeated_entities_across_hosts_have_unique_ids(self):
        index = quilt.index_by_id(quilt.build_tree(self._stress()))
        # Uniqueness is enforced by index_by_id (it raises on any duplicate).
        self.assertEqual(len(index), len(set(index)))
        self.assertIn("source:h0:claude", index)
        self.assertIn("source:h2:hermes", index)
        self.assertIn("agent:h1:codex:w3", index)

    def test_all_bounds_are_finite(self):
        index = quilt.index_by_id(quilt.build_tree(self._stress()))
        for node_id, node in index.items():
            for v in node["bounds"]:
                self.assertTrue(math.isfinite(v), node_id)

    def test_every_child_is_inside_its_parent(self):
        index = quilt.index_by_id(quilt.build_tree(self._stress()))
        for node in index.values():
            if node["bounds"][2] <= 0:
                continue
            for child in node["children"]:
                self.assertTrue(inside(child["bounds"], node["bounds"]), (node["id"], child["id"]))

    def test_siblings_do_not_overlap(self):
        index = quilt.index_by_id(quilt.build_tree(self._stress()))
        for node in index.values():
            kids = node["children"]
            for i, a in enumerate(kids):
                for b in kids[i + 1:]:
                    self.assertFalse(intersects(a["bounds"], b["bounds"]), (a["id"], b["id"]))

    def test_three_runs_under_one_agent_are_three_distinct_leaves(self):
        s = snap(
            sources=["claude"],
            agents=[{"worker_id": "w", "sources": ["claude"], "freshness": None}],
            runs=[{"task_id": "t", "run_id": i, "worker_id": "w", "sources": ["claude"], "freshness": None} for i in range(3)],
        )
        index = quilt.index_by_id(quilt.build_tree(s))
        agent = index["agent:local:claude:w"]
        self.assertEqual(len(agent["children"]), 3)
        self.assertEqual(len({c["id"] for c in agent["children"]}), 3)
        for c in agent["children"]:
            self.assertEqual(c["children"], [])  # real leaves only


class WireFormTests(unittest.TestCase):
    def test_wire_form_is_json_safe_and_carries_no_event_bodies(self):
        import json
        s = snap(sources=["claude"], agents=[{"worker_id": "w", "sources": ["claude"], "freshness": None}],
                 runs=[{"task_id": "t", "run_id": 1, "worker_id": "w", "sources": ["claude"], "freshness": None,
                        "events": 5, "prompt": "SECRET-PROMPT-TEXT"}])
        wire = quilt.to_wire(quilt.build_tree(s))
        text = json.dumps(wire)  # must serialize
        self.assertNotIn("SECRET-PROMPT-TEXT", text)
        self.assertEqual(set(wire), {"id", "kind", "label", "state", "bounds", "children"})
        for node in quilt.index_by_id(quilt.build_tree(s)).values():
            self.assertTrue(all(isinstance(v, float) for v in node["bounds"]))


if __name__ == "__main__":
    unittest.main()
