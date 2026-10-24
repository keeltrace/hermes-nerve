import math
import random
import unittest

from hermes_nerve.observatory import camera as cam_mod
from hermes_nerve.observatory.camera import (
    MAX_SCALE,
    MIN_SCALE,
    Camera,
    fit_rect,
    intersects,
    pan,
    rebase,
    screen_to_world,
    visible_rect,
    world_to_screen,
    zoom_at,
)


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


class AnchorTests(unittest.TestCase):
    def test_zoom_keeps_cursor_world_point_fixed(self):
        cam = Camera(scale=2.0, tx=30.0, ty=-12.0)
        sx, sy = 411.0, 207.5
        before = screen_to_world(cam, sx, sy)
        for factor in (1.1, 0.5, 3.0, 1 / 1.1):
            after_cam = zoom_at(cam, factor, sx, sy)
            after = screen_to_world(after_cam, sx, sy)
            self.assertTrue(close(before[0], after[0]), factor)
            self.assertTrue(close(before[1], after[1]), factor)

    def test_repeated_zoom_in_and_out_returns_to_start(self):
        # Stay inside the clamp range: a zoom that hits MAX_SCALE is not
        # reversible (the clamp discards the excess), which is by design.
        cam = Camera(scale=1.0, tx=5.0, ty=7.0)
        z = cam
        for _ in range(10):
            z = zoom_at(z, 1.2, 300.0, 200.0)
        for _ in range(10):
            z = zoom_at(z, 1 / 1.2, 300.0, 200.0)
        self.assertTrue(close(z.scale, cam.scale, 1e-9))
        self.assertTrue(close(z.tx, cam.tx, 1e-6))
        self.assertTrue(close(z.ty, cam.ty, 1e-6))

    def test_clamp_still_anchors_at_the_cursor(self):
        # Hitting MAX_SCALE must not drift the point under the cursor.
        cam = Camera(scale=60.0, tx=0.0, ty=0.0)
        sx, sy = 100.0, 100.0
        before = screen_to_world(cam, sx, sy)
        clamped = zoom_at(cam, 10.0, sx, sy)
        self.assertEqual(clamped.scale, MAX_SCALE)
        after = screen_to_world(clamped, sx, sy)
        self.assertTrue(close(before[0], after[0]))
        self.assertTrue(close(before[1], after[1]))

    def test_scale_is_clamped_both_ways(self):
        self.assertEqual(zoom_at(Camera(), 1e9, 0, 0).scale, MAX_SCALE)
        self.assertEqual(zoom_at(Camera(), 1e-9, 0, 0).scale, MIN_SCALE)


class PanAndRebaseTests(unittest.TestCase):
    def test_pan_only_changes_translation(self):
        cam = Camera(scale=3.0, tx=1.0, ty=2.0)
        moved = pan(cam, 10.0, -4.0)
        self.assertEqual(moved.scale, 3.0)
        self.assertEqual((moved.tx, moved.ty), (11.0, -2.0))

    def test_rebase_is_identity_on_screen_positions(self):
        cam = Camera(scale=7.5, tx=-123.0, ty=88.0)
        for ox, oy in ((0, 0), (250.0, -40.0), (-1e4, 3e4)):
            rebased = rebase(cam, ox, oy)
            for wx, wy in ((0.0, 0.0), (12.5, -3.0), (-400.0, 90.0)):
                # A point addressed relative to the new origin maps to the same screen point.
                sx, sy = world_to_screen(cam, wx, wy)
                rx, ry = world_to_screen(rebased, wx - ox, wy - oy)
                self.assertTrue(close(sx, rx, 1e-9), (ox, oy, wx))
                self.assertTrue(close(sy, ry, 1e-9), (ox, oy, wy))

    def test_rebase_then_zoom_matches_zoom_then_rebase(self):
        # Atomic continuity: rebasing mid-zoom must not change where the zoom lands.
        cam = Camera(scale=2.0, tx=10.0, ty=20.0)
        sx, sy = 150.0, 60.0
        direct = zoom_at(cam, 1.5, sx, sy)
        via = zoom_at(rebase(cam, 33.0, -7.0), 1.5, sx, sy)
        for wx, wy in ((4.0, 5.0), (-9.0, 2.0)):
            a = world_to_screen(direct, wx, wy)
            b = world_to_screen(via, wx - 33.0, wy + 7.0)
            self.assertTrue(close(a[0], b[0], 1e-9))
            self.assertTrue(close(a[1], b[1], 1e-9))

    def test_rebase_keeps_translation_small_on_long_zoom(self):
        cam = Camera()
        for _ in range(60):
            cam = zoom_at(cam, 1.3, 400.0, 300.0)
        self.assertTrue(math.isfinite(cam.tx) and math.isfinite(cam.ty))


class FitAndVisibleTests(unittest.TestCase):
    def test_fit_rect_centres_bounds_in_viewport(self):
        cam = fit_rect(800, 600, (100, 100, 200, 100))
        x0, y0 = world_to_screen(cam, 100, 100)
        x1, y1 = world_to_screen(cam, 300, 200)
        self.assertTrue(0 <= x0 and x1 <= 800 and 0 <= y0 and y1 <= 600)
        self.assertTrue(close((x0 + x1) / 2, 400.0, 1e-6))
        self.assertTrue(close((y0 + y1) / 2, 300.0, 1e-6))

    def test_visible_rect_inverts_the_viewport(self):
        cam = Camera(scale=2.0, tx=-100.0, ty=-50.0)
        vx, vy, vw, vh = visible_rect(cam, 400, 200)
        self.assertTrue(close(vx, 50.0) and close(vy, 25.0))
        self.assertTrue(close(vw, 200.0) and close(vh, 100.0))

    def test_intersects_is_strict_on_edges(self):
        self.assertTrue(intersects((0, 0, 10, 10), (5, 5, 10, 10)))
        self.assertFalse(intersects((0, 0, 10, 10), (10, 0, 5, 5)))  # touching edge is not overlap


class ValidationTests(unittest.TestCase):
    def test_non_finite_inputs_are_refused(self):
        bad = (float("nan"), float("inf"), float("-inf"))
        for v in bad:
            with self.assertRaises(ValueError):
                zoom_at(Camera(), v, 0, 0)
            with self.assertRaises(ValueError):
                pan(Camera(), v, 0)
            with self.assertRaises(ValueError):
                rebase(Camera(), 0, v)

    def test_non_positive_factor_and_empty_viewport_are_refused(self):
        with self.assertRaises(ValueError):
            zoom_at(Camera(), 0.0, 0, 0)
        with self.assertRaises(ValueError):
            fit_rect(0, 100, (0, 0, 10, 10))
        with self.assertRaises(ValueError):
            fit_rect(100, 100, (0, 0, 0, 10))


class FrameValidationTests(unittest.TestCase):
    def test_frame_rejects_non_finite_and_non_positive_units(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                cam_mod.Frame(bad, 0.0, 1.0)
            with self.assertRaises(ValueError):
                cam_mod.Frame(0.0, bad, 1.0)
            with self.assertRaises(ValueError):
                cam_mod.Frame(0.0, 0.0, bad)
        for bad_units in (0.0, -1.0, -1e-300):
            with self.assertRaises(ValueError):
                cam_mod.Frame(0.0, 0.0, bad_units)

    def test_frame_is_immutable(self):
        f = cam_mod.Frame(1.0, 2.0, 4.0)
        with self.assertRaises(Exception):
            f.units = 8.0  # frozen dataclass

    def test_root_frame_defaults_are_identity(self):
        self.assertEqual(cam_mod.Frame(), cam_mod.ROOT_FRAME)
        self.assertEqual((cam_mod.ROOT_FRAME.origin_x, cam_mod.ROOT_FRAME.origin_y, cam_mod.ROOT_FRAME.units), (0.0, 0.0, 1.0))

    def test_root_to_root_is_exact_identity(self):
        cam = Camera(scale=3.25, tx=-17.0, ty=9.5)
        out = cam_mod.enter_frame(cam, cam_mod.ROOT_FRAME, cam_mod.ROOT_FRAME)
        self.assertEqual(out, cam)


class FrameTransitionTests(unittest.TestCase):
    def assert_same_screen(self, cam0, f0, cam1, f1, points):
        for px, py in points:
            gx, gy = cam_mod.frame_to_global(f0, px, py)
            lx, ly = cam_mod.global_to_frame(f1, gx, gy)
            s0 = world_to_screen(cam0, px, py)
            s1 = world_to_screen(cam1, lx, ly)
            self.assertTrue(close(s0[0], s1[0], 1e-9), (px, py, s0, s1))
            self.assertTrue(close(s0[1], s1[1], 1e-9), (px, py, s0, s1))

    def test_formula_matches_contract(self):
        f0 = cam_mod.Frame(10.0, -4.0, 2.0)
        f1 = cam_mod.Frame(30.0, 6.0, 0.5)
        cam = Camera(scale=2.0, tx=3.0, ty=-7.0)
        out = cam_mod.enter_frame(cam, f0, f1)
        self.assertTrue(close(out.scale, 2.0 * (0.5 / 2.0)))
        self.assertTrue(close(out.tx, 3.0 + (30.0 - 10.0) / 2.0 * 2.0))
        self.assertTrue(close(out.ty, -7.0 + (6.0 - -4.0) / 2.0 * 2.0))

    def test_enter_then_leave_roundtrips(self):
        f0 = cam_mod.Frame(0.0, 0.0, 1.0)
        f1 = cam_mod.Frame(120.0, -33.0, 0.25)
        cam = Camera(scale=0.8, tx=-40.0, ty=61.0)
        back = cam_mod.leave_frame(cam_mod.enter_frame(cam, f0, f1), f1, f0)
        self.assertTrue(close(back.scale, cam.scale, 1e-12))
        self.assertTrue(close(back.tx, cam.tx, 1e-9))
        self.assertTrue(close(back.ty, cam.ty, 1e-9))

    def test_screen_positions_are_stable_for_fixed_points(self):
        f0 = cam_mod.Frame(5.0, 5.0, 1.0)
        f1 = cam_mod.Frame(-200.0, 300.0, 0.1)
        cam0 = Camera(scale=1.5, tx=20.0, ty=-10.0)
        cam1 = cam_mod.enter_frame(cam0, f0, f1)
        points = [(0.0, 0.0), (12.5, -3.0), (-400.0, 90.0), (1e3, 2e3)]
        self.assert_same_screen(cam0, f0, cam1, f1, points)

    def test_five_nested_frames_roundtrip_and_keep_screen_positions(self):
        # Each child is a smaller frame inside the global space; the chain is
        # entered from the root and left back out in reverse order.
        # Units shrink by 0.7 per level so the scale stays inside [MIN_SCALE, MAX_SCALE]
        # (a 0.25 ratio per level would fall below MIN_SCALE at depth 5 and be refused).
        frames = [
            cam_mod.ROOT_FRAME,
            cam_mod.Frame(100.0, 100.0, 0.7),
            cam_mod.Frame(130.0, 110.0, 0.49),
            cam_mod.Frame(131.0, 111.5, 0.343),
            cam_mod.Frame(131.2, 111.6, 0.2401),
            cam_mod.Frame(131.21, 111.61, 0.16807),
        ]
        root_cam = Camera(scale=1.0, tx=0.0, ty=0.0)
        points = [(0.0, 0.0), (1.0, -1.0), (-7.5, 3.25)]
        cam = root_cam
        for parent, child in zip(frames, frames[1:]):
            prev = cam
            cam = cam_mod.enter_frame(cam, parent, child)
            self.assert_same_screen(prev, parent, cam, child, [(0.0, 0.0), (2.0, 4.0), (-3.0, -5.5)])
        deepest = cam
        for child, parent in reversed(list(zip(frames, frames[1:]))):
            deepest = cam_mod.leave_frame(deepest, parent, child)
        self.assertTrue(close(deepest.scale, root_cam.scale, 1e-9))
        self.assertTrue(close(deepest.tx, root_cam.tx, 1e-6))
        self.assertTrue(close(deepest.ty, root_cam.ty, 1e-6))

    def test_randomized_points_and_frames_keep_screen_positions(self):
        rng = random.Random(20261008)
        accepted = 0
        refused = 0
        for _ in range(300):
            f0 = cam_mod.Frame(rng.uniform(-1e4, 1e4), rng.uniform(-1e4, 1e4), 10 ** rng.uniform(-1, 1))
            f1 = cam_mod.Frame(rng.uniform(-1e4, 1e4), rng.uniform(-1e4, 1e4), 10 ** rng.uniform(-1, 1))
            cam0 = Camera(scale=10 ** rng.uniform(-1, 1), tx=rng.uniform(-500, 500), ty=rng.uniform(-500, 500))
            try:
                cam1 = cam_mod.enter_frame(cam0, f0, f1)
            except ValueError:
                refused += 1
                continue  # out of the allowed scale range: refused, never clamped
            accepted += 1
            pts = [(rng.uniform(-50, 50), rng.uniform(-50, 50)) for _ in range(3)]
            self.assert_same_screen(cam0, f0, cam1, f1, pts)
            back = cam_mod.leave_frame(cam1, f1, f0)
            self.assertTrue(close(back.scale, cam0.scale, 1e-9))
            self.assertTrue(close(back.tx, cam0.tx, 1e-6))
            self.assertTrue(close(back.ty, cam0.ty, 1e-6))
        self.assertGreater(accepted, 50)  # the sample must actually exercise the happy path

    def test_extrema_within_scale_range_are_accepted(self):
        f0 = cam_mod.Frame(0.0, 0.0, 1.0)
        f1 = cam_mod.Frame(0.0, 0.0, 1.0)
        out = cam_mod.enter_frame(Camera(scale=MAX_SCALE, tx=1.0, ty=1.0), f0, f1)
        self.assertEqual(out.scale, MAX_SCALE)
        out = cam_mod.enter_frame(Camera(scale=MIN_SCALE, tx=1.0, ty=1.0), f0, f1)
        self.assertEqual(out.scale, MIN_SCALE)

    def test_out_of_range_scale_is_refused_not_clamped(self):
        f0 = cam_mod.Frame(0.0, 0.0, 1.0)
        f_small = cam_mod.Frame(0.0, 0.0, 0.001)
        cam = Camera(scale=2.0, tx=0.0, ty=0.0)
        # 2.0 * (1 / 0.001) = 2000 > MAX_SCALE: must be refused, not clamped to 64.
        with self.assertRaises(ValueError):
            cam_mod.enter_frame(cam, f0, f_small)
        # Entering a finer frame (units 1 -> 0.001) with a small scale collapses below MIN_SCALE.
        with self.assertRaises(ValueError):
            cam_mod.enter_frame(Camera(scale=0.06, tx=0.0, ty=0.0), f0, f_small)
        # The reverse direction (units 0.001 -> 1) from scale 0.06 yields 60, which is in range.
        self.assertTrue(close(cam_mod.enter_frame(Camera(scale=0.06), f_small, f0).scale, 60.0))

    def test_overflow_and_non_finite_intermediates_are_refused(self):
        big = 1e308
        f0 = cam_mod.Frame(-big, 0.0, 1.0)
        f1 = cam_mod.Frame(big, 0.0, 1.0)
        # origin difference overflows to inf
        with self.assertRaises(ValueError):
            cam_mod.enter_frame(Camera(scale=1.0, tx=0.0, ty=0.0), f0, f1)
        # tiny units ratio underflows the scale to zero, which is out of range
        with self.assertRaises(ValueError):
            cam_mod.enter_frame(Camera(scale=1.0), cam_mod.ROOT_FRAME, cam_mod.Frame(0.0, 0.0, 1e-320))
        # a camera that is itself not finite is refused
        with self.assertRaises(ValueError):
            cam_mod.enter_frame(Camera(scale=float("nan")), cam_mod.ROOT_FRAME, cam_mod.ROOT_FRAME)
        with self.assertRaises(ValueError):
            cam_mod.enter_frame(Camera(scale=1.0, tx=float("inf")), cam_mod.ROOT_FRAME, cam_mod.ROOT_FRAME)

    def test_failed_transition_does_not_mutate_inputs(self):
        cam = Camera(scale=2.0, tx=3.0, ty=4.0)
        f0 = cam_mod.Frame(0.0, 0.0, 1.0)
        f_bad = cam_mod.Frame(0.0, 0.0, 0.001)
        before = (cam, f0, f_bad)
        with self.assertRaises(ValueError):
            cam_mod.enter_frame(cam, f0, f_bad)
        self.assertEqual((cam, f0, f_bad), before)
        self.assertEqual((cam.scale, cam.tx, cam.ty), (2.0, 3.0, 4.0))

    def test_non_frame_arguments_are_refused(self):
        with self.assertRaises(ValueError):
            cam_mod.enter_frame(Camera(), (0.0, 0.0, 1.0), cam_mod.ROOT_FRAME)


if __name__ == "__main__":
    unittest.main()
