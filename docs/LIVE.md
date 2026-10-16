# Lab 6 live car monitoring — RobertAris

The optional adapter subscribes to ROS 2 and sends snapshots to the dashboard over
HTTP. It never publishes drive commands or writes ROS parameters. Run the adapter
in your existing ROS environment, and use the MacBook as the browser. Switching
Lab 6 branches does not require switching the dashboard repository.

## 1. Check the actual car topics

Start your normal saved-map localization and Lab 6 launch, then inspect:

```bash
ros2 topic info /pf/pose/odom
ros2 topic echo /pf/pose/odom --once
ros2 topic info /scan
ros2 topic info /drive
ros2 topic list | sort
```

The checked Lab 5 waypoint logger uses `/pf/pose/odom` with
`nav_msgs/msg/Odometry` in `map`; that is the adapter default. Your actual
localization launch is authoritative. Supported alternatives are:

| CLI selection | Expected type |
| --- | --- |
| `--pose-type odometry` | `nav_msgs/msg/Odometry` |
| `--pose-type pose` | `geometry_msgs/msg/PoseStamped` |
| `--pose-type pose_covariance` | `geometry_msgs/msg/PoseWithCovarianceStamped` |

For example, use `--pose-topic /pf/viz/inferred_pose --pose-type pose` **only if**
`ros2 topic info` confirms `PoseStamped`. A topic's name does not establish its
message type. The current Lab 6 simulator default is `/ego_racecar/odom`, also
Odometry; pass that explicitly when monitoring the ROS simulator.

Make sure the localization pose represents the same physical point that your
controller uses (for example, the rear axle). The dashboard cannot determine that
from a topic name. `actual_speed` comes from Odometry's `twist.twist.linear.x`;
verify your localization publisher actually supplies it. Pose-only messages have
no measured-speed value, and commanded speed is never substituted for it.

## 2. Start the dashboard on the ROS host

Install this standalone tools repo and start its server using the main
[README](../README.md). Use the dashboard's **Live** mode and load the **saved map
YAML used for localization**, with its referenced image alongside it. Keep the
server bound to `127.0.0.1:8765`.

If the ROS host is the car or a Linux computer, open a separate MacBook terminal:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@car
```

Replace `user@car` with the hostname/IP you normally SSH into. Then open
`http://127.0.0.1:8765` in the MacBook browser. Keep SSH running. This tunnel lets
the server remain local on the ROS host; no ROS installation or DDS networking is
required on the Mac. If ROS and the dashboard already run locally, skip the tunnel.

## 3. Start the adapter in a second ROS-host terminal

Source the same ROS installation and workspace that run the car. For example,
substitute your installed distro and workspace for these paths:

```bash
source /opt/ros/humble/setup.bash
source ~/f1tenth_ws/install/setup.bash
cd ~/robertaris-racing-dashboard
python3 -m racing_dashboard.ros_adapter \
  --url http://127.0.0.1:8765 \
  --pose-topic /pf/pose/odom \
  --pose-type odometry \
  --scan-topic /scan \
  --drive-topic /drive \
  --map-frame map \
  --route-file /absolute/path/to/your/real_track_waypoints.csv
```

The route file is optional when your branch publishes `/rrt/waypoints`. Its first
two CSV columns must be X and Y in **map-frame meters**; comment headers and one
ordinary header row are accepted. The file remains local to the adapter host.
The shipped Levine simulator CSV is not automatically the route for your saved
real map. Load the real map and its matching real-car waypoints together.

With a valid localized pose and route, the adapter reports unsigned geometric
distance to the nearest route segment and continuous progress along the route.
Routes are treated as closed loops by default; use `--route-open` for an open
polyline. Progress unwraps at the loop seam, so it can exceed the route's length
after a lap. Segment geometry is cached until the route changes. A pose jump over
1 meter between received poses raises `diagnostics.localization_jump` and resets
the progress anchor; invalid/stale localization also resets that anchor.

Route deviation is **not** a collision or clearance measure. An RRT detour around
an obstacle can intentionally increase deviation. These metrics only make sense
when the route and localization are correctly aligned in the same map; TF cannot
detect that a different saved map's CSV was loaded. No metric is produced without
valid localization and route geometry.

The ROS Python interpreter must have `rclpy`, `tf2_ros`, `nav_msgs`, `sensor_msgs`,
`geometry_msgs`, `visualization_msgs`, and `ackermann_msgs` from your ROS workspace.
The browser server can use its separate Python environment. A normal Mac Python
venv generally cannot import ROS: do not run the adapter in that environment.
No ROS packages are imported by the ordinary dashboard server or adapter `--help`.

For ROS simulation with simulated time, also use:

```bash
python3 -m racing_dashboard.ros_adapter \
  --pose-topic /ego_racecar/odom --pose-type odometry \
  --ros-args -p use_sim_time:=true
```

Use the same `ROS_DOMAIN_ID`, RMW configuration and sourced workspace as the
running car. Leave real-car parameter tuning to the existing controller workflow;
the dashboard's tuning controls affect its local simulation only.

## What each branch can show

| Input | Visualization |
| --- | --- |
| Selected localization topic | Car position, heading, driven trace |
| `/scan`, `sensor_msgs/msg/LaserScan` | Laser hits transformed from the sensor frame |
| `/drive`, `ackermann_msgs/msg/AckermannDriveStamped` | Commanded speed and steering |
| `/rrt/path`, `nav_msgs/msg/Path` | Selected local path |
| `/rrt/tree`, `visualization_msgs/msg/Marker` LINE_LIST | Accepted RRT tree edges |
| `/rrt/goal`, Marker | Planning target |
| `/rrt/waypoints`, Marker | Reference route |
| `/rrt/grid`, `nav_msgs/msg/OccupancyGrid` | Planner's local occupancy grid |

Missing `/rrt/*` topics are reported in diagnostics; the available pose, scan and
command monitoring still works. These topics do not expose rejected samples,
planning time, stop reasons, tracking target, effective parameters, or the exact
command arc. The adapter reports those limitations instead of inventing values.
It does not infer a vehicle footprint because dimensions and pose reference have
not been verified on the real car. The current Lab 6 node publishes accepted RRT
edges rather than a ranked set of complete candidate trajectories.

## Frames, timing, and connection failures

All spatial data is transformed into `--map-frame` using TF at its message stamp.
Marker pose and rotated grid origins are composed as well. Each path pose can use
its own header, with an empty frame or zero stamp falling back to the path header.
Sensor extrinsics therefore belong in your normal TF tree. These are planar
XY/yaw transforms for a level track; scan motion within a sweep is not compensated.
Zero-stamped messages use the latest transform and are monitored by receive age.

If TF is missing or an input is invalid, its previous overlay clears and a
diagnostic explains the failure. Do not fix an alignment error by relabeling a
topic's frame. Check the TF chain and that the saved-map origin and route agree:

```bash
ros2 run tf2_ros tf2_echo map laser
```

Replace `laser` with the scan's actual `header.frame_id`. A localization jump or a
bad pose reference can still misalign correctly transformed data.

Dynamic overlays clear after one second without valid input. Pose status becomes
stale rather than presenting an old location as current. The last valid occupancy
grid and reference route remain visible because they can be static; their ages
are reported separately. In particular, a route published at 1 Hz does not flicker
when a message arrives slightly late. A new route/grid message with failed TF or
invalid data still clears that overlay (an explicit static route file can remain).
`--stale-after 1.0` changes this threshold. Message stamps are also checked against
the ROS clock; large clock errors or mismatched `use_sim_time` appear in diagnostics.

Snapshots are sent at up to 10 Hz (`--publish-hz`, maximum 30). Network requests
run in a separate worker with a single latest-snapshot slot. A slow or disconnected
dashboard cannot block ROS callbacks or build a backlog. POST failures are logged
and retried with backoff; reconnection resumes with the latest snapshot. Ctrl-C
stops the monitoring adapter without changing the running controller.

Use the dashboard's recording/replay controls to save received snapshots. Those
files contain what the dashboard received, not all original ROS messages. For
complete raw input and TF recording, use `ros2 bag record` with your actual pose,
scan, drive, `/tf`, `/tf_static`, and available `/rrt/*` topics. Keep generated
recordings out of the Lab 6 source repository.
