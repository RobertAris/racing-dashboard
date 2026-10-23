"""Isolated, read-only adapter for a Lab 7 MPC and the F1TENTH simulator.

The process target imports simulator dependencies only after startup. Every reset
compiles the selected checkout's sources again; nothing is copied into the tool
and the MPC is never given simulator collision/lap information.
"""

from __future__ import annotations

from collections import deque
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
import yaml
from PIL import Image

from .mpc_controller import ControllerSource


# Modest dashboard ranges, in SI units. These also protect against accidental
# extreme commands; the source planner's own configuration validation still runs.
TUNABLES = {
    "MAX_SPEED": (0.1, 15.0), "max_accel": (0.1, 3.0),
    "profile_accel": (0.1, 3.0), "profile_decel": (0.1, 3.0),
}
STARTS = {
    "levine": (-12.0, 0.0, 0.0), "spielberg": (14.59, 3.92, -2.877),
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
        raise ValueError("Select a Lab 7 MPC repository using --repo or the repository field")
    value["repo"] = str(Path(value["repo"]).expanduser().resolve())
    value["helper"] = str(Path(value.get("helper") or Path(__file__).with_name("f1tenth_helper.py")).expanduser().resolve())
    if value.get("case", "lap") != "lap":
        raise ValueError("MPC simulation supports the lap scenario")
    value["case"] = value.get("case", "lap")
    if value.get("track", "levine") not in STARTS:
        raise ValueError("track must be levine or spielberg")
    value["track"] = value.get("track", "levine")
    if not isinstance(value.get("ref", ""), str):
        raise ValueError("ref must be a Git revision or empty for the working tree")
    value["ref"] = value.get("ref", "")
    seed = value.get("seed", 7)
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer between 0 and 4294967295")
    value["seed"] = seed
    value["noise"] = _number(value.get("noise", 0.01), "noise", 0.0, 1.0)
    value["control_dt"] = _number(value.get("control_dt", 0.1), "control_dt", 0.01, 0.5)
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


def command_geometry(config, speed, steering, pose):
    """A display-only constant-command arc over the MPC model horizon."""
    curvature = math.tan(steering) / config["wheelbase"]
    distance = abs(speed) * config["DTK"] * config["TK"]
    arc = np.linspace(0, distance, max(2, min(120, int(distance / 0.025) + 1)))
    headings = arc * curvature
    if abs(curvature) < 1e-8:
        points = np.column_stack((arc, np.zeros(len(arc))))
    else:
        points = np.column_stack((np.sin(headings) / curvature, (1 - np.cos(headings)) / curvature))
    rectangle = np.array([[config["vehicle_front"], config["vehicle_half_width"]],
                          [config["vehicle_front"], -config["vehicle_half_width"]],
                          [-config["vehicle_rear"], -config["vehicle_half_width"]],
                          [-config["vehicle_rear"], config["vehicle_half_width"]],
                          [config["vehicle_front"], config["vehicle_half_width"]]])
    footprints = []
    for index in np.unique(np.linspace(0, len(points) - 1, min(6, len(points))).astype(int)):
        local = local_to_world(rectangle, [*points[index], headings[index]])
        footprints.append(local_to_world(local, pose).tolist())
    return local_to_world(points, pose).tolist(), footprints


def validate_map_route(map_yaml, route, start):
    """Reject mismatched map frames before creating an expensive simulator."""
    path = Path(map_yaml)
    if not path.is_file():
        raise ValueError(f"The selected MPC track map is missing: {path}; run scripts/setup_simulator.sh")
    try:
        description = yaml.safe_load(path.read_text())
        image_path = Path(description["image"])
        if not image_path.is_absolute():
            image_path = path.parent / image_path
        with Image.open(image_path) as image:
            pixels = np.asarray(image.convert("L"))
        resolution = float(description["resolution"])
        origin = np.asarray(description["origin"], dtype=float)
        if resolution <= 0 or not math.isfinite(resolution) or origin.shape != (3,) or not np.isfinite(origin).all():
            raise ValueError("Invalid map coordinates")
        def known_free(points):
            delta = np.asarray(points) - origin[:2]
            c, sine = math.cos(origin[2]), math.sin(origin[2])
            local = delta @ np.array([[c, -sine], [sine, c]])
            columns = np.floor(local[:, 0] / resolution).astype(int)
            rows = pixels.shape[0] - 1 - np.floor(local[:, 1] / resolution).astype(int)
            inside = (columns >= 0) & (columns < pixels.shape[1]) & (rows >= 0) & (rows < pixels.shape[0])
            free = np.zeros(len(points), dtype=bool)
            values = pixels[rows[inside], columns[inside]] / 255.0
            occupancy = values if description.get("negate", 0) else 1.0 - values
            free[inside] = occupancy < float(description.get("free_thresh", 0.196))
            return inside, free
        inside, free = known_free(route)
        if inside.mean() < .99 or free.mean() < .8:
            raise ValueError("The selected waypoint route does not match this map's free space and coordinate frame")
        _, start_free = known_free(np.asarray([start[:2]]))
        if not bool(start_free[0]):
            raise ValueError("The selected start pose is not in known free map space")
    except (OSError, KeyError, TypeError) as error:
        raise ValueError(f"Cannot verify MPC map and route: {error}") from error
    return hashlib.sha256(path.read_bytes() + image_path.read_bytes()).hexdigest()


class SimulationSession:
    """Actual Lab 7 MPC callbacks and F1TENTH physics, isolated in one worker."""

    def __init__(self, options):
        self.env = self.source = None
        self.modules = []
        try:
            self._initialize(options)
        except Exception:
            self.close()
            raise

    def _initialize(self, options):
        self.options = validate_options(options)
        self.source = ControllerSource(self.options["repo"], self.options["track"], self.options["ref"],
                                       self.options.get("waypoints"))
        self.controller = self.source.create(self.options["config"])
        self.route = np.asarray(self.controller.node.waypoints[:, :2], dtype=float)
        self.helper = load_source(self.options["helper"], "helper")
        self.modules.append(self.helper.__name__)
        for name in ("build_env", "rear_pose"):
            if not callable(getattr(self.helper, name, None)):
                raise ValueError(f"Simulator helper must provide {name}()")
        requested_map = self.options.get("map_yaml")
        maps = getattr(self.helper, "MAPS", None)
        filename = "levine_blocked.yaml" if self.options["track"] == "levine" else "Spielberg.yaml"
        stock_path = Path(maps).resolve() / filename if maps is not None else None
        if requested_map:
            map_path = Path(requested_map).expanduser().resolve()
        else:
            if stock_path is None:
                raise ValueError("Simulator helper must expose MAPS or select an explicit map YAML")
            map_path = stock_path
        self.map_yaml = str(map_path)
        self.start = STARTS[self.options["track"]]
        if requested_map and map_path != stock_path:
            # Custom map/route pairs use their explicit route's first rear pose.
            first = self.controller.node.waypoints[0]
            self.start = (float(first[0]), float(first[1]), float(first[2]))
        map_hash = validate_map_route(map_path, self.route, self.start)
        self.metadata = {**self.source.metadata,
            "helper_file": self.options["helper"], "helper_sha256": self.helper.__source_sha256__,
            "model": "F1TENTH gym dynamics; actual Lab 7 MPC with ROS facade",
            "track": self.options["track"], "case": "lap", "seed": self.options["seed"],
            "noise": self.options["noise"], "map_yaml": self.map_yaml, "map_hash": map_hash,
            "pose_reference": "rear axle / base_link; simulator CoG converted using vehicle lr",
            "speed_source": "simulator state speed; odometry components retain slip angle",
            "pose_offset": self.controller.node.pose_offset,
        }
        if hasattr(self.helper, "SIMULATOR_REVISIONS"):
            self.metadata["simulator_expected_revisions"] = self.helper.SIMULATOR_REVISIONS
            self.metadata["simulator_actual_revisions"] = getattr(self.helper, "SIMULATOR_ACTUAL_REVISIONS", None)
        self.env = self.helper.build_env(map_yaml=self.map_yaml, rear_start=self.start,
                                        noise_std=self.options["noise"], seed=self.options["seed"])
        self.integration_dt = float(self.env.env_config.simulation_config.timestep)
        if not math.isfinite(self.integration_dt) or not 0 < self.integration_dt <= self.options["control_dt"]:
            raise ValueError("Simulator integration timestep must be positive and no larger than control_dt")
        self.action_steps = max(1, round(self.options["control_dt"] / self.integration_dt))
        self.dt = self.action_steps * self.integration_dt
        self.metadata["control_dt"] = self.dt
        self.metadata["model_dt"] = float(self.controller.node.config.DTK)
        self.lidar = self.env.env_config.lidar_config
        self.progress = RouteProgress(self.route, self.start)
        self.target = self.progress.length
        self.elapsed = self.best_progress = self.last_progress_time = 0.0
        self.status, self.stop_reason = "paused", None
        self.rate = self.options["rate"]
        self.plan_pose = np.asarray(self.helper.rear_pose(self.env), dtype=float)
        self.observation_time = 0.0
        self.planning_ms = self.cycle_ms = 0.0
        self.planning_history = deque(maxlen=250)
        self.config_revision = 0
        self._configuration()

    def _configuration(self):
        config = self.controller.configuration()
        native = self.controller.node.config
        params = getattr(self.env, "vehicle_params", None)
        length, width = float(getattr(params, "length", native.LENGTH)), float(getattr(params, "width", native.WIDTH))
        lr = float(getattr(params, "lr", 0.17145))
        config.update(wheelbase=float(native.WB), vehicle_front=length / 2 + lr,
                      vehicle_rear=max(0.0, length / 2 - lr), vehicle_half_width=width / 2)
        self.config = config
        self.metadata["effective_configuration"] = config
        return config

    def apply(self, changes):
        """Rebuild the native MPC at a cycle boundary; keep physics and car pose."""
        changes = validate_changes(changes)
        overrides = {**self.options["config"], **changes}
        replacement = self.source.create(overrides)
        self.controller = replacement
        self.options["config"] = overrides
        self.config_revision += 1
        self.metadata["pose_offset"] = self.controller.node.pose_offset
        return self._configuration()

    def step(self):
        if self.status in TERMINAL:
            return
        self.plan_pose = np.asarray(self.helper.rear_pose(self.env), dtype=float)
        self.observation_time = self.elapsed
        state = self.env.unwrapped.sim.state
        speed = float(state.standard_state[0, 3])
        beta = float(state.standard_state[0, 6]) if state.standard_state.shape[1] > 6 else 0.0
        started = time.perf_counter()
        speed, steering = self.controller.step(self.plan_pose, speed, beta, self.elapsed)
        self.planning_ms = (time.perf_counter() - started) * 1000
        self.planning_history.append(self.planning_ms)
        for _ in range(self.action_steps):
            self.env.step(np.array([[steering, speed]]))
            self.elapsed += self.integration_dt
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
            if self.elapsed >= 300.0 and self.status not in TERMINAL:
                self.status, self.stop_reason = "no_progress", "300_second_simulation_limit"
        self.cycle_ms = (time.perf_counter() - started) * 1000

    def snapshot(self):
        pose = np.asarray(self.helper.rear_pose(self.env), dtype=float)
        speed, steering = self.controller.speed, self.controller.steering
        arc, footprints = command_geometry(self.config, speed, steering, self.plan_pose)
        state = self.env.unwrapped.sim.state
        scans = np.asarray(state.scans[0]) if getattr(self.lidar, "enabled", True) else np.empty(0)
        angles = np.linspace(self.lidar.angle_min, self.lidar.angle_max, len(scans))
        valid = np.isfinite(scans) & (scans >= self.lidar.range_min) & (scans < self.lidar.range_max)
        scans, angles = scans[valid], angles[valid]
        lidar_offset = getattr(self.lidar, "base_link_to_lidar_tf", (.275, 0.0, 0.0))
        angles = angles + float(lidar_offset[2])
        hits = np.column_stack((scans * np.cos(angles) + lidar_offset[0], scans * np.sin(angles) + lidar_offset[1]))
        diagnostics = {**self.controller.diagnostics(),
            "command_arc_source": "derived constant command over the MPC model horizon; not the optimized prediction",
            "plan_pose": self.plan_pose.tolist(), "observation_time_s": self.observation_time,
            "sensor_age_s": self.elapsed - self.observation_time, "stop_reason": self.stop_reason,
            "config_revision": self.config_revision,
            "planning_mean_ms": float(np.mean(self.planning_history)) if self.planning_history else 0.0,
            "planning_p95_ms": float(np.percentile(self.planning_history, 95)) if self.planning_history else 0.0,
            "route_deviation_note": "distance to full reference route; no obstacle avoidance is added to the source MPC",
            "limitations": ["ROS transport, callback backlog and localization errors are not simulated.",
                            "Simulator collision is an observer-only terminal condition.",
                            "Apply reconstructs the MPC and clears warm starts while preserving car state.",
                            "Model DTK and callback period are distinct; both are recorded."],
        }
        return {"mode": "simulation", "status": self.status, "time_s": self.elapsed,
            "pose": pose.tolist(), "speed_command": speed, "actual_speed": float(state.standard_state[0, 3]),
            "steering": steering, "progress_m": self.progress.distance, "target_m": self.target,
            "cross_track_error": self.progress.cross_track, "planning_ms": self.planning_ms, "cycle_ms": self.cycle_ms,
            "path": self.controller.prediction, "reference": self.controller.reference,
            "command_arc": arc, "footprints": footprints,
            "scan_hits": local_to_world(hits[::max(1, int(math.ceil(len(hits) / 1000)))], pose).tolist(),
            "route": self.route.tolist(), "config": self.config, "metadata": self.metadata,
            "diagnostics": diagnostics, "map_yaml": self.map_yaml, "rate": self.rate}

    def close(self):
        if self.env is not None:
            self.env.close()
            self.env = None
        if self.source is not None:
            self.source.close()
            self.source = None
        for name in self.modules:
            sys.modules.pop(name, None)
        self.modules.clear()


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
                                            if key in {"repo", "helper", "waypoints", "map_yaml", "case", "track", "ref", "seed", "noise", "control_dt", "rate"}})
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
