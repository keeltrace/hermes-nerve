"""Python<->JS camera parity plus a production-reachability guard.

The observatory's camera geometry exists twice: ``camera`` holds the tested
Python implementation, and ``assets/app.js`` (served through ``ui.APP_JS``)
holds the one the operator sees, with a "mirrors camera.py" comment as the only
binding. A one-sided edit used to diverge silently with no failing test.

Enforced here: (1) parity by execution -- the JS helpers are sliced out of the
served asset and run in node against the Python functions over one shared input
table, refusal included; (2) reachability -- camera and quilt must stay
reachable from the shipped CLI entry point, or their tests prove nothing.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hermes_nerve.observatory import camera as cam_mod
from hermes_nerve.observatory import collector, ui

HAS_NODE = shutil.which("node") is not None
NAN, INF = float("nan"), float("inf")

FORWARD = [
    ({"s": 2.0, "tx": 10.0, "ty": -5.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 100.0, "oy": 50.0, "units": 4.0}),
    ({"s": 1.5, "tx": 3.0, "ty": 7.0}, {"ox": 100.0, "oy": 50.0, "units": 4.0}, {"ox": 3.0, "oy": -2.0, "units": 0.5}),
    ({"s": 0.25, "tx": -12.0, "ty": 8.0}, {"ox": 3.0, "oy": -2.0, "units": 0.5}, {"ox": -7.5, "oy": 12.0, "units": 2.0}),
    ({"s": 1.0, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
]
EDGE_ACCEPT = [
    ({"s": 0.1, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 0.5}),
    ({"s": 32.0, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 2.0}),
]
REFUSE_RANGE = [
    ({"s": 0.06, "tx": 1.0, "ty": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 0.5}),
    ({"s": 40.0, "tx": 1.0, "ty": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 2.0}),
]
INVALID = [
    ({"s": NAN, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": 1.0, "tx": INF, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": 1.0, "tx": 0.0, "ty": -INF}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": "1", "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": 1.0, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": 1.0, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": -1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": 1.0, "tx": 0.0, "ty": 0.0}, {"ox": NAN, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": 1.0, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 0.0}),
    ({"s": 1.0, "tx": 0.0, "ty": 0.0}, None, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ({"s": 1.0, "tx": 0.0, "ty": 0.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}, None),
    (None, {"ox": 0.0, "oy": 0.0, "units": 1.0}, {"ox": 0.0, "oy": 0.0, "units": 1.0}),
]
BOUNDS_OK = [
    ([100.0, 200.0, 50.0, 40.0], {"ox": 10.0, "oy": 20.0, "units": 2.0}),
    ([1.0, 2.0, 3.0, 4.0], {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ([-40.0, -90.0, 12.5, 0.25], {"ox": -40.0, "oy": -90.0, "units": 0.5}),
]
BOUNDS_REFUSE = [
    ([0.0, 0.0, 0.0, 5.0], {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ([0.0, 0.0, 5.0, 0.0], {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ([-1.0, 0.0, 5.0, 0.0], {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ([100.0, 200.0, 50.0, 40.0], {"ox": 0.0, "oy": 0.0, "units": 0.0}),
    ([100.0, 200.0, 50.0, 40.0], {"ox": 0.0, "oy": 0.0, "units": -2.0}),
    ([100.0, 200.0, 50.0, 40.0], {"ox": NAN, "oy": 0.0, "units": 1.0}),
    ([100.0, 200.0, 50.0, 40.0], {"ox": 0.0, "oy": 0.0, "units": INF}),
    ([100.0, 200.0, 50.0, 40.0], None),
    ([100.0, 200.0, 50.0], {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ([NAN, 0.0, 5.0, 5.0], {"ox": 0.0, "oy": 0.0, "units": 1.0}),
    ([100.0, 200.0, 50.0, 40.0], {"ox": 0.0, "oy": 0.0, "units": 1e-320}),
]


def _frame(f):
    """A camera.Frame, or a stand-in that camera.local_bounds itself must reject.

    Rows whose frame is illegal (units <= 0, non-finite origin) are passed as a
    plain object: constructing a real Frame would raise here and hide the fact
    that ``local_bounds`` is the function under test. Its contract is to return
    None for these, never to raise.
    """
    if f is None:
        return None
    if f["units"] <= 0 or f["ox"] != f["ox"] or f["oy"] != f["oy"] or f["units"] in (INF, -INF):
        return _RawFrame(f["ox"], f["oy"], f["units"])
    return cam_mod.Frame(f["ox"], f["oy"], f["units"])


class _RawFrame:
    """Not a camera.Frame on purpose: the invalid rows must reach local_bounds."""

    def __init__(self, ox, oy, units):
        self.origin_x, self.origin_y, self.units = ox, oy, units


def _cam(c):
    return None if c is None else cam_mod.Camera(scale=c["s"], tx=c["tx"], ty=c["ty"])


def _triples(rows):
    return [{"cam": c, "parent": p, "child": ch} for c, p, ch in rows]


def _dump(value):
    """JSON with non-finite floats emitted as JS literals, never as null."""
    if isinstance(value, dict):
        return "{" + ",".join("%s:%s" % (json.dumps(k), _dump(v)) for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_dump(v) for v in value) + "]"
    if isinstance(value, float):
        if value != value:
            return "NaN"
        if value == INF:
            return "Infinity"
        if value == -INF:
            return "-Infinity"
    return json.dumps(value)


def _slice_helper(js, name):
    """Cut one two-space-indented `function name(...) {...}` block out of the asset."""
    start = js.index("  function %s(" % name)
    return js[start:js.index("\n  }\n", start) + len("\n  }\n")]


def _run_node(script):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "parity.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise AssertionError("node failed: " + proc.stderr)
    return json.loads(proc.stdout)


def _parity_script():
    """Node program that runs the *served* JS helpers, not a Python transcription."""
    js = ui.APP_JS
    marker = "  var MIN_S"
    script = js[js.index(marker):js.index("\n", js.index(marker)) + 1]
    script += _slice_helper(js, "enterFrame") + "\n" + _slice_helper(js, "localBounds") + "\n"
    script += "const groups = " + _dump({
        "forward": _triples(FORWARD), "edge": _triples(EDGE_ACCEPT),
        "refuse": _triples(REFUSE_RANGE), "invalid": _triples(INVALID),
    }) + ";\n"
    script += "const boundsOk = " + _dump([{"b": b, "f": f} for b, f in BOUNDS_OK]) + ";\n"
    script += "const boundsRefuse = " + _dump([{"b": b, "f": f} for b, f in BOUNDS_REFUSE]) + ";\n"
    script += "const out = {};\n"
    script += "for (const k of Object.keys(groups)) out[k] = groups[k].map(c => enterFrame(c.cam, c.parent, c.child));\n"
    script += "out.bounds_ok = boundsOk.map(r => localBounds(r.b, r.f));\n"
    script += "out.bounds_refuse = boundsRefuse.map(r => localBounds(r.b, r.f));\n"
    script += "const cam = {s: 0.06, tx: 1, ty: 2}; const before = JSON.stringify(cam);\n"
    script += "out.failed_null = enterFrame(cam, {ox: 0, oy: 0, units: 1}, {ox: 0, oy: 0, units: 0.5}) === null;\n"
    script += "out.unchanged = JSON.stringify(cam) === before;\n"
    script += "console.log(JSON.stringify(out));\n"
    return script


@unittest.skipUnless(HAS_NODE, "node not installed")
class EnterFrameParityTests(unittest.TestCase):
    """The served JS enterFrame and camera.enter_frame must agree, refusals included."""

    @classmethod
    def setUpClass(cls):
        cls.out = _run_node(_parity_script())

    def _eq(self, got, exp):
        self.assertAlmostEqual(got["s"], exp.scale, places=12)
        self.assertAlmostEqual(got["tx"], exp.tx, places=12)
        self.assertAlmostEqual(got["ty"], exp.ty, places=12)

    def test_forward_transitions_match_python(self):
        for got, (c, p, ch) in zip(self.out["forward"], FORWARD):
            self.assertIsNotNone(got, "JS refused a transition Python accepts")
            self._eq(got, cam_mod.enter_frame(_cam(c), _frame(p), _frame(ch)))

    def test_reverse_round_trip_restores_the_original_camera(self):
        # The inverse of parent->child is child->parent applied to the result.
        for got, (c, p, ch) in zip(self.out["forward"], FORWARD):
            if got is None:
                continue
            back = cam_mod.leave_frame(
                cam_mod.Camera(got["s"], got["tx"], got["ty"]),
                _frame(ch), _frame(p),
            )
            self.assertAlmostEqual(back.scale, c["s"], places=12)
            self.assertAlmostEqual(back.tx, c["tx"], places=12)
            self.assertAlmostEqual(back.ty, c["ty"], places=12)

    def test_scale_range_is_inclusive_on_both_sides(self):
        for got, (c, p, ch) in zip(self.out["edge"], EDGE_ACCEPT):
            self.assertIsNotNone(got, "inclusive range edge was refused by JS")
            self._eq(got, cam_mod.enter_frame(_cam(c), _frame(p), _frame(ch)))
        self.assertEqual(self.out["edge"][0]["s"], 0.05)
        self.assertEqual(self.out["edge"][1]["s"], 64.0)

    def test_out_of_range_is_refused_by_both_not_clamped_by_either(self):
        for c, p, ch in REFUSE_RANGE:
            with self.assertRaises(ValueError):
                cam_mod.enter_frame(_cam(c), _frame(p), _frame(ch))
        self.assertEqual(self.out["refuse"], [None] * len(REFUSE_RANGE))
        for got in self.out["refuse"]:
            self.assertIsNone(got, "JS clamped where Python refused")

    def test_invalid_input_is_refused_by_js_and_raises_in_python(self):
        self.assertEqual(len(self.out["invalid"]), len(INVALID))
        for got, (c, p, ch) in zip(self.out["invalid"], INVALID):
            try:
                cam_mod.enter_frame(_cam(c), _frame(p), _frame(ch))
            except (ValueError, TypeError):
                pass
            else:
                self.fail("Python accepted input the contract refuses: %r" % ((c, p, ch),))
            self.assertIsNone(got, "JS accepted input Python refuses: %r" % ((c, p, ch),))

    def test_refusal_is_atomic_in_js(self):
        self.assertTrue(self.out["failed_null"])
        self.assertTrue(self.out["unchanged"])


@unittest.skipUnless(HAS_NODE, "node not installed")
class LocalBoundsParityTests(unittest.TestCase):
    """The served JS localBounds and camera.local_bounds must agree exactly."""

    @classmethod
    def setUpClass(cls):
        cls.out = _run_node(_parity_script())

    def test_valid_conversions_match_python(self):
        self.assertEqual(len(self.out["bounds_ok"]), len(BOUNDS_OK))
        for got, (b, f) in zip(self.out["bounds_ok"], BOUNDS_OK):
            exp = cam_mod.local_bounds(list(b), _frame(f))
            self.assertIsNotNone(got, "JS refused bounds Python converts")
            self.assertIsNotNone(exp)
            for i, (g, e) in enumerate(zip(got, exp)):
                self.assertAlmostEqual(g, e, places=12, msg="component %d" % i)

    def test_refusals_agree_exactly(self):
        self.assertEqual(len(self.out["bounds_refuse"]), len(BOUNDS_REFUSE))
        for got, (b, f) in zip(self.out["bounds_refuse"], BOUNDS_REFUSE):
            self.assertIsNone(cam_mod.local_bounds(list(b), _frame(f)),
                              "Python accepted a case the contract refuses: %r" % ((b, f),))
            self.assertIsNone(got, "JS accepted a case Python refuses: %r" % ((b, f),))

    def test_python_side_never_raises_for_the_refusal_table(self):
        for b, f in BOUNDS_OK + BOUNDS_REFUSE:
            cam_mod.local_bounds(list(b), _frame(f))  # returns None, never raises


class PythonRefusalContractTests(unittest.TestCase):
    """The Python half of the contract, asserted without a JS engine."""

    def test_transition_frame_refuses_non_finite_and_out_of_range_without_clamping(self):
        for c, p, ch in REFUSE_RANGE + INVALID:
            with self.assertRaises(ValueError):
                cam_mod.enter_frame(_cam(c), _frame(p), _frame(ch))

    def test_camera_is_frozen_so_a_refused_transition_cannot_mutate_it(self):
        cam = _cam(FORWARD[0][0])
        with self.assertRaises(Exception):
            cam.scale = 99.0

    def test_enter_and_leave_frame_are_inverses(self):
        cam = _cam(FORWARD[0][0])
        parent, child = _frame(FORWARD[0][1]), _frame(FORWARD[0][2])
        back = cam_mod.leave_frame(cam_mod.enter_frame(cam, parent, child), child, parent)
        self.assertAlmostEqual(back.scale, cam.scale, places=12)
        self.assertAlmostEqual(back.tx, cam.tx, places=12)
        self.assertAlmostEqual(back.ty, cam.ty, places=12)


PACKAGE_ROOT = Path(cam_mod.__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
ENTRY_MODULE = "hermes_nerve.observatory.__main__"
REQUIRED = ("camera", "quilt")


def _module_name(path):
    try:
        rel = path.relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


def _imports_of(path):
    """Absolute in-package module names a module imports.

    ``from . import x`` inside ``a.b.c`` means ``a.b.x``; ``from .c import y``
    means ``a.b.c.y``; ``from ..c import y`` means ``a.c.y``. The level counts
    packages to climb from the importing module's package, so the base is the
    module's own parts with ``level`` entries dropped.
    """
    name = _module_name(path) or ""
    parts = name.split(".")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("hermes_nerve"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = ".".join(parts[: max(0, len(parts) - node.level)])
                if node.module:
                    found.add(base + "." + node.module)
                else:  # `from . import a, b`: each name is a submodule of base.
                    found.update(base + "." + a.name for a in node.names)
            elif node.module and node.module.startswith("hermes_nerve"):
                found.add(node.module)
    return found


def _production_modules():
    return [(n, p) for p in sorted(PROJECT_ROOT.joinpath("hermes_nerve").rglob("*.py"))
            for n in [_module_name(p)] if n]


def _reachable_from_entry():
    by_name = dict(_production_modules())
    seen, stack = set(), [ENTRY_MODULE]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        path = by_name.get(name)
        if path is None:
            continue
        for target in _imports_of(path):
            if target in by_name:
                stack.append(target)
            else:  # `from . import a, b` yields the package: expand to children.
                stack.extend(c for c in by_name if c.startswith(target + "."))
    return seen


class ProductionReachabilityTests(unittest.TestCase):
    """camera and quilt must stay reachable from the shipped entry point."""

    def test_camera_and_quilt_are_reachable_from_production_code(self):
        reachable = _reachable_from_entry()
        self.assertIn(ENTRY_MODULE, reachable, "entry module not found: " + ENTRY_MODULE)
        missing = sorted(m for m in ("hermes_nerve.observatory." + r for r in REQUIRED)
                         if m not in reachable)
        self.assertEqual(
            missing, [],
            "no production importer reaches %s: the observatory's tested geometry is "
            "unreachable from python -m hermes_nerve.observatory, so the shipped server "
            "makes no camera decision and these modules' tests prove nothing about what "
            "the operator sees. Wire them into the serving path "
            "(server.py -> collector.py -> quilt -> camera)." % ", ".join(missing),
        )

    def test_entry_module_imports_without_error(self):
        # A static graph can be satisfiable while the real import fails (circular
        # import, missing dependency). This executes the real thing.
        proc = subprocess.run(
            [sys.executable, "-c", "import importlib; importlib.import_module(%r)" % ENTRY_MODULE],
            capture_output=True, text=True, timeout=30, cwd=str(PROJECT_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_snapshot_carries_python_built_quilt(self):
        with tempfile.TemporaryDirectory() as d:
            snap = collector.build_snapshot(
                store_path=Path(d) / "missing.sqlite3",
                shadow_path=Path(d) / "missing.jsonl",
            )
        root = snap["quilt"]
        self.assertEqual(root["id"], "fleet")
        self.assertEqual(set(root), {"id", "kind", "label", "state", "bounds", "children"})
        self.assertIsInstance(root["bounds"][0], float)


class OversizedShadowPathTests(unittest.TestCase):
    """The no-quota / oversized shadow path must still report, not crash."""

    def test_oversized_shadow_is_unavailable_with_a_fixed_reason(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            shadow = Path(d) / "shadow.jsonl"
            shadow.write_bytes(b"x")
            os.truncate(shadow, collector.SHADOW_MAX_BYTES + 1)
            snap = collector.build_snapshot(
                store_path=Path(d) / "missing.sqlite3", shadow_path=shadow
            )
            self.assertEqual(snap["shadow"]["status"], "unavailable")
            self.assertEqual(snap["shadow"]["error"], "ShadowTooLarge")
            self.assertEqual(snap["quilt"]["id"], "fleet")


if __name__ == "__main__":
    unittest.main()
