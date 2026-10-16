"""Read-only ROS 2 -> RobertAris dashboard bridge; ROS is optional until main()."""
from __future__ import annotations

import argparse
import csv
import json
import math
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def finite(*values):
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("non-finite coordinate or command")


def quaternion_yaw(q):
    """Normalize a ROS quaternion before extracting its planar heading."""
    finite(q.x, q.y, q.z, q.w)
    norm = math.sqrt(q.x*q.x + q.y*q.y + q.z*q.z + q.w*q.w)
    if norm < 1e-12:
        raise ValueError("zero quaternion")
    x, y, z, w = q.x/norm, q.y/norm, q.z/norm, q.w/norm
    return math.atan2(2*(w*z + x*y), 1 - 2*(y*y + z*z))


def transform_point(point, transform):
    """Apply target<-source planar (x, y, yaw), with translation after rotation."""
    x, y = float(point[0]), float(point[1])
    tx, ty, yaw = transform
    finite(x, y, tx, ty, yaw)
    c, s = math.cos(yaw), math.sin(yaw)
    return [tx + c*x - s*y, ty + s*x + c*y]


def transform_pose(pose, transform):
    xy = transform_point(pose[:2], transform)
    finite(pose[2])
    yaw = pose[2] + transform[2]
    return xy + [math.atan2(math.sin(yaw), math.cos(yaw))]


def message_pose(pose):
    return [float(pose.position.x), float(pose.position.y), quaternion_yaw(pose.orientation)]


def grid_origin(info, transform):
    """Keep OccupancyGrid pixels unchanged; compose its rotated origin into map."""
    return transform_pose(message_pose(info.origin), transform)


def marker_points(marker, transform):
    """Marker points are relative to marker.pose as well as header.frame_id."""
    local_pose = message_pose(marker.pose)
    result = []
    for point in marker.points:
        result.append(transform_point(transform_point((point.x, point.y), local_pose), transform))
    return result


def load_route(filename):
    """Accept XY CSV, comment headers, and one ordinary header row."""
    points = []
    with Path(filename).expanduser().open(encoding="utf-8") as source:
        for number, row in enumerate(csv.reader(source), 1):
            if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                continue
            try:
                x, y = float(row[0]), float(row[1])
                finite(x, y)
            except (ValueError, IndexError):
                if not points and any(word in row[0].lower() for word in ("x", "position")):
                    continue
                raise ValueError(f"Invalid XY CSV row {number} in {filename}") from None
            points.append([x, y])
    if len(points) < 2:
        raise ValueError("Route file needs at least two XY points")
    return points


class RouteProjection:
    """Cached polyline segments and continuous arc position on a closed route."""

    def __init__(self, closed=True):
        self.closed = closed
        self.route = None
        self.segments = []
        self.length = 0.0
        self.reset_progress()

    def reset_progress(self):
        self.last_s = self.progress = None

    def set_route(self, points):
        signature = tuple((float(point[0]), float(point[1])) for point in points)
        if signature == self.route:
            return
        self.route = signature
        self.segments = []
        self.length = 0.0
        self.reset_progress()
        pairs = list(zip(signature, signature[1:]))
        if self.closed and len(signature) > 2 and signature[-1] != signature[0]:
            pairs.append((signature[-1], signature[0]))
        for (x, y), (end_x, end_y) in pairs:
            dx, dy = end_x - x, end_y - y
            squared = dx*dx + dy*dy
            if squared < 1e-16:
                continue
            length = math.sqrt(squared)
            self.segments.append((x, y, dx, dy, squared, self.length, length))
            self.length += length

    def project(self, xy):
        if not self.segments:
            return None, None
        best = None
        for x, y, dx, dy, squared, start, length in self.segments:
            t = max(0.0, min(1.0, ((xy[0]-x)*dx + (xy[1]-y)*dy) / squared))
            distance_sq = (xy[0]-x-t*dx)**2 + (xy[1]-y-t*dy)**2
            arc = start + t*length
            if best is None or distance_sq < best[0]:
                best = distance_sq, arc
        distance_sq, arc = best
        if self.last_s is None:
            self.progress = arc
        else:
            delta = arc - self.last_s
            if self.closed:
                delta = (delta + self.length/2) % self.length - self.length/2
            self.progress += delta
        self.last_s = arc
        return math.sqrt(distance_sq), self.progress


class LatestPoster:
    """One bounded latest-only slot: network I/O never blocks ROS callbacks."""

    def __init__(self, url, report=print):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Dashboard URL must be an http(s) URL")
        self.url = url.rstrip("/") + "/api/live"
        self.report = report
        self.queue = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="dashboard-http", daemon=True)
        self.thread.start()

    def submit(self, snapshot):
        try:
            self.queue.put_nowait(snapshot)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(snapshot)
            except queue.Full:
                pass

    def _run(self):
        failures = 0
        while not self.stop.is_set():
            try:
                snapshot = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                data = json.dumps(snapshot, allow_nan=False).encode("utf-8")
                request = urllib.request.Request(self.url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=2) as response:
                    response.read(1024)
                if failures:
                    self.report("Dashboard connection restored")
                failures = 0
            except (OSError, ValueError, urllib.error.URLError) as error:
                failures += 1
                self.report(f"Dashboard POST failed ({error}); retrying with latest snapshot")
                self.stop.wait(min(10, 0.5 * 2 ** min(failures - 1, 5)))

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2.2)


class LiveState:
    """ROS-independent conversion/state logic, also exercised with message stubs."""

    def __init__(self, options, lookup, check_stamp=lambda header: None, now=time.monotonic):
        self.options, self.lookup, self.check_stamp, self.now = options, lookup, check_stamp, now
        self.values, self.received, self.errors = {}, {}, {}
        self.start = now()
        self.static_route = load_route(options.route_file) if options.route_file else []
        self.projector = RouteProjection(closed=not getattr(options, "route_open", False))
        self.last_pose_xy = None
        self.localization_jump_time = None
        self.localization_jump_m = None
        self.topics = {
            "pose": options.pose_topic, "scan_hits": options.scan_topic,
            "command": options.drive_topic, "path": "/rrt/path", "tree": "/rrt/tree",
            "goal": "/rrt/goal", "route": "/rrt/waypoints", "grid": "/rrt/grid",
        }

    def update(self, key, message, convert):
        self.received[key] = self.now()
        try:
            self.check_stamp(message.header)
            self.values[key] = convert(message)
            self.errors.pop(key, None)
            if key == "pose":
                xy = self.values[key]["pose"][:2]
                if self.last_pose_xy is not None:
                    distance = math.hypot(xy[0] - self.last_pose_xy[0], xy[1] - self.last_pose_xy[1])
                    if distance > 1.0:
                        self.localization_jump_time, self.localization_jump_m = self.now(), distance
                        self.projector.reset_progress()
                self.last_pose_xy = xy
        except Exception as error:
            # An old overlay must not survive a new message with failed TF/validation.
            self.values[key] = None
            self.errors[key] = f"{type(error).__name__}: {error}"
            if key == "pose":
                self.last_pose_xy = None
                self.projector.reset_progress()

    def tf(self, header, fallback_frame=None, fallback_stamp=None):
        frame = header.frame_id or fallback_frame
        if not frame:
            raise ValueError("empty frame_id; coordinate frame is unknown")
        stamp = header.stamp
        if stamp.sec == 0 and stamp.nanosec == 0 and fallback_stamp is not None:
            stamp = fallback_stamp
        return self.lookup(frame, stamp)

    def pose(self, message):
        pose = message.pose if self.options.pose_type == "pose" else message.pose.pose
        result = {"pose": transform_pose(message_pose(pose), self.tf(message.header))}
        result["actual_speed"] = None
        if self.options.pose_type == "odometry":
            speed = float(message.twist.twist.linear.x)
            finite(speed)
            result["actual_speed"] = speed
        return result

    def command(self, message):
        finite(message.drive.speed, message.drive.steering_angle)
        return {"speed_command": float(message.drive.speed), "steering": float(message.drive.steering_angle)}

    def scan(self, message):
        finite(message.angle_min, message.angle_increment, message.range_min, message.range_max)
        if message.range_min < 0 or message.range_max <= message.range_min:
            raise ValueError("invalid LaserScan range limits")
        transform = self.tf(message.header)
        stride = max(1, math.ceil(len(message.ranges) / self.options.scan_max_points))
        result = []
        for i in range(0, len(message.ranges), stride):
            radius = message.ranges[i]
            if math.isfinite(radius) and message.range_min <= radius <= message.range_max:
                angle = message.angle_min + i*message.angle_increment
                result.append(transform_point((radius*math.cos(angle), radius*math.sin(angle)), transform))
        return result

    def path(self, message):
        result = []
        for pose in message.poses:
            # Every pose may have its own frame/stamp. No assumption that the path
            # header applies to stamped poses from other publishers.
            transform = self.tf(pose.header, message.header.frame_id, message.header.stamp)
            result.append(transform_point((pose.pose.position.x, pose.pose.position.y), transform))
        return result

    def marker(self, message, key):
        # visualization_msgs/Marker constants: DELETE=2, DELETEALL=3, LINE_LIST=5.
        if message.action in (2, 3):
            return None if key == "goal" else []
        if message.action != 0:
            raise ValueError(f"unsupported marker action {message.action}")
        if message.frame_locked:
            raise ValueError("frame_locked markers are unsupported; publish stamped map-frame markers")
        if key == "tree" and message.type != 5:
            raise ValueError("tree marker must be LINE_LIST")
        points = marker_points(message, self.tf(message.header))
        if key == "tree" and len(points) % 2:
            raise ValueError("LINE_LIST requires point pairs")
        if key == "goal":
            if points:
                return points[0]
            # A conventional single sphere has no points; its pose is the goal.
            if message.type == 2:
                return transform_point(message_pose(message.pose)[:2], self.tf(message.header))
            return None
        return points

    def grid(self, message):
        info = message.info
        finite(info.resolution)
        if info.width <= 0 or info.height <= 0 or info.resolution <= 0:
            raise ValueError("invalid occupancy grid dimensions or resolution")
        if len(message.data) != info.width*info.height:
            raise ValueError("occupancy grid data length does not match dimensions")
        data = [int(value) for value in message.data]
        if any(value < -1 or value > 100 for value in data):
            raise ValueError("invalid occupancy probability")
        return {"data": data, "width": int(info.width), "height": int(info.height),
                "resolution": float(info.resolution), "origin": grid_origin(info, self.tf(message.header))}

    def snapshot(self):
        now = self.now()
        ages = {key: max(0.0, now - received) for key, received in self.received.items()}

        def fresh(key):
            return (key in ages and ages[key] <= self.options.stale_after
                    and key not in self.errors and self.values.get(key) is not None)

        pose = self.values.get("pose") if fresh("pose") else None
        command = self.values.get("command") if fresh("command") else None
        status = "connected" if pose else ("stale_pose" if "pose" in ages else "waiting_for_pose")
        if "pose" in self.errors:
            status = "invalid_pose"
        # A route is a reference, often published at only 1 Hz. Retain a valid
        # route between messages rather than making geometry flicker at timeout.
        route = self.values.get("route")
        if route is None:
            route = self.static_route
        self.projector.set_route(route)
        cross_track, progress = self.projector.project(pose["pose"][:2]) if pose else (None, None)
        if pose is None:
            self.projector.reset_progress()
        snapshot = {
            "mode": "live", "status": status, "time_s": now - self.start,
            "pose": pose["pose"] if pose else None,
            "actual_speed": pose["actual_speed"] if pose else None,
            "speed_command": command["speed_command"] if command else None,
            "steering": command["steering"] if command else None,
            "cross_track_error": cross_track, "progress_m": progress,
            "path": self.values.get("path") if fresh("path") else [],
            "tree": self.values.get("tree") if fresh("tree") else [],
            "goal": self.values.get("goal") if fresh("goal") else None,
            "route": route,
            "scan_hits": self.values.get("scan_hits") if fresh("scan_hits") else [],
            # Occupancy maps can be static; retain last valid grid and report age.
            "grid": self.values.get("grid"),
            "metadata": {
                "source": "ROS 2 read-only adapter", "author": "RobertAris",
                "map_frame": self.options.map_frame, "pose_topic": self.options.pose_topic,
                "pose_type": self.options.pose_type, "scan_topic": self.options.scan_topic,
                "drive_topic": self.options.drive_topic,
                "route_file": str(self.options.route_file or ""),
                "route_closed": self.projector.closed,
                "route_length_m": self.projector.length or None,
                "route_metrics": "nearest segment distance and continuous arc; map/route alignment must be verified",
                "speed_source": "odometry twist.linear.x (verify publisher)" if self.options.pose_type == "odometry" else "unavailable",
                "configuration_source": "unknown; adapter does not read planner parameters",
            },
            "diagnostics": {
                "pose_age_s": ages.get("pose"), "scan_age_s": ages.get("scan_hits"),
                "grid_age_s": ages.get("grid"), "topic_ages_s": ages,
                "route_age_s": ages.get("route"),
                "localization_jump": (self.localization_jump_time is not None
                                      and now - self.localization_jump_time <= self.options.stale_after),
                "localization_jump_m": self.localization_jump_m,
                "missing_topics": [topic for key, topic in self.topics.items() if key not in ages],
                "stale_topics": [topic for key, topic in self.topics.items() if key in ages and not fresh(key)],
                "errors": dict(self.errors),
                "limitations": [
                    "Monitoring only; no drive publication or live car parameter writes.",
                    "Planner timing, rejection reasons and effective configuration are unavailable from standard /rrt topics.",
                    "No command arc or footprint is inferred: wheelbase, dimensions and pose reference need verification.",
                    "Laser points use TF at scan header stamp; per-beam motion compensation is not performed.",
                    "Transforms are planar (XY/yaw); suitable for a level track.",
                ],
            },
        }
        return snapshot


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--url", default="http://127.0.0.1:8765", help="Dashboard base URL")
    result.add_argument("--pose-topic", default="/pf/pose/odom", help="Lab 5 real-car default; verify with ros2 topic info")
    result.add_argument("--pose-type", choices=("odometry", "pose", "pose_covariance"), default="odometry")
    result.add_argument("--scan-topic", default="/scan")
    result.add_argument("--drive-topic", default="/drive")
    result.add_argument("--map-frame", default="map")
    result.add_argument("--publish-hz", type=float, default=10)
    result.add_argument("--stale-after", type=float, default=1.0, help="Clear dynamic overlays after this many seconds")
    result.add_argument("--scan-max-points", type=int, default=540)
    result.add_argument("--route-file", help="Optional real-track XY CSV, already in map-frame meters")
    result.add_argument("--route-open", action="store_true", help="Do not close the reference route or wrap its progress")
    return result


def main(argv=None):
    cli = parser()
    options, ros_arguments = cli.parse_known_args(argv)
    if not (math.isfinite(options.publish_hz) and 0 < options.publish_hz <= 30):
        cli.error("--publish-hz must be finite, >0 and <=30")
    if not (math.isfinite(options.stale_after) and options.stale_after > 0):
        cli.error("--stale-after must be finite and >0")
    if options.scan_max_points < 1:
        cli.error("--scan-max-points must be >0")
    if not options.map_frame:
        cli.error("--map-frame must not be empty")
    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import qos_profile_sensor_data
        from rclpy.time import Time
        from tf2_ros import Buffer, TransformListener
        from nav_msgs.msg import Odometry, OccupancyGrid, Path as RosPath
        from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped
        from sensor_msgs.msg import LaserScan
        from ackermann_msgs.msg import AckermannDriveStamped
        from visualization_msgs.msg import Marker
    except ImportError as error:
        cli.exit(2, f"ROS 2 adapter dependency unavailable: {error}.\nRun this adapter using the Python interpreter from your sourced ROS 2/car workspace,\nwith rclpy, tf2_ros and ackermann_msgs installed. The dashboard itself does not need ROS.\nSee docs/LIVE.md.\n")

    class Adapter(Node):
        def __init__(self):
            super().__init__("robertaris_dashboard_monitor")
            self.buffer = Buffer()
            self.listener = TransformListener(self.buffer, self)
            self.state = LiveState(options, self.lookup, self.check_stamp)
            self.poster = LatestPoster(options.url, lambda text: self.get_logger().warning(text))
            pose_message = {"odometry": Odometry, "pose": PoseStamped,
                            "pose_covariance": PoseWithCovarianceStamped}[options.pose_type]
            subscriptions = [
                (pose_message, options.pose_topic, "pose", self.state.pose),
                (LaserScan, options.scan_topic, "scan_hits", self.state.scan),
                (AckermannDriveStamped, options.drive_topic, "command", self.state.command),
                (RosPath, "/rrt/path", "path", self.state.path),
                (OccupancyGrid, "/rrt/grid", "grid", self.state.grid),
            ]
            self.subscriptions_kept = []
            for message_type, topic, key, convert in subscriptions:
                self.subscriptions_kept.append(self.create_subscription(
                    message_type, topic, lambda msg, key=key, convert=convert: self.state.update(key, msg, convert),
                    qos_profile_sensor_data))
            for key, topic in (("tree", "/rrt/tree"), ("goal", "/rrt/goal"), ("route", "/rrt/waypoints")):
                self.subscriptions_kept.append(self.create_subscription(
                    Marker, topic, lambda msg, key=key: self.state.update(key, msg, lambda item: self.state.marker(item, key)),
                    qos_profile_sensor_data))
            self.timer = self.create_timer(1/options.publish_hz, lambda: self.poster.submit(self.state.snapshot()))
            self.get_logger().info(f"Read-only monitor: {options.pose_topic} -> {options.url}; publishing no ROS control topics")

        def check_stamp(self, header):
            stamp = header.stamp
            if stamp.sec == 0 and stamp.nanosec == 0:
                return
            age = self.get_clock().now().nanoseconds/1e9 - (stamp.sec + stamp.nanosec/1e9)
            if not -options.stale_after <= age <= options.stale_after:
                raise ValueError(f"message stamp is {age:.3f}s from ROS clock; check clock/use_sim_time")

        def lookup(self, frame, stamp):
            frame = frame.lstrip("/")
            target = options.map_frame.lstrip("/")
            if frame == target:
                return (0.0, 0.0, 0.0)
            # Explicit stamped target<-source transform, never a latest-pose shortcut.
            transform = self.buffer.lookup_transform(target, frame, Time.from_msg(stamp)).transform
            finite(transform.translation.x, transform.translation.y)
            return (transform.translation.x, transform.translation.y, quaternion_yaw(transform.rotation))

    rclpy.init(args=ros_arguments)
    node = None
    try:
        node = Adapter()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.poster.close()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
