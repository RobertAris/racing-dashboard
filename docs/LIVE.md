# Live monitoring and chat for any controller repository

Use the dashboard with Lab 8 MPPI, Lab 7 MPC, or another controller to capture
laps, inspect tracking behavior and discuss code and tuning experiments. Select
the local repository and branch in chat settings; the source reader does not
require a particular lab layout or controller algorithm. The adapter
subscribes to ROS 2 and sends data to the local dashboard over HTTP. It publishes
no drive commands and writes no ROS parameters. Controller source remains in its
own checkout.

## Check the running controller and topics

Use your existing saved-map localization and controller launch workflow. Inspect
the actual graph before selecting the adapter inputs:

```bash
ros2 node list
ros2 topic info /pf/pose/odom
ros2 topic echo /pf/pose/odom --once
ros2 topic info /drive
ros2 topic list | sort
```

The real-car example uses `/pf/pose/odom` with `nav_msgs/msg/Odometry`.
Your localization launch is authoritative; names alone do not establish message
types or pose conventions.

| Adapter input | Expected ROS message | Purpose |
| --- | --- | --- |
| Selected pose topic | Odometry, PoseStamped or PoseWithCovarianceStamped | Localized position/heading and available measured speed |
| `/drive` | `ackermann_msgs/msg/AckermannDriveStamped` | Commanded speed and steering angle |
| Optional selected reference topic | `nav_msgs/msg/Path` | Reference visualization, potentially only a short horizon |
| Optional selected path topic | `nav_msgs/msg/Path` | Planned/predicted trajectory visualization |
| `/scan` | `sensor_msgs/msg/LaserScan` | Available laser hits for inspection |
| Full route CSV | Local file on adapter host | Canonical closed lap geometry and source yaw/speed fields |

For alternate pose types, use `--pose-type pose` for PoseStamped or
`--pose-type pose_covariance` for PoseWithCovarianceStamped. Pose-only messages
have no measured speed; commanded speed is never substituted for it.

## Start the server and browser

Install and start the dashboard as shown in the [README](../README.md). Keep the
server bound to `127.0.0.1:8765` and load the saved map YAML used for localization,
with its referenced image. Map paths and analysis source checkout paths refer to
files on the **dashboard server host**.

For a remote ROS host, open a MacBook terminal:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@car
```

Open **http://127.0.0.1:8765**. Keep the tunnel open. This makes the server available
in the Mac browser without ROS or DDS networking on the Mac. Use the same local
and remote port, including when choosing a port other than 8765; the ChatGPT
callback uses that same loopback port.

## Connect the adapter

If another dashboard already receives the adapter's data, use its loopback URL
under **Existing dashboard URL** in the local dashboard's live connection panel.
For example, leave the car's SSH tunnel on 8765 and run the updated local app with
`--port 8766 --telemetry-url http://127.0.0.1:8765`. It reads telemetry and captured
laps without changing the adapter or remote settings. Keep the tunnel running.
The local app's repository paths refer to the Mac in this arrangement.

Run it using the ROS host's sourced ROS Python interpreter. For example, replace
the distro, workspace and file paths below with your actual installation:

```bash
source /opt/ros/humble/setup.bash
source ~/f1tenth_ws/install/setup.bash
cd ~/robertaris-racing-dashboard
python3 -m racing_dashboard.ros_adapter \
  --url http://127.0.0.1:8765 \
  --planner-profile generic \
  --pose-topic /pf/pose/odom --pose-type odometry \
  --scan-topic /scan --drive-topic /drive \
  --map-frame map \
  --route-file /absolute/path/to/real_track_x_y_yaw_speed.csv
```

Use **generic** for Lab 8 or any controller without the older compatibility topic
layout. It only requires localized pose and the full route for lap timing;
commanded speed/steering come from the selected drive topic. A controller does
not have to publish MPC or RRT visualization topics. If it publishes Path
visualizations, add `--path-topic` and/or `--reference-topic` with the names you
verified using `ros2 topic list` and `ros2 topic info`. They remain optional and
never replace the full lap route.

For example, omit both optional flags when the Lab 8 controller only exposes
pose and drive commands. If its visualization message type differs from
`nav_msgs/msg/Path`, leave that overlay disconnected; pose/drive capture and chat
still work. Choose `--speed-mode magnitude` or `--speed-mode longitudinal` to match
the publisher's measured velocity convention. Generic automatic mode uses planar
velocity magnitude.

The existing **mpc** profile remains the default for compatibility and supplies
`/mpc/prediction` and `/mpc/reference`; **rrt** supplies the older RRT marker/grid
topics. Pass the profile explicitly when changing controller families.

The adapter needs `rclpy`, `tf2_ros`, `nav_msgs`, `sensor_msgs`, `geometry_msgs`,
`visualization_msgs` and `ackermann_msgs` from the ROS environment. The server can
use a separate Python environment. A normal Mac virtual environment generally
cannot import ROS. No ROS packages are imported by the ordinary server or adapter
`--help`.

### Monitor an already-running ROS simulator

The simulator's Odometry topic is normally `/ego_racecar/odom`. Check its graph,
then use the simulator's matching full route and saved map:

```bash
python3 -m racing_dashboard.ros_adapter \
  --url http://127.0.0.1:8765 \
  --planner-profile generic \
  --pose-topic /ego_racecar/odom --pose-type odometry \
  --route-file /absolute/path/to/simulator_x_y_yaw_speed.csv \
  --ros-args -p use_sim_time:=true
```

Use `use_sim_time` when the simulator publishes ROS clock time. Match its
`ROS_DOMAIN_ID`, RMW configuration and sourced workspace. This monitors the
existing ROS simulation and controller.

The dashboard's standalone simulator has a specific MPC controller interface.
For Lab 8 and other controllers, run your existing ROS simulator and connect it
with the generic adapter above. See the simulation compatibility section in the
[README](../README.md).
It reads the selected controller revision and launch configuration without
switching branches. The lap engineer described here analyzes Live car captures,
including telemetry received from an existing ROS simulator.

## Align the full route, map and vehicle reference

Supply the running controller's **full closed-track CSV**: required `x,y` in
map-frame meters, optionally followed by `yaw` in radians and `speed` in m/s.
Comment headers and one ordinary header row are accepted. The route file is on
the adapter host. The simulator's bundled route is not automatically aligned to
a saved real-car map.

`/mpc/reference` and `/mpc/prediction` contain only short horizons. They are never
the canonical full route for lap detection. Without valid full route geometry,
the adapter can show telemetry but cannot time a full lap.

Check that the pose, route and saved-map origin agree. TF transforms do not detect
that a route from a different saved map was selected. Geometric route deviation
is unsigned distance to the nearest route segment, not clearance or collision
status. Intentional detours can increase it.

The pose must represent the same physical vehicle point used by the controller.
Set `--pose-offset-m` only after verifying it: a positive value shifts the
localized pose backward along its heading, matching the reviewed MPC controller's
`pose_offset` convention. Do not reuse a simulator offset for the real car without
checking its localization reference.

`--speed-mode auto|longitudinal|magnitude` selects measured-speed semantics.
The MPC profile's automatic mode uses planar Odometry velocity magnitude, matching
the reviewed controller. Verify the publisher supplies meaningful velocity and
frame conventions. Missing measurements remain missing.

## Configure and validate the start gate

The default gate center is the first route waypoint, pointing along the route's
forward tangent. To choose a different gate, provide `--lap-gate-x`,
`--lap-gate-y` and `--lap-gate-yaw` together, in map-frame meters and radians.
The yaw points in the intended direction of travel. Adjust
`--lap-gate-half-width` to cover the actual crossing region.

The first forward crossing begins a timed lap. The next crossing completes it
only after sufficient ordered route coverage. Reverse crossings, jitter and
initial partial laps do not become full laps. A localization jump, implausible
step, missing/stale pose, clock restart or route change can invalidate a capture.
Use `--lap-min-time`, `--lap-max-gap` and `--lap-max-step` to set verified limits
for the track and measurement rate. `--route-open` disables lap detection.

Capture uses the adapter's pose stream before the latest-only display transport.
Completed captures go into a durable outbox, selectable with `--lap-spool-dir`.
Keep generated data outside controller source control. A slow model or browser
does not block ROS callbacks. Finite storage limits apply; restore delivery and
analysis when the queue/backpressure indicators show a backlog.

## Connect your analysis provider

In **Live car → Analysis connection**, choose **ChatGPT plan**, model
**`gpt-6.1-sol`**, reasoning effort **high**. Install the optional sign-in dependency
in the server's environment with `python3 -m pip install -e '.[llm]'`.

Select **Analyze completed live laps automatically** and save. Click
**Connect ChatGPT**, then the displayed **Continue with ChatGPT** link. Sign into
your own account, authorize this dashboard and return after completion. This is
an independent, scoped app authorization; the dashboard does not read another
app's credential cache or gain access to existing ChatGPT conversations.

The callback is `http://127.0.0.1:<dashboard-port>/auth/callback`. Keep the same-port
SSH tunnel open during sign-in. Each installation uses its own account connection;
the repository includes no account or personal token. Account/workspace model
availability and usage limits still apply. A model catalog entry alone does not
verify that inference will succeed.

Alternatively, use your own **OpenAI API** key, or an **OpenAI-compatible endpoint**
with a base URL, model and optional key. API billing is independent of a ChatGPT
subscription. Choose **Provider default** effort if a compatible server expects
no reasoning-effort field. The endpoint is contacted from the dashboard server
host; a model on a different machine needs suitable private connectivity.

Saving settings and listing models issue no test inference. The password field
clears on submission. Credentials stay in protected server-side storage and are
excluded from evidence exports. Enabled reviews send compact evidence and
configured source context to the chosen provider. Capturing and viewing laps do
not require a model connection.

## Select the repository and branch for chat

Open **Settings** in the main chat. Set **Repository path or GitHub URL** to the
checkout you are using, such as `/path/to/lab8-checkout`, or paste its GitHub URL.
GitHub URLs are matched to existing local clones; no clone or fetch is performed.
Use **Load branches** to
list its local and remote-tracking branches, then choose **Branch or revision**.
You can also enter a branch, tag or commit directly. The chosen reference must
already exist locally; the dashboard does not fetch or check out branches.

Leave the branch field blank to read the checkout's current working files,
including local edits. Chat shows the repository and current branch so you can
check which source is attached. An explicit reference reads that revision's Git
blobs without switching the checkout. Python, C/C++, CUDA, configuration and other
supported source files are read generically; MPC package names and Lab 7 file
paths are not required.

Every new question refreshes source context and saved lap history. Ask normal
questions such as “Which parameter in this Lab 8 controller should I tune?” or
“Show the code change for this branch.” The engineer uses the selected repository's
actual parameter names and code instead of assuming MPC settings. Source files
are never imported, executed or modified. Each received lap also keeps the
selected source manifest separately from the refreshed chat context.

Local source is not proof of what was built or launched on the car. Runtime
configuration remains unknown unless supplied in telemetry or verified by you.
The engineer distinguishes local source defaults, your statements and measured
lap evidence when recommending an experiment.

## Interpret the report and replay its evidence

A saved lap has deterministic lap/sector measurements and an optional model
report. Findings separate observation from hypothesis, cite existing evidence,
recommend one reviewed experiment and explain how to validate it. Baselines require
matching route, gate and measurement conventions. Active configuration can be
unknown or different, and track conditions may change between laps.

Select evidence to highlight its recorded map segment in amber. Displayed times
are seconds since lap start; exports retain the original timestamps.
**Evidence** downloads measurements, source context, report and discussion as JSON.
**Replay** downloads a version-1 dashboard recording with scalar pose/command
samples and the full route. Load it in Replay mode to play, scrub or step. Replay
is capped at 12,000 frames, preserving both lap boundaries; the evidence export
retains all captured lap samples. Per-lap archives do not contain scans/grids.

The server persists captures and reports in `laps.sqlite3` under `--data-dir`
(default `~/.local/share/robertaris-racing-dashboard/`). Finished evidence retention
and the pending-job queue are bounded. The rolling **Download recording** buffer
is separate and can drop old frames. Failed model reviews preserve the lap and
can be retried. The model runs in a background worker, independently of ingestion.

The reviewed MPC's published Paths contain XY/yaw without reference speed, solver
status, solve duration or iteration counts. Their headers can use solve-completion
time while their initial state comes from earlier odometry. Missing paths or zero
drive commands do not establish solver infeasibility. These messages do not support
a precisely time-aligned prediction residual. Steering is a command, not measured
wheel angle; route deviation does not establish slip, collision or clearance.

Review proposed changes and apply any chosen experiment through the existing
controller workflow. The dashboard never applies the recommendation to the car.

## Ask follow-up questions

Select a valid lap and open **Discuss this lap** below its report. Start with the
suggested prompt or type a question. The engineer can ask which settings were
active for that captured lap. **Answer this question** opens an explicit setting
name/value form; check the suggested value, enter your answer and choose
**Confirm and discuss**. Numeric parameters use plain SI values, such as `3.0`
for the maximum speed in m/s. **User-confirmed for this lap** lets you inspect,
correct and retract those facts. Answers remain user-confirmed context for this
lap, rather than runtime-verified measurements.

The reply can propose one parameter experiment, explain the supporting evidence,
and state how to validate and roll it back. A code proposal additionally needs
your confirmation that the exact pinned `deployed_source_commit` was used. Its
downloadable `.patch` must match the supplied source file at that revision and
produce valid Python syntax. These checks run in memory; no controller code is
executed or modified. Test proposed behavior through your normal workflow.

Choose another valid captured lap under **Compare with** and describe the tested
change to assess its results. Geometry and measurement conventions must match;
the original lap's confirmations do not automatically describe the other lap.
The model receives observed time/sector differences with configuration and
condition uncertainty. This does not establish that a change caused a gain.

Sending a discussion turn uses your selected provider even if automatic analysis
is paused. Opening a lap alone makes no discussion request. Conversation history
and current confirmations survive a server restart and are included in Evidence
exports. Failed turns can be retried; a disconnected provider leaves the turn
queued. Each lap supports up to 20 exchanges, with a bounded portion of older
conversation sent to the model alongside the current confirmations.

## Frames, freshness and troubleshooting

Spatial messages are transformed into `--map-frame` using TF at their message
stamp. A path pose can use its own frame/stamp; an empty frame or zero stamp falls
back to the path header. Zero-stamped messages use the latest transform and are
monitored by receive age. Planar XY/yaw transforms assume a level track. Laser
motion within a scan is not compensated.

A failed transform or invalid message clears its old dynamic overlay and records
a diagnostic. Dynamic overlays expire after `--stale-after` seconds (default 1).
A stale pose is not displayed as a fresh current position. Message stamps are
checked against the ROS clock; mismatched `use_sim_time` appears in diagnostics.
Do not fix a frame mismatch by relabeling the message. Check the actual TF chain,
for example `ros2 run tf2_ros tf2_echo map laser` using the scan's real frame name.

Display snapshots default to 10 Hz (`--publish-hz`, maximum 30). A separate
latest-only HTTP worker prevents network delays from blocking callbacks.
Connection failures retry with bounded backoff and resume with current telemetry.
Completed lap delivery uses its separate durable outbox. Ctrl-C stops monitoring
without changing the running MPC controller.

For complete original messages and TF, record a ROS bag with your actual pose,
`/drive`, `/mpc/reference`, `/mpc/prediction`, available scan, `/tf` and `/tf_static`
topics. Keep generated recordings outside the controller repository. Automated
checks cover conversion and capture logic; verify native ROS transport, map
alignment and model access in the actual deployment.
