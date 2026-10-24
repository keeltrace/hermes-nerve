"""Focused checks for the zoom quilt slice: snapshot shape, JS hooks, and a JS syntax check.

No credentials, no network, no browser. The JS syntax check runs only if node exists.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_nerve.observatory import camera as cam_mod
from hermes_nerve.observatory import collector, ui

FORWARD = [
    ({"s": 2.0, "tx": 10.0, "ty": -5.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 100.0, "oy": 50.0, "units": 4.0}),
    ({"s": 1.5, "tx": 3.0, "ty": 7.0}, {"ox": 100.0, "oy": 50.0, "units": 4.0}, {"ox": 3.0, "oy": -2.0, "units": 0.5}),
    ({"s": 0.25, "tx": -12.0, "ty": 8.0}, {"ox": 3.0, "oy": -2.0, "units": 0.5}, {"ox": -7.5, "oy": 12.0, "units": 2.0}),
]
EDGE_ACCEPT = [
    ({"s": 0.1, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 0.5}),  # -> 0.05
    ({"s": 32.0, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 2.0}),  # -> 64
]
REFUSE_RANGE = [
    ({"s": 0.06, "tx": 1.0, "ty": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 0.5}),  # 0.03
    ({"s": 40.0, "tx": 1.0, "ty": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 2.0}),  # 80
]
INVALID = [
    ({"s": float("nan"), "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": 1.0, "tx": float("inf"), "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": "1", "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": 1.0, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": 1.0, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": -1.0}),
    ({"s": 1.0, "tx": 0.0, "ty": 0.0}, {"ox": float("nan"), "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": 1.0, "tx": 0.0, "ty": 0.0}, None, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    (None, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
]


def _frame(f):
    return cam_mod.Frame(f["ox"], f["oy"], f["units"])


def _cam(c):
    return cam_mod.Camera(scale=c["s"], tx=c["tx"], ty=c["ty"])


def _triples(rows):
    return [{"cam": c, "parent": p, "child": ch} for c, p, ch in rows]


class EnterFrameParityTests(unittest.TestCase):
    """Runs the embedded JS enterFrame in Node and checks it against camera.enter_frame."""

    def _run_node(self):
        js = ui.APP_JS
        mn = js.index("  var MIN_S")
        fn = js.index("  function enterFrame(")
        end = js.index("\n  }\n", fn) + len("\n  }\n")
        body = js[mn:js.index("\n", mn) + 1] + js[fn:end]
        body += "const groups = " + json.dumps({"forward": _triples(FORWARD), "edge": _triples(EDGE_ACCEPT),
                                               "refuse": _triples(REFUSE_RANGE), "invalid": _triples(INVALID)}) + ";\n"
        body += "const out = {};\n"
        body += "for (const k of Object.keys(groups)) out[k] = groups[k].map(c => enterFrame(c.cam, c.parent, c.child));\n"
        body += "out.back = out.forward.map((r, i) => enterFrame(r, groups.forward[i].child, groups.forward[i].parent));\n"
        body += "const cam = {s: 0.06, tx: 1, ty: 2}; const before = JSON.stringify(cam);\n"
        body += "out.failed_null = enterFrame(cam, {ox: 0, oy: 0, units: 1}, {ox: 0, oy: 0, units: 0.5}) === null;\n"
        body += "out.unchanged = JSON.stringify(cam) === before;\n"
        body += "console.log(JSON.stringify(out));\n"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "enter_frame.js"
            path.write_text(body, encoding="utf-8")
            proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_three_frames_match_python_enter_frame(self):
        out = self._run_node()
        self.assertEqual(len(out["forward"]), 3)
        for got, (c, p, ch) in zip(out["forward"], FORWARD):
            exp = cam_mod.enter_frame(_cam(c), _frame(p), _frame(ch))
            self.assertAlmostEqual(got["s"], exp.scale, places=12)
            self.assertAlmostEqual(got["tx"], exp.tx, places=12)
            self.assertAlmostEqual(got["ty"], exp.ty, places=12)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_reverse_round_trip_restores_original_camera(self):
        out = self._run_node()
        for back, (c, _, _) in zip(out["back"], FORWARD):
            self.assertAlmostEqual(back["s"], c["s"], places=12)
            self.assertAlmostEqual(back["tx"], c["tx"], places=12)
            self.assertAlmostEqual(back["ty"], c["ty"], places=12)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_scale_bounds_are_inclusive_and_out_of_range_is_refused_not_clamped(self):
        out = self._run_node()
        for got, (c, p, ch) in zip(out["edge"], EDGE_ACCEPT):
            self.assertIsNotNone(got)
            self.assertAlmostEqual(got["s"], cam_mod.enter_frame(_cam(c), _frame(p), _frame(ch)).scale, places=12)
        self.assertEqual(out["edge"][0]["s"], 0.05)
        self.assertEqual(out["edge"][1]["s"], 64.0)
        for (c, p, ch) in REFUSE_RANGE:
            with self.assertRaises(ValueError):
                cam_mod.enter_frame(_cam(c), _frame(p), _frame(ch))
        self.assertEqual(out["refuse"], [None, None])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_strict_validation_refuses_non_finite_non_numeric_and_non_positive_units(self):
        out = self._run_node()
        self.assertEqual(out["invalid"], [None] * len(INVALID))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_failed_conversion_leaves_caller_state_unchanged(self):
        out = self._run_node()
        self.assertTrue(out["failed_null"])
        self.assertTrue(out["unchanged"])


def _empty_snapshot(tmp: Path) -> dict:
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    return collector.build_snapshot(
        store_path=tmp / "missing.sqlite3",
        shadow_path=tmp / "missing-shadow.jsonl",
        now=now,
    )


class SnapshotQuiltShapeTests(unittest.TestCase):
    def test_snapshot_carries_quilt_root_with_fleet_and_host(self):
        with tempfile.TemporaryDirectory() as d:
            snap = _empty_snapshot(Path(d))
        self.assertIn("quilt", snap)
        root = snap["quilt"]
        self.assertEqual(root["id"], "fleet")
        self.assertEqual(root["kind"], "fleet")
        self.assertEqual(len(root["children"]), 1)
        self.assertEqual(root["children"][0]["kind"], "host")
        self.assertEqual(root["children"][0]["children"], [])  # no invented sources/agents/runs

    def test_quilt_is_json_safe_with_finite_bounds_and_stable_keys(self):
        with tempfile.TemporaryDirectory() as d:
            snap = _empty_snapshot(Path(d))
        text = json.dumps(snap["quilt"])
        self.assertIn("bounds", text)
        self.assertEqual(set(snap["quilt"]), {"id", "kind", "label", "state", "bounds", "children"})
        for b in snap["quilt"]["bounds"]:
            self.assertIsInstance(b, float)


class UiHookTests(unittest.TestCase):
    """The embedded JS and HTML expose the required hooks. These are source checks, not behaviour."""

    def test_html_has_mode_toggle_and_quilt_scripts(self):
        self.assertIn('id="mode"', ui.INDEX_HTML)
        self.assertIn('src="/app.js"', ui.INDEX_HTML)

    def test_js_has_one_transform_and_cursor_anchored_wheel(self):
        js = ui.APP_JS
        self.assertIn('addEventListener("wheel"', js)
        self.assertIn("zoomAt(quilt.cam", js)           # cursor-anchored zoom path
        self.assertIn('"translate(" + c.tx', js)         # one affine transform applied to one layer
        self.assertIn("scale(\" + c.s", js)

    def test_js_has_pan_keyboard_and_reset_hooks(self):
        js = ui.APP_JS
        self.assertIn('addEventListener("pointermove"', js)
        for key in ('"Escape"', '"Home"', '"ArrowLeft"', '"+"', '"-"'):
            self.assertIn(key, js)

    def test_js_has_accessible_list_and_reduced_motion_css(self):
        self.assertIn('"aria-label": "Quilt entities', ui.APP_JS)
        self.assertIn("prefers-reduced-motion", ui.APP_CSS)
        self.assertIn("max-width:600px", ui.APP_CSS)

    def test_js_contains_no_unsafe_sinks(self):
        js = ui.APP_JS
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertNotIn("document.write", js)
        self.assertNotIn("eval(", js)

    def test_js_does_not_claim_live_when_collector_state_is_unknown(self):
        self.assertNotIn('setMaster("LIVE", "ok");', ui.APP_JS)
        self.assertIn('"COLLECTED " + new Date()', ui.APP_JS)
        self.assertIn('setMaster("COLLECTOR CONNECTED", "off");', ui.APP_JS)

    def test_js_has_no_external_asset_urls(self):
        js = ui.APP_JS.replace("http://www.w3.org/2000/svg", "")
        self.assertNotIn("http://", js)
        self.assertNotIn("https://", js)


class JsSyntaxTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_embedded_app_js_passes_node_check(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "app.js"
            path.write_text(ui.APP_JS, encoding="utf-8")
            proc = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class WheelNormalizationTests(unittest.TestCase):
    """Executes the real embedded wheelPixels helper in Node (no Python mirror)."""

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_wheel_pixels_executes_in_node(self):
        import re
        m = re.search(r"^  function wheelPixels\(.*\}\s*$", ui.APP_JS, re.M)
        self.assertIsNotNone(m, "wheelPixels helper not found in APP_JS")
        cases = [
            ([10, 0, 16, 600], 10),
            ([2, 1, 20, 600], 40),
            ([1, 2, 20, 600], 200),
            ([-1, 2, 20, 600], -200),
            ([float("nan"), 0, 16, 600], 0),
            ([2, 1, float("nan"), 600], 32),
            ([5, 5, 20, 600], 0),
        ]
        script = m.group(0) + "\nconst cases = " + json.dumps(
            [[None if (isinstance(x, float) and x != x) else x for x in args] for args, _ in cases]
        ) + ";\nconsole.log(JSON.stringify(cases.map(function (a) { return wheelPixels(a[0] === null ? NaN : a[0], a[1], a[2] === null ? NaN : a[2], a[3] === null ? NaN : a[3]); })));"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "wheel.js"
            path.write_text(script, encoding="utf-8")
            proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [want for _, want in cases])


class QuiltIdentityTests(unittest.TestCase):
    """Executes the real embedded quiltIdentity/shouldFitQuilt helpers in Node."""

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_quilt_identity_helpers_execute_in_node(self):
        import re
        helpers = [re.search(r"^  function %s\(.*\}\s*$" % name, ui.APP_JS, re.M) for name in ("quiltIdentity", "shouldFitQuilt")]
        self.assertTrue(all(helpers), "identity helpers not found in APP_JS")
        cases = {
            "same_id_bounds_changed_no_refit": "shouldFitQuilt({s:1,tx:0,ty:0}, quiltIdentity({id:'fleet',kind:'fleet',bounds:[0,0,10,10]}), {id:'fleet',kind:'fleet',bounds:[0,0,99,99]})",
            "new_id_refits": "shouldFitQuilt({s:1,tx:0,ty:0}, quiltIdentity({id:'fleet',kind:'fleet'}), {id:'other',kind:'fleet'})",
            "new_kind_refits": "shouldFitQuilt({s:1,tx:0,ty:0}, quiltIdentity({id:'fleet',kind:'fleet'}), {id:'fleet',kind:'host'})",
            "no_camera_fits": "shouldFitQuilt(null, quiltIdentity({id:'fleet',kind:'fleet'}), {id:'fleet',kind:'fleet'})",
            "absent_root_fits": "shouldFitQuilt({s:1,tx:0,ty:0}, quiltIdentity({id:'fleet',kind:'fleet'}), undefined)",
            "empty_id_fits": "shouldFitQuilt({s:1,tx:0,ty:0}, quiltIdentity({id:'',kind:'fleet'}), {id:'',kind:'fleet'})",
            "non_string_id_fits": "shouldFitQuilt({s:1,tx:0,ty:0}, quiltIdentity({id:7,kind:'fleet'}), {id:7,kind:'fleet'})",
            "punct_ids_distinct_refit": "shouldFitQuilt({s:1,tx:0,ty:0}, quiltIdentity({id:'a,b',kind:'k'}), {id:'a',kind:'b,k'})",
            "punct_ids_same_no_refit": "shouldFitQuilt({s:1,tx:0,ty:0}, quiltIdentity({id:'a,b',kind:'k'}), {id:'a,b',kind:'k'})",
        }
        expected = {
            "same_id_bounds_changed_no_refit": False,
            "new_id_refits": True,
            "new_kind_refits": True,
            "no_camera_fits": True,
            "absent_root_fits": True,
            "empty_id_fits": True,
            "non_string_id_fits": True,
            "punct_ids_distinct_refit": True,
            "punct_ids_same_no_refit": False,
        }
        body = "\n".join(h.group(0) for h in helpers) + "\n"
        body += "const out = {};\n"
        for name, expr in cases.items():
            body += "out[%s] = %s;\n" % (json.dumps(name), expr)
        body += "console.log(JSON.stringify(out));\n"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "identity.js"
            path.write_text(body, encoding="utf-8")
            proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), expected)

    def test_render_path_uses_identity_helpers_not_bounds(self):
        start = ui.APP_JS.index("function renderQuilt(")
        end = ui.APP_JS.index("\n  }", start)
        render = ui.APP_JS[start:end]
        self.assertIn("quiltIdentity(q)", render)
        self.assertIn("shouldFitQuilt(quilt.cam, quilt.key, q)", render)
        self.assertNotIn("bounds.join", render)


class LocalBoundsTests(unittest.TestCase):
    """Executes the real embedded localBounds helper in Node (no Python mirror)."""

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_local_bounds_executes_in_node(self):
        start = ui.APP_JS.index("  function localBounds(")
        end = ui.APP_JS.index("\n  }\n", start) + len("\n  }\n")
        helper = ui.APP_JS[start:end]
        body = helper + "\n"
        body += "const src = [100, 200, 50, 40];\n"
        body += "const before = JSON.stringify(src);\n"
        body += "const out = {};\n"
        body += "out.offset_scaled = localBounds(src, {ox: 10, oy: 20, units: 2});\n"
        body += "out.root_frame = localBounds([1, 2, 3, 4], {ox: 0, oy: 0, units: 1});\n"
        body += "out.bad_units_zero = localBounds(src, {ox: 0, oy: 0, units: 0});\n"
        body += "out.bad_frame_nan_ox = localBounds(src, {ox: NaN, oy: 0, units: 1});\n"
        body += "out.bad_frame_missing = localBounds(src, undefined);\n"
        body += "out.bad_zero_width = localBounds([0, 0, 0, 5], {ox: 0, oy: 0, units: 1});\n"
        body += "out.bad_nan_x = localBounds([NaN, 0, 5, 5], {ox: 0, oy: 0, units: 1});\n"
        body += "out.no_mutation = JSON.stringify(src) === before;\n"
        body += "console.log(JSON.stringify(out));\n"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "local_bounds.js"
            path.write_text(body, encoding="utf-8")
            proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {
            "offset_scaled": [45, 90, 25, 20],
            "root_frame": [1, 2, 3, 4],
            "bad_units_zero": None,
            "bad_frame_nan_ox": None,
            "bad_frame_missing": None,
            "bad_zero_width": None,
            "bad_nan_x": None,
            "no_mutation": True,
        })

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_qnodeel_invalid_frame_fails_gracefully_in_node(self):
        import re
        names = ("localBounds", "qNodeEl")
        helpers = [re.search(r"^  function %s\(.*?\n  \}\n" % name, ui.APP_JS, re.M | re.S) for name in names]
        self.assertTrue(all(helpers), "localBounds/qNodeEl not found in APP_JS")
        stubs = ("function el(tag, attrs, children) { return { style: {}, attrs: attrs }; }\n"
                 "function dash(v) { return v === null || v === undefined || v === '' ? 'UNRECORDED' : String(v); }\n")
        body = stubs + "\n".join(h.group(0) for h in helpers) + "\n"
        body += "const src = { id: 'n1', kind: 'host', state: 'ok', label: 'h', bounds: [100, 200, 50, 40] };\n"
        body += "const before = JSON.stringify(src);\n"
        body += "const out = {};\n"
        body += "const ok = qNodeEl(src, { ox: 10, oy: 20, units: 2 });\n"
        body += "out.valid_left = ok.style.left; out.valid_width = ok.style.width;\n"
        body += "out.root_default_left = qNodeEl(src).style.left;\n"
        body += "out.bad_units_null = qNodeEl(src, { ox: 0, oy: 0, units: 0 });\n"
        body += "out.bad_nan_null = qNodeEl(src, { ox: NaN, oy: 0, units: 1 });\n"
        body += "out.no_mutation = JSON.stringify(src) === before;\n"
        body += "console.log(JSON.stringify(out));\n"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "qnode_frame.js"
            path.write_text(body, encoding="utf-8")
            proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {
            "valid_left": "45px",
            "valid_width": "25px",
            "root_default_left": "100px",
            "bad_units_null": None,
            "bad_nan_null": None,
            "no_mutation": True,
        })


if __name__ == "__main__":
    unittest.main()
