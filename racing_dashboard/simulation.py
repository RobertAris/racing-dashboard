"""Isolated, read-only adapter for a Lab 6 checkout and its real F1TENTH simulator.

The process target imports simulator dependencies only after startup. Every reset
compiles the selected checkout's sources again; nothing is copied into the tool
and the planner is never patched or given simulator collision/lap information.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, is_dataclass, replace
import hashlib
import io
import math
from pathlib import Path
import queue
import subprocess
import sys
import time
import types
import uuid

import numpy as np


# Modest dashboard ranges, in SI units. These also protect against accidental
# extreme commands; the source planner's own configuration validation still runs.
TUNABLES = {
    "max_speed": (0.1, 5.0),
    "tracking_lookahead": (0.1, 2.0),
    "goal_lookahead": (0.5, 5.0),
    "goal_bias": (0.0, 1.0),
    "max_expansion_dist": (0.05, 1.5),
    "planning_budget": (0.001, 0.15),
}
STARTS = {
    "lap": (9.96, 2.8, math.pi / 2),
    "turn": (9.96, 2.8, math.pi / 2),
    "straight": (8.08, 8.67, math.pi),
}
TERMINAL = {"complete", "collision", "no_progress", "error"}


def _number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return result


def validate_changes(changes):
    """Validate a partial dashboard configuration without changing active state."""
    if not isinstance(changes, dict):
        raise ValueError("config must be an object containing tuning parameters")
    unknown = set(changes) - set(TUNABLES)
    if unknown:
        raise ValueError("Unsupported tuning parameters: " + ", ".join(sorted(unknown)))
    return {key: _number(value, key, *TUNABLES[key]) for key, value in changes.items()}


def validate_options(options):
    if not isinstance(options, dict):
        raise ValueError("Simulation options must be an object")
    value = dict(options)
    if not value.get("repo"):
        raise ValueError("Select a Lab 6 repository using --repo or the repository field")
    value["repo"] = str(Path(value["repo"]).expanduser().resolve())
    value["helper"] = str(Path(value.get("helper") or Path(__file__).with_name("f1tenth_helper.py")).expanduser().resolve())
    if value.get("case", "lap") not in STARTS:
        raise ValueError("case must be lap, straight, or turn")
    value["case"] = value.get("case", "lap")
    seed = value.get("seed", 7)
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer between 0 and 4294967295")
    value["seed"] = seed
    value["noise"] = _number(value.get("noise", 0.01), "noise", 0.0, 1.0)
    value["control_dt"] = _number(value.get("control_dt", 0.04), "control_dt", 0.01, 0.5)
    value["rate"] = _number(value.get("rate", 1.0), "rate", 0.25, 4.0)
    value["config"] = validate_changes(value.get("config", {}))
    return value


def load_source(path, role):
    """Import trusted local Python freshly, bypassing timestamp-based bytecode."""
    source = Path(path).resolve()
    if not source.is_file():
        raise ValueError(f"{role} file does not exist: {source}")
    name = f"_robertaris_{role}_{uuid.uuid4().hex}"
    module = types.ModuleType(name)
    module.__file__ = str(source)
    module.__package__ = ""
    source_bytes = source.read_bytes()
    module.__source_sha256__ = hashlib.sha256(source_bytes).hexdigest()
    sys.modules[name] = module  # dataclasses resolve their defining module here
    try:
        exec(compile(source_bytes, str(source), "exec"), module.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def source_metadata(repo, planner_path, waypoint_path, helper_path, digests=None):
    def git(*args):
        try:
            result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                    text=True, timeout=3, check=True)
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    status = git("status", "--porcelain")
    metadata = {
        "repo": str(repo), "branch": git("branch", "--show-current") or "detached/unknown",
        "commit": git("rev-parse", "HEAD"), "dirty": None if status is None else bool(status),
        "planner_sha256": hashlib.sha256(Path(planner_path).read_bytes()).hexdigest(),
        "waypoints_sha256": hashlib.sha256(Path(waypoint_path).read_bytes()).hexdigest(),
        "helper_sha256": hashlib.sha256(Path(helper_path).read_bytes()).hexdigest(),
        "planner_file": str(planner_path), "waypoints_file": str(waypoint_path),
        "helper_file": str(helper_path), "model": "F1TENTH gym dynamics and LiDAR",
    }
    metadata.update(digests or {})
    return metadata


def local_to_world(points, pose):
    points = np.asarray(points, dtype=float).reshape(-1, 2)
    c, s = math.cos(pose[2]), math.sin(pose[2])
    return points @ np.array([[c, s], [-s, c]]) + np.asarray(pose[:2])


class RouteProgress:
    """Continuous nearest-segment projection; observes pose, never simulator laps."""

    def __init__(self, waypoints, pose):
        self.points = np.asarray(waypoints, dtype=float)
        self.segments = np.roll(self.points, -1, axis=0) - self.points
        self.lengths = np.linalg.norm(self.segments, axis=1)
        self.arc = np.r_[0.0, np.cumsum(self.lengths[:-1])]
        self.length = float(self.lengths.sum())
        if self.length <= 0 or np.any(self.lengths <= 0):
            raise ValueError("The route must be a loop with distinct adjacent points")
        self.previous, self.cross_track = self.project(pose)
        self.distance = 0.0

    def project(self, pose):
        fractions = np.clip(np.sum((np.asarray(pose[:2]) - self.points) * self.segments,
                                   axis=1) / self.lengths**2, 0.0, 1.0)
        projections = self.points + fractions[:, None] * self.segments
        distances = np.linalg.norm(projections - pose[:2], axis=1)
        index = int(np.argmin(distances))
        return float(self.arc[index] + fractions[index] * self.lengths[index]), float(distances[index])

    def update(self, pose):
        current, self.cross_track = self.project(pose)
        delta = (current - self.previous + self.length / 2) % self.length - self.length / 2
        self.distance += delta
        self.previous = current
        return self.distance


def display_grid(grid, pose, max_cells=4500):
    """Conservative display-only pooling. Planner always retains its original grid."""
    values = np.asarray(grid.grid)
    scale = max(1, int(math.ceil(math.sqrt(values.size / max_cells))))
    height, width = (int(math.ceil(size / scale)) for size in values.shape)
    if scale > 1:
        padded = np.full((height * scale, width * scale), -1, dtype=np.int8)
        padded[:values.shape[0], :values.shape[1]] = values
        blocks = padded.reshape(height, scale, width, scale)
        free = np.all(blocks == 0, axis=(1, 3))
        occupied = np.any(blocks > 0, axis=(1, 3))
        values = np.where(occupied, 100, np.where(free, 0, -1))
    origin = local_to_world([[grid.x_min, grid.y_min]], pose)[0]
    return {"data": values.astype(int).ravel().tolist(), "width": width, "height": height,
            "resolution": float(grid.resolution * scale),
            "origin": [float(origin[0]), float(origin[1]), float(pose[2])]}


def command_geometry(config, speed, steering, pose):
    """Derived stopping-distance command arc, not a captured internal trial arc."""
    curvature = math.tan(steering) / config.wheelbase
    distance = max(0.2, abs(speed) * config.reaction_time
                   + speed * speed / (2 * config.braking_deceleration)) if speed else 0.0
    arc = np.linspace(0, distance, max(2, min(120, int(distance / 0.025) + 1)))
    headings = arc * curvature
    if abs(curvature) < 1e-8:
        points = np.column_stack((arc, np.zeros(len(arc))))
    else:
        points = np.column_stack((np.sin(headings) / curvature, (1 - np.cos(headings)) / curvature))
    rectangle = np.array([[config.vehicle_front, config.vehicle_half_width],
                          [config.vehicle_front, -config.vehicle_half_width],
                          [-config.vehicle_rear, -config.vehicle_half_width],
                          [-config.vehicle_rear, config.vehicle_half_width],
                          [config.vehicle_front, config.vehicle_half_width]])
    footprints = []
    for index in np.unique(np.linspace(0, len(points) - 1, min(6, len(points))).astype(int)):
        footprint_local = local_to_world(rectangle, [*points[index], headings[index]])
        footprints.append(local_to_world(footprint_local, pose).tolist())
    return local_to_world(points, pose).tolist(), footprints


class SimulationSession:
    """One simulation, owned entirely by its worker process."""

    def __init__(self, options):
        self.env = None
        self.modules = []
        try:
            self._initialize(options)
        except Exception:
            self.close()
            raise

    def _initialize(self, options):
        self.options = validate_options(options)
        repo = Path(self.options["repo"])
        planner_path = repo / "lab7_pkg/lab7_pkg/planner.py"
        helper_path = Path(self.options["helper"])
        waypoint_path = Path(self.options.get("waypoints") or repo / "lab7_pkg/waypoints/levine_centerline.csv").expanduser().resolve()
        self.module = load_source(planner_path, "planner")
        self.modules.append(self.module.__name__)
        for name in ("PlannerConfig", "LocalPlanner", "LocalGrid", "PlanResult"):
            if not hasattr(self.module, name):
                raise ValueError(f"Selected branch's planner is missing {name}; expected the Lab 6 planner API")
        self.helper = load_source(helper_path, "helper")
        self.modules.append(self.helper.__name__)
        for name in ("build_env", "rear_pose"):
            if not callable(getattr(self.helper, name, None)):
                raise ValueError(f"Simulator helper must provide {name}()")
        if not waypoint_path.is_file():
            raise ValueError(f"Waypoint CSV does not exist: {waypoint_path}")
        try:
            waypoint_bytes = waypoint_path.read_bytes()
            waypoints = np.loadtxt(io.StringIO(waypoint_bytes.decode("utf-8")), delimiter=",",
                                   comments="#", ndmin=2)[:, :2]
        except Exception as exc:
            raise ValueError(f"Cannot load x,y waypoint CSV {waypoint_path}: {exc}") from exc
        self.config = replace(self.module.PlannerConfig(), random_seed=self.options["seed"],
                              **self.options["config"])
        if not is_dataclass(self.config):
            raise ValueError("PlannerConfig must be a dataclass supporting validated replace()")
        self.planner = self.module.LocalPlanner(waypoints, self.config)
        self.grid = self.module.LocalGrid(self.config)
        self.route = np.asarray(self.planner.waypoints)
        self.start = STARTS[self.options["case"]]
        build_arguments = {"rear_start": self.start, "noise_std": self.options["noise"],
                           "seed": self.options["seed"]}
        requested_map = self.options.get("map_yaml")
        if requested_map:
            map_yaml = Path(requested_map).expanduser().resolve()
            if not map_yaml.is_file():
                raise ValueError(f"Map YAML does not exist: {map_yaml}")
            # The Lab 6 helper selects maps by MAPS/<track_name>.yaml. Reject
            # arbitrary locations rather than silently simulating another map.
            maps = getattr(self.helper, "MAPS", None)
            if maps is None or map_yaml.parent != Path(maps).resolve():
                raise ValueError("This simulator helper loads maps from its MAPS directory only; "
                                 "provide a helper with the desired MAPS directory")
            build_arguments["track_name"] = map_yaml.stem
            self.map_yaml = str(map_yaml)
        else:
            maps = getattr(self.helper, "MAPS", None)
            self.map_yaml = str(Path(maps) / "levine_obs.yaml") if maps is not None else None
        self.metadata = source_metadata(repo, planner_path, waypoint_path, helper_path, {
            "planner_sha256": self.module.__source_sha256__,
            "helper_sha256": self.helper.__source_sha256__,
            "waypoints_sha256": hashlib.sha256(waypoint_bytes).hexdigest(),
        })
        self.metadata.update({"case": self.options["case"], "seed": self.options["seed"],
                              "noise": self.options["noise"], "map_yaml": self.map_yaml})
        if hasattr(self.helper, "SIMULATOR_REVISIONS"):
            self.metadata["simulator_expected_revisions"] = self.helper.SIMULATOR_REVISIONS
            self.metadata["simulator_actual_revisions"] = getattr(self.helper, "SIMULATOR_ACTUAL_REVISIONS", None)
        self.env = self.helper.build_env(**build_arguments)
        self.integration_dt = float(self.env.env_config.simulation_config.timestep)
        if not math.isfinite(self.integration_dt) or not 0 < self.integration_dt <= self.options["control_dt"]:
            raise ValueError("Simulator integration timestep must be positive and no larger than control_dt")
        self.action_steps = max(1, round(self.options["control_dt"] / self.integration_dt))
        self.dt = self.action_steps * self.integration_dt
        self.metadata["control_dt"] = self.dt
        self.lidar = self.env.env_config.lidar_config
        self.angle_increment = (self.lidar.angle_max - self.lidar.angle_min) / (self.lidar.num_beams - 1)
        self.progress = RouteProgress(self.route, self.start)
        self.target = {"lap": self.progress.length, "straight": 16.0, "turn": 10.0}[self.options["case"]]
        self.elapsed = self.best_progress = self.last_progress_time = 0.0
        self.status = "paused"
        self.rate = self.options["rate"]
        self.plan_pose = np.asarray(self.helper.rear_pose(self.env), dtype=float)
        self.observation_time = 0.0
        self.result = self.module.PlanResult()
        self.planning_ms = self.cycle_ms = 0.0
        self.planning_history = deque(maxlen=250)
        self.stop_reason = None
        self.config_revision = 0
        self._update_scan()

    def _update_scan(self):
        state = self.env.unwrapped.sim.state
        self.grid.update(np.asarray(state.scans[0]), self.lidar.angle_min, self.angle_increment,
                         self.lidar.range_min, self.lidar.range_max)

    def apply(self, changes):
        """At a cycle boundary, replace config without moving/reseeding the car."""
        changes = validate_changes(changes)
        updated = replace(self.config, **changes)
        # All permitted fields leave grid geometry invariant. Keep its observations
        # and planner RNG; invalidate only the planner's cached path.
        self.config = updated
        self.planner.config = updated
        self.grid.config = updated
        self.planner.reset()
        self.result = self.module.PlanResult()
        self.options["config"].update(changes)
        self.config_revision += 1
        return asdict(self.config)

    def step(self):
        if self.status in TERMINAL:
            return
        self.plan_pose = np.asarray(self.helper.rear_pose(self.env), dtype=float)
        self.observation_time = self.elapsed
        started = time.perf_counter()
        self._update_scan()
        plan_started = time.perf_counter()
        self.result = self.planner.plan(self.grid, self.plan_pose.copy())
        self.planning_ms = (time.perf_counter() - plan_started) * 1000
        self.planning_history.append(self.planning_ms)
        speed, steering = float(self.result.speed), float(self.result.steering)
        if not np.all(np.isfinite([speed, steering])):
            raise ValueError("Planner returned a non-finite speed or steering command")
        for _ in range(self.action_steps):
            self.env.step(np.array([[steering, speed]]))
            self.elapsed += self.integration_dt
            # Collision is an observer-only terminal condition. It is never an
            # input to plan(), grid.update(), or a path-selection decision.
            if bool(self.env.unwrapped.sim.state.collisions[0]):
                self.status, self.stop_reason = "collision", "simulator_collision_observed"
                break
        pose = np.asarray(self.helper.rear_pose(self.env), dtype=float)
        distance = self.progress.update(pose)
        if self.status != "collision":
            if distance >= self.target:
                self.status, self.stop_reason = "complete", "reference_distance_reached"
            elif distance > self.best_progress + 0.05:
                self.best_progress, self.last_progress_time = distance, self.elapsed
            elif self.elapsed - self.last_progress_time >= 12.0:
                self.status, self.stop_reason = "no_progress", "no_reference_progress_for_12_seconds"
            if self.elapsed >= 180.0 and self.status not in TERMINAL:
                self.status, self.stop_reason = "no_progress", "180_second_simulation_limit"
        self.cycle_ms = (time.perf_counter() - started) * 1000

    def snapshot(self):
        pose = np.asarray(self.helper.rear_pose(self.env), dtype=float)
        speed, steering = float(self.result.speed), float(self.result.steering)
        arc, footprints = command_geometry(self.config, speed, steering, self.plan_pose)
        hits = np.asarray(self.grid.hits).reshape(-1, 2)
        clearance = None
        if len(hits):
            dx = np.maximum(np.maximum(-self.config.vehicle_rear - hits[:, 0],
                                       hits[:, 0] - self.config.vehicle_front), 0)
            dy = np.maximum(np.abs(hits[:, 1]) - self.config.vehicle_half_width, 0)
            clearance = float(np.min(np.hypot(dx, dy)))
        diagnostics = {
            "command_arc_source": "derived from returned command and configured stopping distance",
            "plan_pose": self.plan_pose.tolist(), "observation_time_s": self.observation_time,
            "sensor_age_s": self.elapsed - self.observation_time,
            "observed_body_clearance_m": clearance,
            "observed_clearance_m": clearance,
            "tracking_target": "unavailable from this planner API",
            "rejected_samples": "unavailable from this planner API",
            "planner_mode": "unavailable from this planner API",
            "stop_reason": self.stop_reason,
            "config_revision": self.config_revision,
            "planning_mean_ms": float(np.mean(self.planning_history)) if self.planning_history else 0.0,
            "planning_p95_ms": float(np.percentile(self.planning_history, 95)) if self.planning_history else 0.0,
            "route_deviation_note": "distance to reference; obstacle avoidance can intentionally increase it",
            "clearance_note": "nearest observed scan hit to body; unseen space is not certified free",
        }
        debug = getattr(self.result, "debug", None)
        if isinstance(debug, dict):
            diagnostics["planner_debug"] = debug
        state = self.env.unwrapped.sim.state
        return {
            "mode": "simulation", "status": self.status, "time_s": self.elapsed,
            "pose": pose.tolist(), "speed_command": speed,
            "actual_speed": float(state.standard_state[0, 3]), "steering": steering,
            "progress_m": self.progress.distance, "target_m": self.target,
            "cross_track_error": self.progress.cross_track,
            "planning_ms": self.planning_ms, "cycle_ms": self.cycle_ms,
            "path": local_to_world(self.result.path, self.plan_pose).tolist(),
            "tree": local_to_world(self.result.tree_edges, self.plan_pose).tolist(),
            "goal": None if self.result.goal is None else local_to_world([self.result.goal], self.plan_pose)[0].tolist(),
            "command_arc": arc, "footprints": footprints,
            "scan_hits": local_to_world(hits[::max(1, int(math.ceil(len(hits) / 1000)))], self.plan_pose).tolist(),
            "grid": display_grid(self.grid, self.plan_pose), "route": self.route.tolist(),
            "config": asdict(self.config), "metadata": self.metadata, "diagnostics": diagnostics,
            "map_yaml": self.map_yaml, "rate": self.rate,
        }

    def close(self):
        if self.env is not None:
            self.env.close()
            self.env = None
        for name in getattr(self, "modules", []):
            sys.modules.pop(name, None)


def _put_latest(output, message):
    """Never block a control cycle behind an unresponsive browser/server."""
    try:
        output.put_nowait(message)
    except queue.Full:
        try:
            output.get_nowait()
        except queue.Empty:
            pass
        try:
            output.put_nowait(message)
        except queue.Full:
            pass


def run_worker(commands, output, options):
    """multiprocessing 'spawn' target; commands and output are bounded queues.

    Commands: pause/resume/step/reset/apply/rate/stop. Apply uses ``config``;
    reset optionally uses ``options`` and ``config``. Ready state is paused.
    """
    # Helpers may import other files from the selected checkout. Respect the
    # read-only integration promise even for those ordinary Python imports.
    sys.dont_write_bytecode = True
    session = None
    current_options = dict(options)
    last_emit = 0.0
    deadline = time.perf_counter()

    def emit_snapshot(force=False):
        nonlocal last_emit
        now = time.perf_counter()
        if session is not None and (force or now - last_emit >= 0.1):
            _put_latest(output, {"type": "snapshot", "data": session.snapshot()})
            last_emit = now

    def create(new_options):
        nonlocal session, current_options, deadline
        checked_options = validate_options(new_options)
        _put_latest(output, {"type": "snapshot", "data": {"mode": "simulation", "status": "loading"}})
        if session is not None:
            session.close()
            session = None
        current_options = checked_options
        session = SimulationSession(current_options)
        deadline = time.perf_counter()
        _put_latest(output, {"type": "event", "data": {"event": "reset", "time_s": 0.0,
                                                        "metadata": session.metadata}})
        emit_snapshot(True)

    try:
        try:
            create(current_options)
        except Exception as exc:
            _put_latest(output, {"type": "error", "message": f"Cannot start simulation: {type(exc).__name__}: {exc}"})
        while True:
            command = None
            try:
                timeout = max(0.0, min(0.05, deadline - time.perf_counter())) if session and session.status == "running" else 0.1
                command = commands.get(timeout=timeout)
            except queue.Empty:
                pass
            if command is not None:
                try:
                    if not isinstance(command, dict):
                        raise ValueError("Command must be an object")
                    action = command.get("action")
                    if action == "stop":
                        return
                    if action == "reset":
                        new_options = dict(current_options)
                        new_options.update(command.get("options", {}))
                        # Accept top-level fields for CLI/adapters as well.
                        new_options.update({key: value for key, value in command.items()
                                            if key in {"repo", "helper", "waypoints", "map_yaml", "case", "seed", "noise", "control_dt", "rate"}})
                        if "config" in command:
                            new_options["config"] = command["config"]
                        create(new_options)
                    elif session is None:
                        raise ValueError("Simulation is unavailable; correct the settings and Reset")
                    elif action == "apply":
                        active = session.apply(command.get("config", {}))
                        current_options["config"] = dict(session.options["config"])
                        _put_latest(output, {"type": "event", "data": {"event": "config_applied", "time_s": session.elapsed,
                                                                       "config": active, "revision": session.config_revision}})
                        emit_snapshot(True)
                    elif action == "pause":
                        if session.status == "running":
                            session.status = "paused"
                        emit_snapshot(True)
                    elif action == "resume":
                        if session.status not in TERMINAL:
                            session.status = "running"
                            deadline = time.perf_counter()
                        emit_snapshot(True)
                    elif action == "step":
                        if session.status not in TERMINAL:
                            session.status = "paused"
                            session.step()
                        emit_snapshot(True)
                    elif action == "rate":
                        session.rate = _number(command.get("rate"), "rate", 0.25, 4.0)
                        current_options["rate"] = session.rate
                        deadline = time.perf_counter()
                        emit_snapshot(True)
                    else:
                        raise ValueError(f"Unknown command action: {action}")
                except Exception as exc:
                    _put_latest(output, {"type": "error", "message": f"Command failed: {type(exc).__name__}: {exc}"})
            if session and session.status == "running" and time.perf_counter() >= deadline:
                try:
                    session.step()
                    emit_snapshot(session.status in TERMINAL)
                    # Do not catch up accumulated lag with a burst of controls.
                    deadline = max(deadline + session.dt / session.rate, time.perf_counter())
                except Exception as exc:
                    session.status, session.stop_reason = "error", str(exc)
                    _put_latest(output, {"type": "error", "message": f"Simulation cycle failed: {type(exc).__name__}: {exc}"})
                    emit_snapshot(True)
    finally:
        if session is not None:
            session.close()
