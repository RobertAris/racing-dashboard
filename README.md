# RobertAris Racing Lab

A local browser dashboard for Lab 6 motion planning: watch the reference route,
RRT tree, selected path and driven trace; tune the real planner in simulation;
monitor the car; and replay a recorded run.

**Author: RobertAris.** This is a separate tools repository. It reads the Lab 6
checkout you select and does not edit it. Switching Lab 6 branches leaves this
tool available. Simulation reloads the selected branch on **Reset** or a new run.

## Start on your MacBook

Keep the dashboard and Lab 6 as sibling checkouts:

```text
~/robertaris-racing-dashboard/
~/ese-6150-lab-6-motion-planning-group-3/
```

From the dashboard directory, install the dashboard and its pinned official
F1TENTH simulator:

```bash
cd ~/robertaris-racing-dashboard
bash scripts/setup_simulator.sh
.venv/bin/racing-dashboard \
  --repo ~/ese-6150-lab-6-motion-planning-group-3
```

Open **http://127.0.0.1:8765** in your browser and click **Start simulation**.
The first start may take a minute while simulator kernels compile. The interface
remains available while it loads. Nothing needs to be installed in your Lab 6
checkout, and ROS is not required for this direct simulation.

Setup needs Git and either [uv](https://docs.astral.sh/uv/getting-started/installation/)
(recommended) or an installed Python 3.12+. `uv` can supply Python 3.13. The
dashboard itself supports Python 3.10+; the pinned simulator needs Python 3.12+.
Downloads and first installation may take several minutes.

The script creates this tool's `.venv` and keeps upstream simulator clones
outside the repository. It reuses the existing `/private/tmp/lab6` simulator
prefix when available. To choose a persistent location instead of temporary
storage, use the same prefix for setup and launch:

```bash
RACING_SIM_ROOT="$HOME/racing-sim/cache" bash scripts/setup_simulator.sh
RACING_SIM_ROOT="$HOME/racing-sim/cache" .venv/bin/racing-dashboard \
  --repo ~/ese-6150-lab-6-motion-planning-group-3
```

The prefix produces `cache_f1tenth_gym` and `cache_f1tenth_gym_ros` directories.
`LAB6_SIM_ROOT` is also supported. Keep those pinned clones clean; setup rejects
unexpected revisions instead of overwriting them. The setup script prints the
exact launch command for your selected location.

If your existing Lab 6 Python environment already has the pinned simulator, you
can use it directly from this directory:

```bash
/private/tmp/lab6_sim_venv/bin/python -m racing_dashboard \
  --repo ~/ese-6150-lab-6-motion-planning-group-3
```

## Use it to tune Lab 6

1. Choose **Simulation** and connect your Lab 6 checkout. Optional paths let you
   select another XY waypoint CSV, compatible simulator helper, or simulator map.
2. Start with **Track lap**, **Straight**, or **Turn**. The starting poses match
   the Lab 6 README; the standalone turn uses an approximate 10 m route target,
   not the grader's exact finish condition.
3. Watch the map. **Fit view**, **Follow car**, drag-to-pan, and wheel-to-zoom
   help inspect narrow sections. Toggle the tree, scan, grid, and body overlays.
4. Change maximum speed or tracking lookahead. **Apply live** updates the
   simulation at a planning-cycle boundary. The active values and revision show
   when the worker has applied them; amber chart marks identify changes.
5. Use **Apply & restart** to compare settings from the same start and seed.
   **Pause** and **Step** inspect one planning cycle at a time. Reset creates a
   fresh planner, RNG and simulator state and reloads the source from your branch.
6. Save a **baseline** to retain its ghost trace and compare completion, elapsed
   time, route deviation and planning p95. Save named presets for later use.
7. **Download recording** before switching modes or resetting. Load that JSON,
   or a Lab 6 runner CSV, in **Replay** to play, scrub, or step through it.

Advanced controls expose `goal_lookahead`, `goal_bias`, `max_expansion_dist`, and
`planning_budget`. Geometry and collision margins remain the selected branch's
settings. Only six validated planning controls are exposed; changing them never
modifies the branch's defaults on disk.

The charts show commanded/estimated speed, steering, geometric distance to the
reference route, and planner/full simulation cycle time. Route deviation can be
intentional obstacle avoidance. A useful setting completes the section safely
and consistently; low route deviation by itself is not the objective.

## Use it with another branch

Pause, switch branches in the Lab 6 checkout, then click **Reset**. The new run
records the branch, commit, dirty state, planner and waypoint hashes, full
effective configuration, and simulator revisions. A running simulation keeps its
already-loaded source until reset. The dashboard never switches branches for you.

For simultaneous comparisons, use separate Lab 6 worktrees and separate dashboard
ports, pointing each instance at its corresponding checkout:

```bash
.venv/bin/racing-dashboard --repo /path/to/lab6-worktree-A --port 8765
.venv/bin/racing-dashboard --repo /path/to/lab6-worktree-B --port 8766
```

Direct simulation currently expects the selected branch to export
`PlannerConfig`, `LocalGrid`, `LocalPlanner`, and `PlanResult` in
`lab7_pkg/lab7_pkg/planner.py`, with the current Lab 6 `plan(grid, pose)` API.
Branches using a different implementation can still use **Live car** to monitor
their ROS topics. An incompatible simulation branch produces an explicit error;
the dashboard does not silently run a different planner.

The default waypoint path is
`lab7_pkg/waypoints/levine_centerline.csv` in the selected checkout. The bundled
simulator helper is part of this tools repo, so it remains available across branch
changes. `--helper /absolute/path/to/helper.py` overrides it when needed.

## Connect to the real car

Use your normal saved-map localization and Lab 6 launch. Run this tool's server
and ROS adapter in the existing ROS environment, then view the browser from the
MacBook. The adapter only monitors; it does not publish `/drive` or write live
car parameters.

On the ROS host, install the small dashboard package in a suitable Python
environment (the simulator is optional for monitoring):

```bash
cd ~/robertaris-racing-dashboard
python3 -m venv .venv-monitor
.venv-monitor/bin/python -m pip install -e .
.venv-monitor/bin/racing-dashboard --mode live \
  --map /absolute/path/to/levine_2nd.yaml
```

In another terminal on the ROS host, source your ROS installation and workspace,
then use their Python interpreter to run the adapter from this repo:

```bash
source /opt/ros/humble/setup.bash
source ~/f1tenth_ws/install/setup.bash
cd ~/robertaris-racing-dashboard
python3 -m racing_dashboard.ros_adapter \
  --pose-topic /pf/pose/odom --pose-type odometry \
  --route-file /absolute/path/to/real_car_waypoints.csv
```

Replace the ROS distro/workspace paths with your actual setup. `/pf/pose/odom`
is the default in your Lab 5 waypoint logger. Confirm the topic type with
`ros2 topic info /pf/pose/odom`; alternate pose types and topic names are supported.
For the ROS simulator, use `--pose-topic /ego_racecar/odom --pose-type odometry`.

On the MacBook, forward the local web port:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@car
```

Open **http://127.0.0.1:8765**, select **Live car**, and check map/pose/waypoint
alignment. Use the saved map used for localization and its matching real-car
waypoints. The simulator route is not automatically aligned to your saved map.
Map YAML/image paths refer to files on the **server host**.

See [docs/LIVE.md](docs/LIVE.md) for topic types, stamped TF transforms, missing
topics, localization jumps, simulated time, and connection troubleshooting.
Native ROS transport has not been tested on the MacBook; conversion and freshness
logic have automated coverage.

## Export tuned settings to Lab 6

Click **Export ROS parameters** to download the six pending controls as a ROS 2
YAML file. Review the values for your car, copy the file to the ROS host, and load
it at planner startup. For the current node name (`rrt_node`):

```bash
ros2 run lab7_pkg rrt_node.py --ros-args \
  --params-file /absolute/path/to/robertaris-rrt-preset.yaml \
  -p pose_topic:=/pf/pose/odom \
  -p waypoints_file:=/absolute/path/to/real_car_waypoints.csv
```

Use your normal controller/launch workflow when applying a preset; run only the
intended controller. If a branch uses another node name or parameter names, adapt
the YAML to that branch. The current Lab 6 node reads these settings at startup;
setting ROS parameters while it runs does not update its planner configuration.

## Recording and replay

Recordings contain available map/pose/path/tree/grid/scan telemetry, source
metadata, configuration changes and commands. Grids are losslessly compressed in
the JSON. Data stays on the server until **Download recording**, then saves to
your browser's download folder. No telemetry goes to a hosted service.

Recordings are bounded to 12000 frames or approximately 64 MiB of packed frame
data; the oldest frames drop when the limit is reached, and the UI reports it.
They are sampled around 10 Hz. Replay is a visualization of captured output;
the simulator itself continues planning around 25 Hz and physics around 100 Hz.
The displayed grid is conservatively pooled for bandwidth; planning uses the
original full-resolution grid. Presets persist in
`~/.local/share/robertaris-racing-dashboard/presets/` on the server. Override with
`--data-dir /path/to/local-data`.

Existing logs from `lab7_pkg/test/run_simulator.py --log run.csv` also import.
CSV replay shows the fields that were actually logged; it cannot recover scans,
trees, selected paths or timing absent from the CSV.

## What the current planner exposes

- Accepted RRT branches, selected path, reference route, goal and occupancy grid.
- A **derived command arc** and vehicle body outlines in direct simulation,
  computed from the returned command and branch configuration. This is a
  visualization of the commanded motion, not a captured internal trial or a
  prediction including slip/actuator dynamics.
- Observed LiDAR-to-body clearance in simulation. Unseen space is not certified
  free; this value is not a replacement for collision validation.
- Decision mode, rejected samples and exact tracking target are unavailable from
  the current `PlanResult`. The UI says so. If a branch exposes a `debug` dict on
  its result, it appears in diagnostics. No algorithm is patched to create data.
- Live monitoring depends on each branch's published topics. Geometry, planner
  timing and active settings remain unavailable when the car does not publish them.

The current algorithm takes the first feasible route/direct/RRT/cached solution;
its tree is not a scored bank of dynamically feasible vehicle trajectories.
Fixed seeds make comparisons more useful, but time-budgeted RRT search remains
sensitive to CPU scheduling. This tool observes performance, not the autograder's
exact scoring or real-car safety guarantees.

## Development and checks

The HTTP server uses Python's standard library. The frontend uses local HTML/CSS
and Canvas2D, with no build step, CDN, browser extension or cloud account. The
simulator runs in a separate process, and both sim and ROS telemetry use bounded
latest-snapshot queues so a slow browser cannot block planning callbacks.

```bash
.venv/bin/python -m unittest discover -s tests -v
node --check racing_dashboard/static/app.js
bash -n scripts/setup_simulator.sh
```

Tests cover source reloads, configuration application/reset, worker controls,
world/map transforms, ROS conversion/staleness, replay integrity and HTTP APIs.
The server binds to loopback; use SSH forwarding for remote access.

Simulator sources are pinned to official
[f1tenth_gym](https://github.com/f1tenth/f1tenth_gym/tree/ec3b1a4693ed20317e42e57a9454d94b824cec81)
and [f1tenth_gym_ros](https://github.com/f1tenth/f1tenth_gym_ros/tree/2c019810b4098ed82d44cad1d7d282a2dfc12421).
Their code/maps retain their upstream licenses. This project's original code is
MIT licensed, Copyright 2026 RobertAris.
