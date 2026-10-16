"""Focused adapter checks with a tiny fake environment; no gym import required."""

import math
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest

import numpy as np

from racing_dashboard.simulation import (
    RouteProgress, SimulationSession, command_geometry, display_grid,
    load_source, local_to_world, run_worker, validate_changes, validate_options,
)


PLANNER = '''
from dataclasses import dataclass, field
import numpy as np
@dataclass
class PlannerConfig:
    max_speed: float = 2.2
    tracking_lookahead: float = .3
    goal_lookahead: float = 3.
    goal_bias: float = .25
    max_expansion_dist: float = .4
    planning_budget: float = .035
    random_seed: int = 7
    wheelbase: float = .3302
    reaction_time: float = .2
    braking_deceleration: float = 2.5
    vehicle_front: float = .46145
    vehicle_rear: float = .11855
    vehicle_half_width: float = .155
@dataclass
class PlanResult:
    speed: float = 0.
    steering: float = 0.
    path: object = field(default_factory=lambda: np.empty((0,2)))
    tree_edges: object = field(default_factory=lambda: np.empty((0,2)))
    goal: object = None
class LocalGrid:
    def __init__(self, config):
        self.config=config
        self.x_min=-.5
        self.y_min=-1.
        self.resolution=.1
        self.grid=np.zeros((20,20), dtype=np.int8)
        self.hits=np.array([[1.,1.]])
    def update(self, *args): pass
class LocalPlanner:
    def __init__(self, waypoints, config):
        self.config=config
        self.waypoints=waypoints
        self.rng=np.random.default_rng(config.random_seed)
        self.path_world=np.ones((2,2))
    def reset(self): self.path_world=np.empty((0,2))
    def plan(self, grid, pose):
        return PlanResult(speed=self.config.max_speed, path=np.array([[0.,0.],[1.,0.]]))
'''

HELPER = '''
import types
import numpy as np
def build_env(rear_start, noise_std, seed):
    return Env(rear_start)
def rear_pose(env): return env.pose.copy()
class Env:
    def __init__(self, start):
        self.pose=np.array(start, dtype=float)
        self.unwrapped=self
        self.closed=False
        self.sim=types.SimpleNamespace(state=types.SimpleNamespace(
            scans=np.ones((1,10)), collisions=np.array([False]),
            standard_state=np.zeros((1,4))))
        self.env_config=types.SimpleNamespace(
            simulation_config=types.SimpleNamespace(timestep=.01),
            lidar_config=types.SimpleNamespace(angle_max=1.,angle_min=-1.,
                num_beams=10,range_min=.05,range_max=25.))
    def step(self, action):
        speed=action[0,1]
        self.pose[:2] += .01*speed*np.array([np.cos(self.pose[2]),np.sin(self.pose[2])])
        self.sim.state.standard_state[0,3]=speed
    def close(self): self.closed=True
'''


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "lab7_pkg/lab7_pkg").mkdir(parents=True)
        (self.root / "lab7_pkg/test").mkdir()
        (self.root / "lab7_pkg/waypoints").mkdir()
        self.planner_file = self.root / "lab7_pkg/lab7_pkg/planner.py"
        self.planner_file.write_text(PLANNER)
        helper_file = self.root / "lab7_pkg/test/simulator_support.py"
        helper_file.write_text(HELPER)
        (self.root / "lab7_pkg/waypoints/levine_centerline.csv").write_text("9,2\n10,2\n10,10\n9,10\n")
        self.options = {"repo": str(self.root), "helper": str(helper_file)}

    def tearDown(self):
        self.directory.cleanup()

    def test_finite_bounded_changes_only(self):
        self.assertEqual(validate_changes({"max_speed": 1.5}), {"max_speed": 1.5})
        for changes in ({"max_speed": float("nan")}, {"goal_bias": 2},
                        {"max_speed": True}, {"vehicle_front": .1},
                        {"planning_budget": "0.02"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_changes(changes)
        for options in ({"seed": -1}, {"seed": 1.2}, {"rate": 8},
                        {"control_dt": 0}, {"case": "random"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                validate_options({**self.options, **options})

    def test_bundled_helper_is_independent_default(self):
        settings = validate_options({"repo": str(self.root)})
        helper = Path(settings["helper"])
        self.assertEqual(helper.name, "f1tenth_helper.py")
        self.assertTrue(helper.is_file())
        self.assertNotIn(str(self.root), str(helper))

    def test_world_geometry_and_grid_origin(self):
        pose = [10., 20., math.pi / 2]
        np.testing.assert_allclose(local_to_world([[1., 0.]], pose), [[10., 21.]])
        session = SimulationSession(self.options)
        try:
            grid = display_grid(session.grid, pose, max_cells=100)
            self.assertEqual(grid["width"], 10)
            self.assertEqual(grid["height"], 10)
            np.testing.assert_allclose(grid["origin"], [11., 19.5, math.pi / 2])
            arc, bodies = command_geometry(session.config, 1., 0., pose)
            self.assertAlmostEqual(arc[-1][0], 10.)
            self.assertGreater(arc[-1][1], 20.)
            self.assertEqual(len(bodies[0]), 5)
        finally:
            session.close()

    def test_apply_keeps_pose_rng_and_clears_path(self):
        session = SimulationSession(self.options)
        try:
            self.assertEqual(session.snapshot()["status"], "paused")
            old_pose, old_rng = session.helper.rear_pose(session.env), session.planner.rng
            session.apply({"max_speed": 1.1})
            np.testing.assert_array_equal(session.helper.rear_pose(session.env), old_pose)
            self.assertIs(session.planner.rng, old_rng)
            self.assertEqual(len(session.planner.path_world), 0)
            self.assertIs(session.grid.config, session.planner.config)
            self.assertEqual(session.config.max_speed, 1.1)
            with self.assertRaises(ValueError):
                session.apply({"max_speed": float("inf")})
            self.assertEqual(session.config.max_speed, 1.1)
            session.step()
            snapshot = session.snapshot()
            self.assertAlmostEqual(snapshot["time_s"], .04)
            self.assertEqual(snapshot["speed_command"], 1.1)
            self.assertNotEqual(snapshot["pose"], old_pose.tolist())
            self.assertEqual(snapshot["diagnostics"]["observation_time_s"], 0.)
        finally:
            session.close()

    def test_new_session_loads_new_selected_branch_source(self):
        first = SimulationSession(self.options)
        digest = first.metadata["planner_sha256"]
        first.close()
        # Same length and mtime deliberately catches stale .pyc loaders.
        original_stat = self.planner_file.stat()
        self.planner_file.write_text(PLANNER.replace("max_speed: float = 2.2", "max_speed: float = 1.2"))
        import os
        os.utime(self.planner_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        second = SimulationSession(self.options)
        try:
            self.assertEqual(second.config.max_speed, 1.2)
            self.assertNotEqual(second.metadata["planner_sha256"], digest)
            self.assertEqual(second.elapsed, 0.)
            self.assertFalse((self.planner_file.parent / "__pycache__").exists())
        finally:
            second.close()

    def test_route_progress_wraps_and_measures_segment_distance(self):
        route = np.array([[0.,0.],[2.,0.],[2.,2.],[0.,2.]])
        progress = RouteProgress(route, [0.,.2,0.])
        progress.update([.2,0.,0.])
        self.assertAlmostEqual(progress.distance, .4)
        progress.update([1.,.3,0.])
        self.assertAlmostEqual(progress.cross_track, .3)
        self.assertAlmostEqual(progress.distance, 1.2)

    def test_worker_pause_step_apply_reset_and_stop(self):
        commands, output = queue.Queue(), queue.Queue(maxsize=100)
        worker = threading.Thread(target=run_worker, args=(commands, output, self.options), daemon=True)
        worker.start()

        def next_snapshot(predicate=lambda data: True):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                message = output.get(timeout=3)
                self.assertNotEqual(message["type"], "error", message)
                if message["type"] == "snapshot" and predicate(message["data"]):
                    return message["data"]
            self.fail("Worker did not emit expected snapshot")

        try:
            ready = next_snapshot(lambda data: data["status"] == "paused")
            self.assertEqual(ready["time_s"], 0.)
            commands.put({"action": "step"})
            step = next_snapshot(lambda data: data.get("time_s", 0) > 0)
            self.assertEqual(step["status"], "paused")
            commands.put({"action": "apply", "config": {"max_speed": 1.0}})
            applied = next_snapshot(lambda data: data.get("config", {}).get("max_speed") == 1.)
            self.assertEqual(applied["time_s"], step["time_s"])
            commands.put({"action": "reset"})
            reset = next_snapshot(lambda data: data.get("time_s") == 0.)
            self.assertEqual(reset["config"]["max_speed"], 1.)
            self.assertEqual(reset["status"], "paused")
        finally:
            commands.put({"action": "stop"})
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
