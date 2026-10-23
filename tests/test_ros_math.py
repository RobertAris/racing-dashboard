"""Meaningful adapter frame/staleness tests without installing ROS on a Mac."""
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from racing_dashboard.ros_adapter import (
    LiveState, RouteProjection, grid_origin, marker_points, parser, quaternion_yaw,
    transform_point, transform_pose,
    load_route_records,
)


def quaternion(yaw, scale=1):
    return NS(x=0.0, y=0.0, z=scale*math.sin(yaw/2), w=scale*math.cos(yaw/2))


def pose(x=0, y=0, yaw=0):
    return NS(position=NS(x=x, y=y, z=0), orientation=quaternion(yaw))


def header(frame="map", seconds=1):
    return NS(frame_id=frame, stamp=NS(sec=seconds, nanosec=0))


class TransformTests(unittest.TestCase):
    def test_transform_rotates_before_translation(self):
        x, y = transform_point([2, 0], [10, -3, math.pi/2])
        self.assertAlmostEqual(x, 10)
        self.assertAlmostEqual(y, -1)

    def test_pose_composes_heading_and_wraps(self):
        result = transform_pose([1, 0, math.pi], [5, 8, math.pi/2])
        self.assertAlmostEqual(result[0], 5)
        self.assertAlmostEqual(result[1], 9)
        self.assertAlmostEqual(result[2], -math.pi/2)

    def test_quaternion_is_normalized(self):
        self.assertAlmostEqual(quaternion_yaw(quaternion(-0.9, scale=7)), -0.9)
        with self.assertRaises(ValueError):
            quaternion_yaw(NS(x=0, y=0, z=0, w=0))

    def test_grid_origin_composes_own_rotation(self):
        info = NS(origin=pose(2, 1, math.pi/4))
        result = grid_origin(info, [10, -5, math.pi/2])
        self.assertAlmostEqual(result[0], 9)
        self.assertAlmostEqual(result[1], -3)
        self.assertAlmostEqual(result[2], 3*math.pi/4)

    def test_marker_pose_and_frame_transform_both_apply(self):
        marker = NS(pose=pose(2, 0, math.pi/2), points=[NS(x=1, y=0)])
        points = marker_points(marker, [10, -2, math.pi/2])
        self.assertAlmostEqual(points[0][0], 9)
        self.assertAlmostEqual(points[0][1], 0)

    def test_nonfinite_coordinates_are_rejected(self):
        with self.assertRaises(ValueError):
            transform_point([float("nan"), 0], [0, 0, 0])

    def test_route_projection_wraps_arc_continuously_and_caches_segments(self):
        projector = RouteProjection()
        route = [[0, 0], [2, 0], [2, 2], [0, 2]]
        projector.set_route(route)
        segments = projector.segments
        distance, before_seam = projector.project([-0.1, 0.2])
        self.assertAlmostEqual(distance, 0.1)
        self.assertAlmostEqual(before_seam, 7.8)
        projector.set_route([list(point) for point in route])
        self.assertIs(projector.segments, segments)
        distance, after_seam = projector.project([0.3, -0.1])
        self.assertAlmostEqual(distance, 0.1)
        self.assertAlmostEqual(after_seam, 8.3)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.clock = [10.0]
        self.lookups = []
        options = parser().parse_args(["--planner-profile", "rrt"])

        def lookup(frame, stamp):
            self.lookups.append((frame, stamp.sec))
            if frame == "missing":
                raise ValueError("TF unavailable")
            return {"map": (0, 0, 0), "odom": (10, 1, math.pi/2),
                    "laser": (2, 3, math.pi/2)}[frame]

        self.state = LiveState(options, lookup, now=lambda: self.clock[0])

    def odometry(self, frame="odom"):
        return NS(header=header(frame), pose=NS(pose=pose(2, 0, 0)),
                  twist=NS(twist=NS(linear=NS(x=1.5))))

    def test_localized_odometry_uses_its_frame_and_stamp(self):
        self.state.update("pose", self.odometry(), self.state.pose)
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["status"], "connected")
        self.assertAlmostEqual(snapshot["pose"][0], 10)
        self.assertAlmostEqual(snapshot["pose"][1], 3)
        self.assertAlmostEqual(snapshot["pose"][2], math.pi/2)
        self.assertEqual(snapshot["actual_speed"], 1.5)
        self.assertEqual(self.lookups, [("odom", 1)])

    def test_tf_failure_clears_previously_valid_pose(self):
        self.state.update("pose", self.odometry(), self.state.pose)
        self.state.update("pose", self.odometry("missing"), self.state.pose)
        snapshot = self.state.snapshot()
        self.assertIsNone(snapshot["pose"])
        self.assertIsNone(snapshot["actual_speed"])
        self.assertEqual(snapshot["status"], "invalid_pose")
        self.assertIn("TF unavailable", snapshot["diagnostics"]["errors"]["pose"])

    def test_stale_dynamics_clear_but_grid_reports_age(self):
        self.state.update("pose", self.odometry(), self.state.pose)
        grid = NS(header=header(), info=NS(width=2, height=1, resolution=0.1, origin=pose()), data=[0, 100])
        self.state.update("grid", grid, self.state.grid)
        self.state.update("path", NS(header=header(), poses=[NS(header=header(), pose=pose(1, 1))]), self.state.path)
        self.clock[0] += 2
        snapshot = self.state.snapshot()
        self.assertIsNone(snapshot["pose"])
        self.assertEqual(snapshot["path"], [])
        self.assertEqual(snapshot["status"], "stale_pose")
        self.assertEqual(snapshot["grid"]["data"], [0, 100])
        self.assertEqual(snapshot["diagnostics"]["grid_age_s"], 2)

    def test_invalid_grid_does_not_leave_old_map(self):
        grid = NS(header=header(), info=NS(width=2, height=1, resolution=0.1, origin=pose()), data=[0, 100])
        self.state.update("grid", grid, self.state.grid)
        grid.data = [0]
        self.state.update("grid", grid, self.state.grid)
        self.assertIsNone(self.state.snapshot()["grid"])

    def test_scan_uses_sensor_extrinsics_and_discards_invalid_ranges(self):
        scan = NS(header=header("laser", 9), angle_min=0, angle_increment=math.pi/2,
                  range_min=0.1, range_max=5, ranges=[1, float("inf"), 0.05, float("nan")])
        points = self.state.scan(scan)
        self.assertEqual(len(points), 1)
        self.assertAlmostEqual(points[0][0], 2)
        self.assertAlmostEqual(points[0][1], 4)
        self.assertEqual(self.lookups, [("laser", 9)])

    def test_path_honors_per_pose_frame_and_stamp(self):
        message = NS(header=header("map", 2), poses=[
            NS(header=header("odom", 3), pose=pose(1, 0)),
            NS(header=header("", 0), pose=pose(4, 5)),
        ])
        points = self.state.path(message)
        self.assertAlmostEqual(points[0][0], 10)
        self.assertAlmostEqual(points[0][1], 2)
        self.assertEqual(points[1], [4, 5])
        self.assertEqual(self.lookups, [("odom", 3), ("map", 2)])

    def test_deleted_marker_clears_tree_without_requiring_tf(self):
        marker = NS(header=header("missing"), action=2)
        self.assertEqual(self.state.marker(marker, "tree"), [])
        self.assertEqual(self.lookups, [])

    def test_persistent_route_reports_age_and_projection_for_fresh_pose(self):
        marker = NS(header=header(), action=0, type=4, frame_locked=False,
                    pose=pose(), points=[NS(x=0, y=0), NS(x=10, y=0), NS(x=10, y=5)])
        self.state.update("route", marker, lambda msg: self.state.marker(msg, "route"))
        self.clock[0] += 2
        message = self.odometry("map")
        message.pose.pose = pose(3, 0.2)
        self.state.update("pose", message, self.state.pose)
        snapshot = self.state.snapshot()
        self.assertEqual(len(snapshot["route"]), 3)
        self.assertEqual(snapshot["diagnostics"]["route_age_s"], 2)
        self.assertAlmostEqual(snapshot["cross_track_error"], 0.2)
        self.assertAlmostEqual(snapshot["progress_m"], 3)

    def test_localization_jump_resets_progress_and_invalid_tf_has_no_metrics(self):
        self.state.static_route = [[0, 0], [2, 0], [2, 2], [0, 2]]
        message = self.odometry("map")
        for x, y in ((0, 0.2), (0.3, 0), (0.4, 0)):
            message.pose.pose = pose(x, y)
            self.state.update("pose", message, self.state.pose)
            before = self.state.snapshot()
        self.assertAlmostEqual(before["progress_m"], 8.4)
        message.pose.pose = pose(2, 1)
        self.state.update("pose", message, self.state.pose)
        jumped = self.state.snapshot()
        self.assertTrue(jumped["diagnostics"]["localization_jump"])
        self.assertGreater(jumped["diagnostics"]["localization_jump_m"], 1)
        self.assertAlmostEqual(jumped["progress_m"], 3)
        self.state.update("pose", self.odometry("missing"), self.state.pose)
        invalid = self.state.snapshot()
        self.assertIsNone(invalid["cross_track_error"])
        self.assertIsNone(invalid["progress_m"])

    def test_mpc_horizons_are_separate_from_full_route_and_velocity_is_magnitude(self):
        options = parser().parse_args([])
        self.assertEqual(options.planner_profile, "mpc")
        self.assertEqual(options.pose_topic, "/pf/pose/odom")
        self.assertEqual(options.drive_topic, "/drive")
        state = LiveState(options, lambda frame, stamp: (0, 0, 0), now=lambda: self.clock[0])
        horizon = NS(header=header(), poses=[NS(header=header(), pose=pose(1, 2)),
                                            NS(header=header(), pose=pose(2, 2))])
        state.update("reference", horizon, state.path)
        state.update("path", horizon, state.path)
        # Legacy overlays are ineligible for the default MPC lap source.
        state.values["route"] = [[0, 0], [2, 0], [2, 2], [0, 2]]
        message = self.odometry("map")
        message.twist.twist.linear = NS(x=-3, y=4)
        state.update("pose", message, state.pose)
        snapshot = state.snapshot()
        self.assertEqual(snapshot["actual_speed"], 5)
        self.assertEqual(snapshot["reference"], [[1, 2], [2, 2]])
        self.assertEqual(snapshot["route"], [])
        self.assertFalse(snapshot["lap"]["enabled"])
        self.assertIn("--route-file", snapshot["lap"]["disabled_reason"])
        self.assertEqual(state.topics["path"], "/mpc/prediction")
        self.assertEqual(state.topics["reference"], "/mpc/reference")
        self.assertNotIn("route", state.topics)
        self.assertFalse(any("/rrt/" in topic for topic in state.topics.values()))
        self.assertEqual(snapshot["diagnostics"]["planner_unavailable"], {})
        self.assertFalse(any("rejection" in message for message in snapshot["diagnostics"]["limitations"]))
        self.clock[0] += 2
        stale = state.snapshot()["diagnostics"]["planner_unavailable"]
        self.assertIn("stale", stale["reference"])
        self.assertIn("solver status is unknown", stale["path"])

    def test_full_csv_preserves_yaw_speed_and_cannot_be_overwritten_by_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory)/"route.csv"
            filename.write_text("# x,y,yaw,speed\n0,0,0,2\n2,0,1.5,3\n2,2,3.1,1\n0,2,-1.5,2\n")
            options = parser().parse_args(["--route-file", str(filename)])
            state = LiveState(options, lambda frame, stamp: (0, 0, 0), now=lambda: self.clock[0])
            state.values["route"] = [[50, 50], [51, 50]]
            state.update("pose", self.odometry("map"), state.pose)
            snapshot = state.snapshot()
            self.assertEqual(snapshot["route"], [[0, 0], [2, 0], [2, 2], [0, 2]])
            self.assertEqual(state.route_records[1]["speed"], 3)
            self.assertEqual(state.route_records[1]["yaw"], 1.5)
            self.assertTrue(snapshot["lap"]["enabled"])
            filename.write_text("x,y,yaw,speed\n0,0,nope,2\n2,2,0,2\n")
            with self.assertRaises(ValueError):
                load_route_records(filename)

    def test_pose_offset_is_explicit_and_original_localization_is_retained(self):
        options = parser().parse_args(["--pose-offset-m", "0.25"])
        state = LiveState(options, lambda frame, stamp: (0, 0, 0), now=lambda: self.clock[0])
        message = self.odometry("map")
        message.pose.pose = pose(1, 2, math.pi/2)
        state.update("pose", message, state.pose)
        self.assertEqual(state.values["pose"]["raw_pose"][:2], [1, 2])
        self.assertAlmostEqual(state.snapshot()["pose"][1], 1.75)
        self.assertEqual(state.snapshot()["metadata"]["pose_offset_m"], 0.25)

    def test_lap_capture_runs_on_pose_samples_and_not_on_repeated_snapshots(self):
        from tests.test_laps import ROUTE, sample
        options = parser().parse_args(["--planner-profile", "mpc", "--lap-gate-x", "2",
                                      "--lap-gate-y", "0", "--lap-gate-yaw", str(math.pi/2)])
        bundles = []
        state = LiveState(options, lambda frame, stamp: (0, 0, 0), now=lambda: self.clock[0],
                          lap_sink=lambda bundle: bundles.append(bundle))
        state.static_route = ROUTE
        message = self.odometry("map")
        for i in range(150):
            point = sample(-0.4+i*0.05, 1+i*0.05)
            seconds = point["time_s"]
            message.header.stamp = NS(sec=int(seconds), nanosec=round((seconds-int(seconds))*1e9))
            message.pose.pose = pose(*point["pose"])
            state.update("pose", message, state.pose)
            for _ in range(3):
                state.snapshot()
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0]["status"], "valid")
        self.assertEqual(state.pose_sequence, 150)
        count = state.laps.status()["sample_count"]
        state.snapshot()
        state.snapshot()
        self.assertEqual(state.laps.status()["sample_count"], count)
        self.clock[0] += 2
        state.snapshot()
        self.assertEqual(bundles[-1]["reason"], "telemetry_gap")
        self.assertFalse(state.laps.status()["recording"])


if __name__ == "__main__":
    unittest.main()
