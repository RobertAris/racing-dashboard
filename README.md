# RobertAris Racing Lab

A local workbench for **autonomous racing controllers across labs**: chat about
your selected repository and branch, monitor the car, capture complete live laps
and review parameter and code recommendations. Lab 8 MPPI, Lab 7 MPC and other
controller repositories can supply source context without a required file layout.
The dashboard never edits the controller checkout, publishes drive commands or
writes live ROS parameters. The optional built-in simulator supports the MPC
interface described below.

**Author: RobertAris.** Keep the dashboard and your controller in separate
repositories. Select your controller checkout and branch in chat settings.
Reading another branch does not switch or modify the checkout's current branch.

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

If the car already has a dashboard receiving telemetry and you run this updated
dashboard locally, connect the two instead of opening an empty local session:

```bash
.venv/bin/racing-dashboard --port 8766 \
  --telemetry-url http://127.0.0.1:8765
```

Open **http://127.0.0.1:8766**. The original SSH tunnel stays open on 8765. The local
dashboard reads live telemetry and imports saved laps from it, retaining each
capture's source manifest. It never changes the remote dashboard's model settings
or sends commands to the car. Chat can use controller source on the local Mac.
You can also set **Existing dashboard URL** under Live car connection settings;
that selection is saved for later launches. Clear it to accept direct ROS adapter
posts. Stale upstream data is labeled rather than kept artificially live.

## Connect controller telemetry

Start your normal localization and controller workflow. In a second
terminal on the ROS host, source that ROS installation and workspace. For example,
replace these paths with your installed distro/workspace:

```bash
source /opt/ros/humble/setup.bash
source ~/f1tenth_ws/install/setup.bash
cd ~/robertaris-racing-dashboard
python3 -m racing_dashboard.ros_adapter \
  --url http://127.0.0.1:8765 \
  --planner-profile generic \
  --pose-topic /pf/pose/odom --pose-type odometry \
  --route-file /absolute/path/to/real_track_x_y_yaw_speed.csv
```

Confirm the actual pose topic and type using `ros2 topic info`. The example uses
real-car localized Odometry; for an already-running ROS simulator, use
`--pose-topic /ego_racecar/odom` and, when the simulator uses ROS time, add
`--ros-args -p use_sim_time:=true`.

The generic adapter reads drive commands, pose and available laser scans without
requiring planner-specific visualization topics. Use `--path-topic` and
`--reference-topic` for optional paths published by your controller, or
`--planner-profile mpc` for the built-in MPC topic defaults. These optional paths
are **short controller horizons**, not full lap routes.
Lap detection needs a separate **full closed-track route**, matching the running
controller's CSV: `x, y, yaw, speed` in map-frame meters, radians and m/s. Check map,
pose and full-route alignment before interpreting lap times or tracking error.

Match the controller's pose offset with `--pose-offset-m` after verification.
Choose the Odometry speed convention with `--speed-mode`; measured speed
stays unavailable when the publisher supplies no valid measurement. See
[the live guide](docs/LIVE.md) for topic overrides, start gates, frames and timing.

## Use the built-in MPC simulator

The standalone simulator supports the MPC interface used by the Lab 7 controller
against the pinned F1TENTH simulator. Source review, chat and live telemetry also
support other controllers; selecting a Lab 8 repository does not make its MPPI
implementation compatible with this simulator. Keep separate checkouts:

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

## Chat with the racing engineer

Use **Racing engineer** at the top of the dashboard as a normal conversation.
Ask about lap performance, compare runs, or request concrete parameter and code
changes. The same chat is available in Live car, Simulation and Replay, including
before the first lap. You do not need to start a per-lap discussion, attach source
for each message, or fill out confirmation forms.

Open **Chat settings** once:

1. Choose your provider and connect your own ChatGPT account, or enter your
   OpenAI API key / compatible endpoint credentials.
2. Enter any controller repository path **on the dashboard server**, including
   a Lab 8 MPPI or another lab checkout. You can also paste its GitHub URL; the
   dashboard matches it to an existing local clone so local edits stay available.
   It does not clone or fetch repositories. A checkout
   selected for simulation is also available to chat when no separate chat source
   is configured.
3. Click **Load branches** and choose the branch you are using, or enter a branch,
   tag or commit under **Branch or revision**. Leave it blank to follow the
   checkout's current branch and working files, including local edits.
4. Save the settings. Automatic lap reports are optional; chat works with them off.

Provider sign-in and model discovery work independently of repository setup.
An unfinished repository field cannot prevent connecting your ChatGPT account.
On Python installations with an empty default HTTPS trust store, the dashboard
loads a verified system certificate bundle, or an installed `certifi` bundle.
Certificate verification remains enabled; certificate errors identify the trust
configuration to fix instead of appearing as generic timeouts.

Every question refreshes the saved lap catalog and local source context. The
chat displays the repository and selected branch/revision. Recommendations use
that controller's source and parameter names rather than a fixed MPC schema. The
engineer receives detailed evidence for relevant laps, source and configuration
files, and a bounded sample of the current run. It can request additional retained
lap evidence or source excerpts when needed. Context size is bounded; file and
history omissions are recorded rather than presented as complete coverage.
Only supported text files in the chosen checkout are read. Credentials, hidden
files, dependency/build directories and symbolic links are excluded.

Try questions such as:

- “Compare my latest lap with the fastest comparable lap. Where am I losing time?”
- “Which speed-profile parameter should I change, from what value to what value?”
- “Show the code change to reduce steering oscillation, including the file and
  how I should test it.”

Replies use normal text and formatted code blocks. **Ask about this lap** focuses
an existing capture without hiding the other saved laps. **Retry** preserves your
message after a failed reply. Chat history survives a server restart; **New chat**
clears the conversation while keeping captured laps. Earlier exchanges may be
omitted from model context when the conversation grows; the saved transcript
remains visible.

Source defaults and local edits do not prove what ran on the car. The engineer
can recommend a numerical experiment against the supplied source, label its
assumptions, and ask you to verify a missing runtime setting in the conversation.
Code snippets and diffs are recommendations for review and testing; chat never
applies them or changes live ROS parameters.

Each installation uses its own connection and account allowance. API billing is
separate from a ChatGPT subscription. Credentials remain in protected server-side
storage. Sending a chat message sends the relevant lap evidence, conversation and
selected source context to the chosen provider. Local source paths refer to the
server host; use a local server or mount the checkout there when needed.

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

## Detailed lap reports and previous discussions

Lap history and automatic reports remain available below the main chat. Select a
capture to inspect sector times, tracking and cited evidence. Source manifests
attached to captures describe the selected local revision, not a verified deployed
revision. Reports run independently of telemetry and can be retried after failure.

Previous per-lap discussions remain accessible under the advanced discussion
panel. Their user confirmations and validated downloadable patches stay scoped to
that original lap and pinned source. New conversations should use the main chat.
Run one dashboard server per data directory; a second server needs a separate
`--data-dir` so pending model requests cannot be processed twice.

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

The server stores lap evidence, reports and chat history in `laps.sqlite3` under
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
