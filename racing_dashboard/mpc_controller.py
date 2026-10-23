"""Run the selected Lab 7 MPC's unchanged classes against a small ROS facade.

All imported controller files and Numba caches live in a temporary worker-owned
directory. The selected checkout is read, never imported in place or modified.
"""
from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import uuid

import numpy as np


def _read(path, limit=8 * 1024 * 1024):
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"Missing or oversized MPC input: {path}")
    return path.read_bytes()


def _git(repo, *arguments, required=True):
    result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(repo), *arguments],
                            capture_output=True, timeout=5, check=False)
    if result.returncode:
        if required:
            raise ValueError("Cannot read the selected MPC Git revision")
        return b""
    if len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError("MPC Git input exceeds the size limit")
    return result.stdout


class ControllerSource:
    def __init__(self, repo, track, ref="", waypoints=None):
        self.repo = Path(repo).expanduser().resolve()
        self.temp = tempfile.TemporaryDirectory(prefix="racing-mpc-")
        self.root = Path(self.temp.name)
        self.track = track
        self.modules = []
        if track not in {"levine", "spielberg"}:
            self.close()
            raise ValueError("track must be levine or spielberg")
        if not self.repo.is_dir():
            self.close()
            raise ValueError("Select a local Lab 7 MPC repository")
        try:
            if not isinstance(ref, str) or (ref and (not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", ref) or ref.startswith("-"))):
                raise ValueError("Select a valid MPC branch, tag or commit")
            commit = (_git(self.repo, "rev-parse", "--verify", ref + "^{commit}").decode().strip() if ref
                      else _git(self.repo, "rev-parse", "HEAD", required=False).decode().strip() or None)
            names = ["mpc/scripts/mpc_node.py", "mpc/scripts/utils.py",
                     f"mpc/launch/{track}_launch.py", f"mpc/waypoints/{track}.csv"]
            self.files = {}
            for relative in names:
                data = _git(self.repo, "show", f"{commit}:{relative}") if ref else _read(self.repo / relative)
                destination = self.root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                self.files[relative] = data
            self.waypoint_path = self.root / names[-1]
            self.waypoint_original = str(self.repo / names[-1])
            if waypoints:
                original = Path(waypoints).expanduser().resolve()
                self.waypoint_path = self.root / "custom-waypoints.csv"
                self.waypoint_path.write_bytes(_read(original))
                self.waypoint_original = str(original)
            launch = ast.parse(self.files[names[2]].decode(), filename=str(self.repo / names[2]))
            self.parameters = {}
            for node in launch.body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PARAMETERS" for t in node.targets):
                    try:
                        self.parameters = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        raise ValueError("MPC launch PARAMETERS must be a literal dictionary") from None
            if not isinstance(self.parameters, dict):
                raise ValueError("MPC launch PARAMETERS must be a literal dictionary")
            self.metadata = {
                "repo": str(self.repo), "branch": ref or _git(self.repo, "branch", "--show-current", required=False).decode().strip() or "detached/unknown",
                "commit": commit, "source_ref": ref, "source_mode": "git_ref" if ref else "working_tree",
                "dirty": False if ref else bool(_git(self.repo, "status", "--porcelain", required=False)),
                "planner_file": str(self.repo / names[0]), "waypoints_file": self.waypoint_original,
                "planner_sha256": hashlib.sha256(self.files[names[0]]).hexdigest(),
                "utils_sha256": hashlib.sha256(self.files[names[1]]).hexdigest(),
                "launch_sha256": hashlib.sha256(self.files[names[2]]).hexdigest(),
                "waypoints_sha256": hashlib.sha256(self.waypoint_path.read_bytes()).hexdigest(),
                "controller": "Lab 7 MPC", "controller_execution": "unchanged source classes and odom_callback with ROS facade",
            }
        except Exception:
            self.close()
            raise

    def create(self, overrides):
        try:
            import cvxpy
            from scipy.sparse import block_diag, csc_matrix, diags
            import numba
        except ImportError as error:
            raise ValueError("MPC simulation dependencies are missing. Install with pip install -e '.[mpc]' or run scripts/setup_simulator.sh") from error
        # Numba's source decorators use cache=True; never write their cache into
        # the source checkout or share it with a different selected revision.
        numba.config.CACHE_DIR = str(self.root / "numba-cache")
        utility_name = "_racing_mpc_utils_" + uuid.uuid4().hex
        utility = types.ModuleType(utility_name)
        utility.__file__ = str(self.root / "mpc/scripts/utils.py")
        sys.modules[utility_name] = utility
        self.modules.append(utility_name)
        exec(compile(self.files["mpc/scripts/utils.py"], utility.__file__, "exec"), utility.__dict__)
        clock = types.SimpleNamespace(seconds=0.0)
        parameters = {**self.parameters, **overrides, "track": self.track,
                      "waypoints_file": str(self.waypoint_path)}
        node_class = _node_facade(parameters, clock)
        name = "_racing_mpc_source_" + uuid.uuid4().hex
        module = types.ModuleType(name)
        module.__file__ = str(self.root / "mpc/scripts/mpc_node.py")
        module.__dict__.update({"math": math, "os": os, "dataclass": dataclass, "field": field,
            "np": np, "cvxpy": cvxpy, "block_diag": block_diag, "csc_matrix": csc_matrix, "diags": diags,
            "Node": node_class, "AckermannDrive": _Drive, "AckermannDriveStamped": _DriveStamped,
            "PoseStamped": _PoseStamped, "Path": _Path, "Odometry": _Odometry,
            "qos_profile_sensor_data": object(),
            "get_package_share_directory": lambda package: str(self.root / "mpc"),
            "nearest_point": utility.nearest_point,
            "calc_interpolated_ref_trajectory": utility.calc_interpolated_ref_trajectory})
        parsed = ast.parse(self.files["mpc/scripts/mpc_node.py"], filename=module.__file__)
        selected = [node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name in {"mpc_config", "State", "MPC"}]
        if {node.name for node in selected} != {"mpc_config", "State", "MPC"}:
            raise ValueError("The selected source does not expose the supported Lab 7 MPC classes")
        # No method rewriting: constructor, reference interpolation, optimization,
        # yaw handling, failure stop, command clipping, and publishing stay intact.
        sys.modules[name] = module
        self.modules.append(name)
        exec(compile(ast.Module(body=selected, type_ignores=[]), module.__file__, "exec"), module.__dict__)
        try:
            node = module.MPC()
        except Exception as error:
            raise ValueError(f"Cannot initialize selected MPC source: {type(error).__name__}: {error}") from error
        for member in ("odom_callback", "config", "waypoints", "drive_pub", "ref_pub", "pred_pub"):
            if not hasattr(node, member):
                raise ValueError("The selected MPC source has an unsupported interface: " + member)
        return MPCController(node, clock)

    def close(self):
        for name in self.modules:
            sys.modules.pop(name, None)
        self.modules.clear()
        self.temp.cleanup()


def _header():
    return types.SimpleNamespace(stamp=None, frame_id="")


def _pose():
    return types.SimpleNamespace(position=types.SimpleNamespace(x=0.0, y=0.0, z=0.0),
        orientation=types.SimpleNamespace(x=0.0, y=0.0, z=0.0, w=0.0))


class _Drive:
    def __init__(self):
        self.speed = self.steering_angle = self.acceleration = self.steering_angle_velocity = self.jerk = 0.0


class _DriveStamped:
    def __init__(self):
        self.header, self.drive = _header(), _Drive()


class _PoseStamped:
    def __init__(self):
        self.header, self.pose = _header(), _pose()


class _Path:
    def __init__(self):
        self.header, self.poses = _header(), []


class _Odometry:
    def __init__(self):
        self.header = _header()
        self.pose = types.SimpleNamespace(pose=_pose())
        self.twist = types.SimpleNamespace(twist=types.SimpleNamespace(
            linear=types.SimpleNamespace(x=0.0, y=0.0, z=0.0), angular=types.SimpleNamespace(x=0.0, y=0.0, z=0.0)))


class _Publisher:
    def __init__(self):
        self.last = None

    def publish(self, message):
        self.last = message


def _node_facade(overrides, clock):
    class Node:
        def __init__(self, name):
            self._parameters = {}

        def declare_parameter(self, name, default):
            parameter = types.SimpleNamespace(value=overrides.get(name, default))
            self._parameters[name] = parameter
            return parameter

        def get_parameter(self, name):
            return self._parameters[name]

        def create_subscription(self, *arguments):
            return None

        def create_publisher(self, *arguments):
            return _Publisher()

        def get_clock(self):
            return types.SimpleNamespace(now=lambda: types.SimpleNamespace(to_msg=lambda: types.SimpleNamespace(
                sec=int(clock.seconds), nanosec=int((clock.seconds % 1) * 1e9))))

        def get_logger(self):
            return types.SimpleNamespace(info=lambda *args: None, warn=lambda *args: None, error=lambda *args: None)

        def destroy_node(self):
            pass
    return Node


class MPCController:
    def __init__(self, node, clock):
        self.node, self.clock = node, clock
        self.reference, self.prediction = [], []
        self.speed = self.steering = 0.0

    def step(self, pose, speed, beta, stamp):
        message = _Odometry()
        message.header.frame_id = "map"
        message.pose.pose.position.x, message.pose.pose.position.y = map(float, pose[:2])
        message.pose.pose.orientation.z = math.sin(pose[2] / 2)
        message.pose.pose.orientation.w = math.cos(pose[2] / 2)
        message.twist.twist.linear.x = float(speed * math.cos(beta))
        message.twist.twist.linear.y = float(speed * math.sin(beta))
        self.clock.seconds = stamp
        self.node.drive_pub.last = self.node.ref_pub.last = self.node.pred_pub.last = None
        self.node.odom_callback(message)
        drive = self.node.drive_pub.last
        if drive is None:
            raise ValueError("The selected MPC callback did not publish a drive command")
        self.speed, self.steering = float(drive.drive.speed), float(drive.drive.steering_angle)
        if not np.all(np.isfinite([self.speed, self.steering])):
            raise ValueError("The selected MPC published a non-finite command")
        def path(publisher):
            message = publisher.last
            return [[float(p.pose.position.x), float(p.pose.position.y)] for p in message.poses] if message else []
        self.reference, self.prediction = path(self.node.ref_pub), path(self.node.pred_pub)
        return self.speed, self.steering

    def configuration(self):
        config = asdict(self.node.config)
        for name, value in list(config.items()):
            if isinstance(value, np.ndarray):
                config[name] = value.tolist()
        for name in ("max_accel", "profile_accel", "profile_decel", "pose_offset"):
            config[name] = getattr(self.node, name, None)
        return config

    def diagnostics(self):
        problem = getattr(self.node, "MPC_prob", None)
        control_available = self.node.oa is not None and self.node.odelta_v is not None
        # The native solver exception path can leave the previous CVXPY status
        # and stats in place. A stop command must never inherit an old "optimal".
        stats = getattr(problem, "solver_stats", None) if control_available else None
        return {"solver_status": getattr(problem, "status", None) if control_available else None,
                "solver_time_ms": float(stats.solve_time * 1000) if stats and stats.solve_time is not None else None,
                "solver_iterations": int(stats.num_iters) if stats and stats.num_iters is not None else None,
                "control_available": control_available}
