"""Pinned headless F1TENTH integration shipped with RobertAris' dashboard.

The stock vehicle, LiDAR mounting, integration settings and rear-axle convention
match the official simulator configuration. No controller implementation is included.
Map loading uses three functions directly from the pinned official
f1tenth/f1tenth_gym_ros source, without importing its ROS node. Upstream code and
maps retain their original MIT license (Copyright 2020 Hongrui Zheng).
Run ``bash scripts/setup_simulator.sh`` from the dashboard checkout to install.
"""

import ast
import math
import os
import pathlib
import subprocess
import tempfile
import warnings

import numpy as np
import yaml
from PIL import Image

try:
    import f1tenth_gym
    from f1tenth_gym.envs import F110Env
    from f1tenth_gym.envs.action import LongitudinalActionType, SteerActionType
    from f1tenth_gym.envs.dynamic_models import DynamicModel, get_f1tenth_vehicle_parameters
    from f1tenth_gym.envs.env_config import (
        ControlConfig, EnvConfig, LoopCounterMode, ObservationConfig, ResetConfig,
        SimulationConfig,
    )
    from f1tenth_gym.envs.integrators import IntegratorType
    from f1tenth_gym.envs.lidar import LiDARConfig
    from f1tenth_gym.envs.observation import ObservationType
    from f1tenth_gym.envs.reset import ResetStrategy
    from f1tenth_gym.envs.track import Raceline, Track
    from f1tenth_gym.envs.track import track as gym_track
except ImportError as exc:
    raise RuntimeError(
        "The pinned F1TENTH simulator is not installed in this Python environment. "
        "Run `bash scripts/setup_simulator.sh` in the dashboard directory, then "
        "launch with `.venv/bin/racing-dashboard`. " + str(exc)
    ) from exc


GYM_REVISION = "ec3b1a4693ed20317e42e57a9454d94b824cec81"
BRIDGE_REVISION = "2c019810b4098ed82d44cad1d7d282a2dfc12421"
SIMULATOR_REVISIONS = {"f1tenth_gym": GYM_REVISION, "f1tenth_gym_ros": BRIDGE_REVISION}


def _simulator_root():
    configured = os.environ.get("RACING_SIM_ROOT") or os.environ.get("LAB6_SIM_ROOT")
    if configured:
        return pathlib.Path(configured).expanduser().resolve()
    legacy = pathlib.Path("/private/tmp/lab6")
    if pathlib.Path(str(legacy) + "_f1tenth_gym").is_dir() and pathlib.Path(str(legacy) + "_f1tenth_gym_ros/maps").is_dir():
        return legacy
    return pathlib.Path(tempfile.gettempdir()) / "robertaris-racing"


SIM_ROOT = _simulator_root()
GYM_ROOT = pathlib.Path(str(SIM_ROOT) + "_f1tenth_gym")
BRIDGE_ROOT = pathlib.Path(str(SIM_ROOT) + "_f1tenth_gym_ros")
MAPS = BRIDGE_ROOT / "maps"
BRIDGE = BRIDGE_ROOT / "f1tenth_gym_ros/gym_bridge.py"


def _check_source(directory, expected):
    try:
        actual = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(directory), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Pinned simulator source missing at {directory}; "
                           "run `bash scripts/setup_simulator.sh` or set RACING_SIM_ROOT") from exc
    if actual != expected or dirty:
        raise RuntimeError(f"Expected clean simulator revision {expected} at {directory}; "
                           "choose another RACING_SIM_ROOT and run scripts/setup_simulator.sh")
    return actual


SIMULATOR_ACTUAL_REVISIONS = {
    "f1tenth_gym": _check_source(GYM_ROOT, GYM_REVISION),
    "f1tenth_gym_ros": _check_source(BRIDGE_ROOT, BRIDGE_REVISION),
}
if pathlib.Path(f1tenth_gym.__file__).resolve().parent.parent != GYM_ROOT.resolve():
    raise RuntimeError("This Python loaded a different F1TENTH installation. Run "
                       "scripts/setup_simulator.sh and launch with .venv/bin/racing-dashboard")

# Reuse the official bridge's map interpretation, including reference-line and
# image-threshold handling. The source stays external and retains its license.
_IGNORED_MAP_KEY_WARNING = '.*ignoring unsupported map key .(centerline|raceline).'
_map_helper_names = {"_reference_lines_from_yaml", "_attach_yaml_reference_lines", "_load_track_from_yaml"}
_map_helpers = [node for node in ast.parse(BRIDGE.read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name in _map_helper_names]
if len(_map_helpers) != len(_map_helper_names):
    raise RuntimeError("Pinned bridge map helpers are unavailable; rerun scripts/setup_simulator.sh")
exec(compile(ast.Module(body=_map_helpers, type_ignores=[]), str(BRIDGE), "exec"), globals())


def build_env(track_name="levine_blocked", lidar=True, rear_start=(-12., 0., 0.), noise_std=.01, seed=7, map_yaml=None):
    """Build the official dynamic model, with 100 Hz physics and stock LiDAR."""
    track_path = pathlib.Path(map_yaml).expanduser().resolve() if map_yaml else MAPS / f"{track_name}.yaml"
    if not track_path.is_file():
        raise ValueError(f"Simulator map is missing: {track_path}; run scripts/setup_simulator.sh")
    track, _ = _load_track_from_yaml(track_path, 1.0)
    config = EnvConfig(
        map_name=track, map_scale=1.0, params=get_f1tenth_vehicle_parameters(), num_agents=1,
        control_config=ControlConfig(longitudinal_mode=LongitudinalActionType.SPEED,
                                     steering_mode=SteerActionType.STEERING_ANGLE),
        simulation_config=SimulationConfig(
            timestep=.01, integrator_timestep=.01, integrator=IntegratorType.RK4,
            dynamics_model=DynamicModel.ST, loop_counter=LoopCounterMode.FRENET_BASED,
            compute_frenet_frame=True, max_laps=None, count_partial_first_lap=False),
        observation_config=ObservationConfig(type=ObservationType.DEFAULT),
        reset_config=ResetConfig(strategy=ResetStrategy.MAP_RANDOM_STATIC),
        lidar_config=LiDARConfig(
            enabled=lidar, num_beams=819, range_min=.05, range_max=25.,
            angle_min=math.radians(-135), angle_max=math.radians(135), noise_std=noise_std,
            base_link_to_lidar_tf=(.275, 0., 0.)),
        render_enabled=False,
    )
    env = F110Env(config=config, render_mode=None)
    try:
        reset_rear_pose(env, rear_start, seed)
    except Exception:
        env.close()
        raise
    return env


def reset_rear_pose(env, rear_start, seed=None):
    rear = np.asarray(rear_start, dtype=float)
    center = rear.copy()
    center[:2] += env.vehicle_params.lr * np.array([math.cos(rear[2]), math.sin(rear[2])])
    return env.reset(seed=seed, options={"poses": center[None, :]})


def rear_pose(env):
    pose = np.asarray(env.unwrapped.sim.state.poses[0]).copy()
    pose[:2] -= env.vehicle_params.lr * np.array([math.cos(pose[2]), math.sin(pose[2])])
    return pose
