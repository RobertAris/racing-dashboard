# RobertAris Racing Lab

A local workbench for **Lab 7 model predictive control**: simulate the selected
controller, monitor the car, capture complete live laps and review model
recommendations for shorter lap times. Inspect the evidence, test a reviewed
change and compare later laps. The dashboard never edits the controller checkout,
publishes drive commands or writes live ROS parameters.

**Author: RobertAris.** The dashboard is a separate repository from the Lab 7
controller. Select the controller checkout on the dashboard server and set its
source-context reference to `origin/liam-trial-2` when configuring your Trial 2 analysis.
Reading that revision does not switch the checkout's current branch.

## Start the dashboard

Run the server on the ROS host, which can be the car or a lab computer. A normal
Python environment is sufficient for the server; the ROS adapter runs separately
in the existing sourced ROS environment.

```bash
cd ~/robertaris-racing-dashboard
python3 -m venv .venv-monitor
.venv-monitor/bin/python -m pip install -e '.[llm]'
.venv-monitor/bin/racing-dashboard --mode live \
  --map /absolute/path/to/the/localization_map.yaml
```

The `llm` extra supplies the identity-token validation dependencies for ChatGPT
sign-in. OpenAI API and compatible endpoint connections do not require that extra.
The server uses Python 3.10+. Map YAML/image and controller source paths refer to
files on the **server host**. Use the saved map used by the running localization,
with its referenced image alongside it.

If the server is remote, forward the same port from the MacBook:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@car
```

Open **http://127.0.0.1:8765** and select **Live car**. Keep the SSH tunnel running.
The same-port tunnel also carries the ChatGPT sign-in callback. No ROS installation
is required on a Mac used only as the browser.

## Connect Lab 7 telemetry

Start your normal, verified localization and MPC controller workflow. In a second
terminal on the ROS host, source that ROS installation and workspace. For example,
replace these paths with your installed distro/workspace:

```bash
source /opt/ros/humble/setup.bash
source ~/f1tenth_ws/install/setup.bash
cd ~/robertaris-racing-dashboard
python3 -m racing_dashboard.ros_adapter \
  --url http://127.0.0.1:8765 \
  --planner-profile mpc \
  --pose-topic /pf/pose/odom --pose-type odometry \
  --route-file /absolute/path/to/real_track_x_y_yaw_speed.csv
```

Confirm the actual pose topic and type using `ros2 topic info`. The example uses
real-car localized Odometry; for an already-running ROS simulator, use
`--pose-topic /ego_racecar/odom` and, when the simulator uses ROS time, add
`--ros-args -p use_sim_time:=true`.

The MPC adapter reads `/drive`, `/mpc/prediction`, `/mpc/reference`, pose and
available laser scans. The predicted/reference paths are **short MPC horizons**.
Lap detection needs a separate **full closed-track route**, matching the running
controller's CSV: `x, y, yaw, speed` in map-frame meters, radians and m/s. Check map,
pose and full-route alignment before interpreting lap times or tracking error.

Match the controller's pose offset with `--pose-offset-m` after verification.
The MPC speed convention uses planar Odometry velocity magnitude; measured speed
stays unavailable when the publisher supplies no valid measurement. See
[the live guide](docs/LIVE.md) for topic overrides, start gates, frames and timing.

## Simulate the Lab 7 controller without ROS

The standalone simulation runs the selected Lab 7 MPC implementation against the
pinned F1TENTH simulator. Keep the dashboard and controller as separate checkouts:

```text
~/robertaris-racing-dashboard/
~/ese-6150-lab-7-model-predictive-control-group-3/
```

Install its environment and select the controller revision and track:

```bash
cd ~/robertaris-racing-dashboard
bash scripts/setup_simulator.sh
.venv/bin/racing-dashboard --mode simulation \
  --repo ~/ese-6150-lab-7-model-predictive-control-group-3 \
  --ref origin/liam-trial-2 --track levine
```

Open **http://127.0.0.1:8765** and start the simulation. Use `--track spielberg`
for Spielberg. The selected track's launch defaults and `x,y,yaw,speed` route
are loaded from the selected controller source. An explicit Git reference is
snapshotted without checking out or changing the controller branch. Leave the
reference blank to use working-tree source, including your own edits, after Reset.
The dashboard opens in Live car mode by default unless `--mode simulation` is set.

The simulator setup needs Git and either `uv` or an installed Python 3.12+; the
script can use `uv` to provision Python. It installs the MPC dependencies and keeps
pinned simulator checkouts outside this repository. Initial downloads and kernel
compilation can take time. ROS is not needed for this standalone workflow.

The simulation controls expose the MPC's `MAX_SPEED`, `max_accel`, `profile_accel`
and `profile_decel`, using its own validation. **Apply** takes effect at a control
cycle boundary: it rebuilds the MPC with the new settings, preserves the current
simulated vehicle pose and resets solver warm-start state. **Apply & restart**
compares settings from a fresh run. Pause and step to inspect control cycles.
Reset reloads the selected controller source, launch defaults and route.

The source-defined model horizon, timestep and cost matrices are displayed as
configuration rather than generic tuning sliders. Change these through your
normal controller workflow, then reset a working-tree simulation to load them.
Explicit references stay pinned to their selected revision. Save a baseline or
recording to compare behavior and revisit an interval.

Built-in track selection uses the compatible Levine blocked or Spielberg map and
its start pose. Use `--map /path/to/map.yaml` and `--waypoints /path/to/route.csv`
for a matching alternative. A missing/incompatible map, route or MPC interface
produces an error. A real-car map and route require their own validated start
pose; selecting arbitrary geometry does not establish a valid simulation setup.
The cycle interval and model timestep are separate settings even when both are
0.1 s. LLM lap recommendations run on Live car captures; standalone simulation is
for testing and inspecting controller behavior.

## Configure the lap engineer

Open **Analysis connection** in Live car:

1. Choose **ChatGPT plan**, model **`gpt-6.1-sol`**, reasoning effort **high**.
2. Enter the Lab 7 controller checkout path on the server. Set the Git reference
   to **`origin/liam-trial-2`**, or select the revision you intend to review.
3. Enable **Analyze completed live laps automatically** and save the settings.
4. Click **Connect ChatGPT**, then the displayed **Continue with ChatGPT** link.
   Sign into your own account and authorize this dashboard. Return to the dashboard
   after the browser flow completes.

This dashboard obtains its own scoped authorization; it does not read another
app's credentials. Model availability and usage limits follow the connected
account and its app settings. Each person running the repository uses their own
account. Listing models does not verify inference access, and neither saving
settings nor listing models sends a test inference.

Other providers remain selectable:

| Provider | Connection |
| --- | --- |
| OpenAI API | Your own API key and model; billed separately from a ChatGPT subscription |
| OpenAI-compatible endpoint | Your base URL, model and optional key, including a privately reachable local server |

Choose **Provider default** reasoning effort when a compatible server does not
accept a reasoning-effort parameter. The endpoint is reached from the dashboard
server host, so `localhost` refers to that host.

The source manifest pins the resolved local Git commit for each received lap.
It is **not verification of the revision or effective settings deployed on the
car**. The dashboard does not assume a launch file or source default equals a
runtime setting. Missing configuration and solver diagnostics remain unknown.

## Review a lap and test a change

The first forward start-line crossing begins a timed capture. The next crossing
finishes a lap after ordered route coverage and data-quality checks pass.
Localization jumps, pose gaps, route changes and restarts can invalidate a capture.
Initial partial laps and reverse/jitter crossings do not count as full laps.

Select a saved lap in **Lap history** to inspect:

- Lap/sector times, measured and commanded speed, tracking deviation and sample quality.
- Up to three findings with an observation, possible cause, confidence, cited
  evidence, proposed change and validation experiment.
- Baseline deltas when route, gate and measurement conventions match. Unknown or
  different active settings are labeled rather than treated as equal.

Select an evidence episode to highlight its recorded track segment in amber.
Displayed episode times are seconds since lap start. Test one reviewed change at
a time through your existing controller workflow, then compare comparable laps.
No controller change is applied by the dashboard.

## Discuss a lap and confirm settings

Select a valid captured lap, then use **Discuss this lap** below its report.
Click **Start discussion** to ask the engineer what is missing, or type a specific
question. The discussion uses your selected provider and model, including when
automatic lap analysis is paused. Sending a message explicitly requests a model
reply; simply opening a saved lap does not start a discussion request.

The engineer can ask which speed cap, acceleration settings or source revision
was used. Click **Answer this question**, enter the value you checked, then choose
**Confirm and discuss**. Suggested answers are hints to verify, not confirmed
values. For numeric settings, enter the number in the displayed SI units, such as
`3.0` for `MAX_SPEED` in m/s. **User-confirmed for this lap** lets you add, correct
or retract facts. These describe the selected captured lap; they are not measured
runtime values and do not carry over to other laps.

With sufficient confirmed settings and recorded evidence, the engineer can
propose one numerical experiment with its rationale, validation and rollback.
The four native MPC parameters are supported. Changes to acceleration limits also
need the related profile limits confirmed so the proposed configuration remains
consistent with the controller's constraints.

Controller source is optional for telemetry discussion and required for code
proposals. If a discussion started without it, use **Choose controller source**
to open Analysis connection, select the checkout and Git reference, then save.
Return to the lap and click **Attach selected source**. This pins the selected
revision for future replies without changing earlier replies or requesting model
inference. If a reply already failed, attachment leaves that error visible;
click **Retry discussion turn** separately when ready. An existing source pin
cannot be silently replaced.

For a logic change, first confirm `deployed_source_commit` with the full commit
hash shown in the engineer's question, after checking that it is the revision
used for this lap. Code proposals include a reviewable, downloadable `.patch`
against a supplied file at that pinned revision. The dashboard checks that the
patch matches the saved source and that Python syntax remains valid, entirely in
memory. It does not execute the patch or establish that its behavior is correct.
Review and test it through your normal controller workflow.

To assess an experiment, choose another captured lap under **Compare with**,
describe what you changed and send a message. Comparison requires matching route,
gate, map and measurement conventions. Measured time differences are evidence;
configuration and track-condition differences still need checking. The original
lap's confirmations remain scoped to that lap.

Discussion history, confirmations and proposals are saved with the lap and
included in **Evidence** exports. A queued turn waits for a connection and survives
a server restart. Failed replies can be retried without duplicating your message.
Conversations are limited to 20 exchanges; older context may be omitted from the
model request while current confirmations and the saved history remain available.
Run one dashboard server per data directory. A second server must use a different
`--data-dir` while the first owns that lap history, preventing duplicate requests.

Model work runs independently of telemetry. Captures remain available without an
analysis connection; pending jobs show in the queue. A failed report preserves its
capture and can be requested again. A model can suggest an investigation when the
available signals do not establish the cause of a slowdown.

## Save evidence and replay

**Evidence** downloads a lap's measurements, source manifest, report and discussion as JSON.
**Replay** downloads a compatible dashboard recording; load it in Replay mode to
play, scrub or step through the lap. Per-lap replay contains scalar pose/command
telemetry and the full route, with up to 12,000 frames. Full original samples remain
in the evidence export; scans and grids are not archived with each lap.

**Download recording** saves the rolling received telemetry, including available
sensor/path overlays. This buffer is bounded to 12,000 frames or approximately
64 MiB; the oldest frames can drop. Per-lap persistence is independent of this
rolling buffer.

The server stores lap evidence and reports in `laps.sqlite3` under
`~/.local/share/robertaris-racing-dashboard/`; override with `--data-dir` outside
source control. Finished evidence retention and pending work are bounded. The
adapter uses a durable outbox when the dashboard cannot accept a completed lap.
For original ROS messages and TF, use `ros2 bag record` with your actual topics.

Ordinary monitoring stays local. Enabled analysis sends compact lap evidence and
configured source context to the chosen model provider. Credentials stay in
protected server-side storage and are excluded from reports and repository source.
The default hosted configuration does not load model weights on the MacBook.

## Measurement limits

The reviewed MPC publishes reference and prediction geometry without target speed,
solver status, solve duration or iteration counts. Source CSV speeds describe the
loaded file, not a verified effective postprocessed speed profile. Path headers
can reflect solve completion while the initial state comes from earlier odometry.
Missing paths or zero drive commands do not establish solver infeasibility, and
these messages do not support a precisely time-aligned prediction residual.

Steering is a command, not measured wheel angle. Route deviation is neither
clearance nor proof of a collision. Recommendations cannot guarantee a lap-time
improvement; use the cited evidence and a controlled follow-up experiment.

## Development

The server uses Python's standard library; the frontend uses local HTML/CSS and
Canvas2D without a build step or third-party browser libraries. The optional
`mpc` dependency group provides CVXPY/OSQP for direct MPC simulation. It is
independent of the server's optional `llm` authentication dependency group.

```bash
.venv-monitor/bin/python -m unittest discover -s tests -v
node --check racing_dashboard/static/app.js
```

Checks cover lap detection, source provenance, durable analysis jobs, provider
authentication, telemetry conversion, replay integrity and HTTP behavior. ROS
transport and actual model access still need verification in the running car's
environment. The server binds to loopback; use SSH forwarding for remote access.

MIT licensed, Copyright 2026 RobertAris. Included upstream components retain their
respective licenses. Simulator sources are pinned to official
[f1tenth_gym](https://github.com/f1tenth/f1tenth_gym/tree/ec3b1a4693ed20317e42e57a9454d94b824cec81)
and [f1tenth_gym_ros](https://github.com/f1tenth/f1tenth_gym_ros/tree/2c019810b4098ed82d44cad1d7d282a2dfc12421).
