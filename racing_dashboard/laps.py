"""ROS-independent lap timing, evidence validation and deterministic analysis.

Lap timing uses acquisition timestamps. Connection health uses monotonic time in
the adapter; these clocks must never be mixed in a recorded lap.
"""
from __future__ import annotations

import hashlib
import bisect
import json
import math
import uuid
from dataclasses import dataclass


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


class RouteGeometry:
    def __init__(self, route, closed=True):
        self.route = [[_number(p[0], "route x"), _number(p[1], "route y")] for p in route]
        self.closed, self.segments, self.length = closed, [], 0.0
        self.segment_indices = []
        pairs = list(zip(self.route, self.route[1:]))
        if closed and len(self.route) > 2 and self.route[0] != self.route[-1]:
            pairs.append((self.route[-1], self.route[0]))
        for index, (a, b) in enumerate(pairs):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            if length > 1e-8:
                self.segments.append((a[0], a[1], dx, dy, length, self.length))
                self.segment_indices.append(index)
                self.length += length
        self.hash = hashlib.sha256(json.dumps({"route": self.route, "closed": closed},
                                               separators=(",", ":")).encode()).hexdigest()
        self.segment_starts = [segment[5] for segment in self.segments]

    def project(self, pose):
        best = None
        for x, y, dx, dy, length, start in self.segments:
            t = max(0, min(1, ((pose[0]-x)*dx + (pose[1]-y)*dy) / length**2))
            distance = math.hypot(pose[0]-x-t*dx, pose[1]-y-t*dy)
            if best is None or distance < best[0]:
                best = (distance, start+t*length)
        return best or (None, None)

    def reference(self, arc, records):
        if not records or len(records) != len(self.route) or not self.segments:
            return {}
        index = max(0, bisect.bisect_right(self.segment_starts, arc)-1)
        segment = self.segments[index]
        source_index = self.segment_indices[index]
        a, b = records[source_index], records[(source_index+1) % len(records)]
        if not isinstance(a, dict) or not isinstance(b, dict):
            return {}
        fraction = max(0, min(1, (arc-segment[5])/segment[4]))
        result = {}
        for key in ("speed", "yaw"):
            va, vb = a.get(key), b.get(key)
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and math.isfinite(va) and math.isfinite(vb):
                delta = vb-va
                if key == "yaw":
                    delta = math.atan2(math.sin(delta), math.cos(delta))
                result["reference_"+key] = va+delta*fraction
        return result


@dataclass
class LapConfig:
    gate_x: float | None = None
    gate_y: float | None = None
    gate_yaw: float | None = None
    gate_half_width_m: float = 2.0
    hysteresis_m: float = 0.1
    min_lap_time_s: float = 2.0
    max_gap_s: float = 1.0
    max_step_m: float = 2.0
    max_speed_mps: float = 20.0
    min_coverage: float = 0.9
    max_cross_track_m: float = 3.0
    max_samples: int = 30000

    def __post_init__(self):
        for name in ("gate_half_width_m", "hysteresis_m", "min_lap_time_s", "max_gap_s",
                     "max_step_m", "max_speed_mps", "max_cross_track_m"):
            if _number(getattr(self, name), name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0.5 <= self.min_coverage <= 1 or self.max_samples < 3:
            raise ValueError("invalid lap coverage or sample limit")
        if (self.gate_x is None) != (self.gate_y is None):
            raise ValueError("gate_x and gate_y must be supplied together")
        for name in ("gate_x", "gate_y", "gate_yaw"):
            if getattr(self, name) is not None:
                _number(getattr(self, name), name)


class LapDetector:
    """Gate timing plus ordered route coverage; reversing never creates a lap.

    Each call processes a new localization sample, never a repeated UI snapshot.
    Returns zero or more completed/invalid bundles; storage belongs to a worker.
    """
    def __init__(self, route=(), *, closed=True, config=None, metadata=None, session_id=None):
        self.config = config or LapConfig()
        self.session_id = session_id or str(uuid.uuid4())
        self.metadata = dict(metadata or {})
        self.lap_number = 0
        self.sequence = 0
        self.last = None
        self.samples = []
        self.armed_negative = False
        self.clock_source = None
        self.geometry = RouteGeometry(route, closed)
        self._set_gate()

    def _set_gate(self):
        self.enabled = self.geometry.closed and len(self.geometry.segments) >= 3 and self.geometry.length > 1
        self.disabled_reason = None if self.enabled else "A full closed route with at least three segments is required"
        c = self.config
        first = self.geometry.segments[0] if self.geometry.segments else (0, 0, 1, 0, 1, 0)
        self.gate = (first[0] if c.gate_x is None else c.gate_x,
                     first[1] if c.gate_y is None else c.gate_y,
                     math.atan2(first[3], first[2]) if c.gate_yaw is None else c.gate_yaw)

    def set_route(self, route, closed=True):
        geometry = RouteGeometry(route, closed)
        if geometry.hash == self.geometry.hash:
            return []
        result = self.invalidate("route_changed")
        self.geometry = geometry
        self._set_gate()
        return result

    def invalidate(self, reason):
        result = [self._finish("invalid", reason)] if len(self.samples) >= 2 else []
        self.samples = []
        self.last = None
        self.armed_negative = False
        return [self._clean_bundle(bundle) for bundle in result]

    def _finish(self, status, reason=None):
        self.lap_number += 1
        start, end = self.samples[0]["time_s"], self.samples[-1]["time_s"]
        return {"id": f"{self.session_id}:{self.lap_number}", "source_session_id": self.session_id,
                "lap_number": self.lap_number, "status": status, "reason": reason,
                "start_time_s": start, "end_time_s": end, "lap_time_s": end-start,
                "route_hash": self.geometry.hash, "route": self.geometry.route,
                "metadata": {**self.metadata, "route_closed": self.geometry.closed,
                             "route_length_m": self.geometry.length, "clock_source": self.clock_source,
                             "gate": {"x": self.gate[0], "y": self.gate[1], "yaw": self.gate[2],
                                      "half_width_m": self.config.gate_half_width_m},
                             "ordered_coverage": self.coverage / self.geometry.length},
                "samples": self.samples}

    def status(self):
        return {"enabled": self.enabled, "disabled_reason": self.disabled_reason,
                "source_session_id": self.session_id, "lap_number": self.lap_number,
                "recording": bool(self.samples), "sample_count": len(self.samples),
                "current_lap_time_s": (self.samples[-1]["time_s"]-self.samples[0]["time_s"]
                                       if self.samples else None),
                "route_hash": self.geometry.hash, "gate": list(self.gate)}

    def update(self, sample, *, clock_source="ros"):
        if not self.enabled:
            return []
        sample = dict(sample)
        sample["time_s"] = _number(sample["time_s"], "sample timestamp")
        sample["pose"] = [_number(v, "pose") for v in sample["pose"]]
        if len(sample["pose"]) != 3:
            raise ValueError("sample pose must contain x,y,yaw")
        self.sequence += 1
        sample["source_sequence"] = self.sequence
        error, arc = self.geometry.project(sample["pose"])
        sample["cross_track_error"], sample["route_arc_m"] = error, arc
        sample.update(self.geometry.reference(arc, self.metadata.get("route_profile", [])))
        result = []
        previous = self.last
        if previous is not None:
            dt = sample["time_s"]-previous["time_s"]
            distance = math.dist(sample["pose"][:2], previous["pose"][:2])
            reason = ("clock_source_changed" if clock_source != self.clock_source else
                      "source_clock_reset" if dt <= 0 else
                      "telemetry_gap" if dt > self.config.max_gap_s else
                      "localization_jump" if (distance > self.config.max_step_m or
                                               distance > self.config.max_speed_mps*dt+0.25) else None)
            if reason:
                result += self.invalidate(reason)
                previous = None
        self.clock_source = clock_source
        if error > self.config.max_cross_track_m:
            return result + self.invalidate("outside_route_corridor")
        x, y, yaw = self.gate
        c, s = math.cos(yaw), math.sin(yaw)
        signed = (sample["pose"][0]-x)*c + (sample["pose"][1]-y)*s
        sample["_signed"] = signed
        if previous is not None:
            delta = (arc-previous["route_arc_m"]+self.geometry.length/2) % self.geometry.length - self.geometry.length/2
            # Near-parallel lanes and self-intersections can move the nearest
            # projection to a distant part of the route without moving the car.
            # Conservatively invalidate instead of crediting a shortcut.
            distance = math.dist(sample["pose"][:2], previous["pose"][:2])
            if abs(delta) > max(0.5, distance*3+0.05):
                return [self._clean_bundle(b) for b in result+self.invalidate("route_projection_discontinuity")]
            if self.samples:
                sample["progress_m"] = previous["progress_m"] + delta
                # Furthest contiguous forward traversal, rather than distance
                # accumulated by repeatedly driving backwards and forwards.
                if delta > 0 and sample["progress_m"]-self.coverage <= self.geometry.length*0.2:
                    self.coverage = max(self.coverage, sample["progress_m"])
            else:
                sample["progress_m"] = 0.0
            if self.armed_negative and previous["_signed"] <= 0 < signed:
                fraction = -previous["_signed"]/(signed-previous["_signed"])
                boundary = self._interpolate(previous, sample, fraction)
                lateral = -(boundary["pose"][0]-x)*s + (boundary["pose"][1]-y)*c
                if abs(lateral) <= self.config.gate_half_width_m and delta > 0:
                    if self.samples:
                        self.samples.append(boundary)
                        self.coverage = min(self.coverage, max(0.0, boundary["progress_m"]))
                        enough = (self.coverage >= self.geometry.length*self.config.min_coverage and
                                  boundary["time_s"]-self.samples[0]["time_s"] >= self.config.min_lap_time_s)
                        if enough:
                            result.append(self._finish("valid"))
                            self.samples = []
                        else:
                            # Crossing again after a partial traversal must not
                            # silently splice multiple attempts into one lap.
                            result.append(self._finish("invalid", "insufficient_ordered_traversal"))
                            self.samples = []
                    boundary = dict(boundary)
                    boundary["progress_m"] = 0.0
                    self.samples = [boundary]
                    sample["progress_m"] = max(0.0, delta*(1-fraction))
                    self.coverage = sample["progress_m"]
                self.armed_negative = False
        else:
            sample["progress_m"] = 0.0
        if signed < -self.config.hysteresis_m:
            self.armed_negative = True
        if self.samples:
            if len(self.samples) >= self.config.max_samples:
                result += self.invalidate("sample_limit_exceeded")
            else:
                self.samples.append(sample)
        self.last = sample
        return [self._clean_bundle(bundle) for bundle in result]

    @staticmethod
    def _interpolate(a, b, fraction):
        result = dict(b)
        for key in ("time_s", "progress_m", "actual_speed", "speed_command", "steering", "cross_track_error", "reference_speed"):
            if isinstance(a.get(key), (float, int)) and isinstance(b.get(key), (float, int)):
                result[key] = a[key] + (b[key]-a[key])*fraction
        result["pose"] = [a["pose"][i] + (b["pose"][i]-a["pose"][i])*fraction for i in range(3)]
        yaw = a["pose"][2] + math.atan2(math.sin(b["pose"][2]-a["pose"][2]),
                                      math.cos(b["pose"][2]-a["pose"][2]))*fraction
        result["pose"][2] = math.atan2(math.sin(yaw), math.cos(yaw))
        if "raw_pose" in a and "raw_pose" in b:
            result["raw_pose"] = [a["raw_pose"][i]+(b["raw_pose"][i]-a["raw_pose"][i])*fraction for i in range(3)]
        result["interpolated_gate_crossing"] = True
        return result

    @staticmethod
    def _clean_bundle(bundle):
        bundle["samples"] = [{k: v for k, v in sample.items() if not k.startswith("_")}
                             for sample in bundle["samples"]]
        return bundle


def validate_lap(bundle):
    """Validate externally received evidence; raise ValueError on malformed data."""
    if not isinstance(bundle, dict):
        raise ValueError("lap must be an object")
    for key in ("id", "source_session_id", "route_hash"):
        if not isinstance(bundle.get(key), str) or not 1 <= len(bundle[key]) <= 200:
            raise ValueError(f"invalid {key}")
    if type(bundle.get("lap_number")) is not int or bundle["lap_number"] < 1:
        raise ValueError("invalid lap_number")
    if bundle.get("status") not in ("valid", "invalid"):
        raise ValueError("invalid lap status")
    start = _number(bundle.get("start_time_s"), "start_time_s")
    end = _number(bundle.get("end_time_s"), "end_time_s")
    duration = _number(bundle.get("lap_time_s"), "lap_time_s")
    if end <= start or duration <= 0 or abs(end-start-duration) > 0.001:
        raise ValueError("inconsistent lap duration")
    route = bundle.get("route")
    if not isinstance(route, list) or not 3 <= len(route) <= 30000:
        raise ValueError("invalid full route")
    for point in route:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("invalid route point")
        for value in point:
            _number(value, "route coordinate")
    metadata = bundle.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("invalid metadata")
    if not isinstance(metadata.get("route_closed", True), bool):
        raise ValueError("route_closed must be boolean")
    geometry = RouteGeometry(route, metadata.get("route_closed", True))
    if geometry.hash != bundle["route_hash"]:
        raise ValueError("route_hash does not match route geometry")
    if len(geometry.segments) < 3 or geometry.length <= 1:
        raise ValueError("lap requires a nondegenerate full route")
    if bundle["status"] == "valid" and metadata.get("route_closed", True) is not True:
        raise ValueError("valid lap requires a closed route")
    if metadata.get("ordered_coverage") is not None:
        coverage = _number(metadata["ordered_coverage"], "ordered coverage")
        if coverage < 0 or (bundle["status"] == "valid" and coverage < 0.85):
            raise ValueError("insufficient ordered coverage for valid lap")
    samples = bundle.get("samples")
    if not isinstance(samples, list) or not 2 <= len(samples) <= 30000:
        raise ValueError("invalid samples")
    previous_time = None
    scalar_keys = {"time_s", "progress_m", "cross_track_error", "actual_speed", "speed_command", "steering",
                   "route_arc_m", "source_sequence", "pose_sequence", "command_source_time_s", "command_age_s",
                   "reference_speed", "reference_yaw"}
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("invalid sample")
        if set(sample)-scalar_keys-{"pose", "raw_pose", "interpolated_gate_crossing"}:
            raise ValueError("lap samples must contain compact telemetry fields only")
        stamp = _number(sample.get("time_s"), "sample timestamp")
        if (previous_time is not None and stamp < previous_time) or not start-0.001 <= stamp <= end+0.001:
            raise ValueError("sample timestamps must be ordered within lap")
        previous_time = stamp
        pose = sample.get("pose")
        if not isinstance(pose, list) or len(pose) != 3:
            raise ValueError("invalid sample pose")
        for value in pose:
            _number(value, "sample pose")
        if sample.get("raw_pose") is not None:
            if not isinstance(sample["raw_pose"], list) or len(sample["raw_pose"]) != 3:
                raise ValueError("invalid raw pose")
            for value in sample["raw_pose"]:
                _number(value, "raw pose")
        if "interpolated_gate_crossing" in sample and not isinstance(sample["interpolated_gate_crossing"], bool):
            raise ValueError("invalid interpolation flag")
        for key in scalar_keys:
            if sample.get(key) is not None:
                _number(sample[key], key)
    if abs(samples[0]["time_s"]-start) > 0.001 or abs(samples[-1]["time_s"]-end) > 0.001:
        raise ValueError("samples must include lap boundaries")
    if bundle["status"] == "valid":
        first_progress = _number(samples[0].get("progress_m"), "start progress")
        last_progress = _number(samples[-1].get("progress_m"), "end progress")
        if abs(first_progress) > 0.1 or not geometry.length*0.85 <= last_progress <= geometry.length*1.15:
            raise ValueError("valid lap must traverse approximately one full route")
    try:
        encoded = json.dumps(bundle, allow_nan=False)
        if len(encoded.encode("utf-8")) > 16*1024*1024:
            raise ValueError("lap evidence exceeds 16 MiB")
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("lap contains non-JSON evidence") from error
    return bundle


def summarize_lap(bundle, baseline=None):
    """Compute measured metrics; interpretation and causality belong to the LLM."""
    validate_lap(bundle)
    samples = bundle["samples"]
    length = RouteGeometry(bundle["route"]).length
    def values(key):
        return [s[key] for s in samples if isinstance(s.get(key), (int, float))]
    def stats(key):
        data = values(key)
        if not data:
            return None
        weighted, observed_time = 0.0, 0.0
        for a, b in zip(samples, samples[1:]):
            if a.get(key) is not None and b.get(key) is not None:
                dt = b["time_s"]-a["time_s"]
                weighted += (a[key]+b[key])*0.5*dt
                observed_time += dt
        return {"min": min(data), "max": max(data),
                "mean": weighted/observed_time if observed_time else sum(data)/len(data),
                "observed_time_s": observed_time}
    sectors = []
    count = max(4, min(12, int(length/5)))
    def crossing(progress):
        for a, b in zip(samples, samples[1:]):
            pa, pb = a.get("progress_m"), b.get("progress_m")
            if pa is not None and pb is not None and pa <= progress <= pb and pb > pa:
                return a["time_s"]+(b["time_s"]-a["time_s"])*(progress-pa)/(pb-pa)
        return None
    start = bundle["start_time_s"]
    for i in range(count):
        end = bundle["end_time_s"] if i == count-1 else crossing(length*(i+1)/count)
        sectors.append({"id": f"sector-{i+1}", "start_progress_m": length*i/count,
                        "end_progress_m": length*(i+1)/count, "start_time_s": start,
                        "end_time_s": end, "time_s": end-start if end is not None and start is not None else None})
        start = end
    evidence = [{"id": "lap-overview", "kind": "lap_overview", "start_time_s": bundle["start_time_s"],
                 "end_time_s": bundle["end_time_s"], "start_sample": 0, "end_sample": len(samples)-1,
                 "start_progress_m": samples[0].get("progress_m"), "end_progress_m": samples[-1].get("progress_m"),
                 "observed": {"lap_time_s": bundle["lap_time_s"], "route_length_m": length,
                              "actual_speed_mps": stats("actual_speed"),
                              "cross_track_error_m": stats("cross_track_error")}}]
    # Group contiguous events and include exact timestamps for map/chart inspection.
    for kind, condition in (
        ("slow_motion", lambda s: s.get("actual_speed") is not None and abs(s["actual_speed"]) < 0.3),
        ("command_speed_gap", lambda s: s.get("actual_speed") is not None and s.get("speed_command") is not None
         and s["speed_command"]-s["actual_speed"] > 1.0),
        ("route_deviation", lambda s: (s.get("cross_track_error") or 0) > 0.5),
    ):
        active = None
        for i in range(len(samples)+1):
            matches = i < len(samples) and condition(samples[i])
            if matches and active is None:
                active = i
            if not matches and active is not None:
                a, b = samples[active], samples[i-1]
                if b["time_s"]-a["time_s"] >= 0.15:
                    evidence.append({"id": f"evidence-{len(evidence)+1}", "kind": kind,
                                     "start_time_s": a["time_s"], "end_time_s": b["time_s"],
                                     "start_sample": active, "end_sample": i-1,
                                     "start_progress_m": a.get("progress_m"), "end_progress_m": b.get("progress_m"),
                                     "observed": {"actual_speed_min": min((s["actual_speed"] for s in samples[active:i]
                                                                         if s.get("actual_speed") is not None), default=None),
                                                  "cross_track_max_m": max((s.get("cross_track_error") or 0 for s in samples[active:i]), default=None)}})
                active = None
    steering = values("steering")
    reversals = sum(1 for a, b, c in zip(steering, steering[1:], steering[2:])
                    if (b-a)*(c-b) < 0 and abs(b-a) > 0.02 and abs(c-b) > 0.02)
    metadata = {k: v for k, v in bundle.get("metadata", {}).items() if k not in ("route_profile", "route")}
    profile = bundle.get("metadata", {}).get("route_profile", [])
    if isinstance(profile, list) and profile:
        speeds = [p["speed"] for p in profile if isinstance(p, dict) and isinstance(p.get("speed"), (int, float))]
        metadata["route_profile_summary"] = {"waypoint_count": len(profile),
                                             "speed_min_mps": min(speeds) if speeds else None,
                                             "speed_max_mps": max(speeds) if speeds else None,
                                             "yaw_available": any(isinstance(p, dict) and "yaw" in p for p in profile)}
    summary = {"lap_id": bundle["id"], "status": bundle["status"], "reason": bundle.get("reason"),
               "lap_time_s": bundle["lap_time_s"], "route_hash": bundle["route_hash"],
               "route_length_m": length, "sample_count": len(samples), "metadata": metadata,
               "actual_speed_mps": stats("actual_speed"), "speed_command_mps": stats("speed_command"),
               "reference_speed_mps": stats("reference_speed"),
               "cross_track_error_m": stats("cross_track_error"), "steering_rad": stats("steering"),
               "steering_reversals": reversals,
               "max_sample_gap_s": max(b["time_s"]-a["time_s"] for a, b in zip(samples, samples[1:])),
               "sectors": sectors,
               "limitations": ["Route deviation may be intentional obstacle avoidance.",
                               "Solver status, solve time, obstacles, and effective planner settings are unavailable unless explicitly supplied.",
                               "Speed and steering means use trapezoidal time weighting where consecutive values exist; statistics do not establish causality."]}
    comparable = baseline and baseline.get("status") == "valid" and bundle["status"] == "valid" and baseline.get("route_hash") == bundle["route_hash"]
    if comparable:
        first_meta, second_meta = bundle.get("metadata", {}), baseline.get("metadata", {})
        for key in ("gate", "map_hash", "map_frame", "pose_offset_m", "speed_source"):
            if first_meta.get(key) != second_meta.get(key):
                comparable = False
                break
    if comparable:
        base = summarize_lap(baseline)
        summary["baseline"] = {"lap_id": baseline["id"], "lap_time_s": baseline["lap_time_s"],
                               "delta_time_s": bundle["lap_time_s"]-baseline["lap_time_s"],
                               "configuration_comparable": baseline.get("metadata", {}).get("configuration") == bundle.get("metadata", {}).get("configuration")
                               and "configuration" in bundle.get("metadata", {})}
        for sector, other in zip(sectors, base["sectors"]):
            sector["delta_time_s"] = (sector["time_s"]-other["time_s"]
                                      if sector["time_s"] is not None and other["time_s"] is not None else None)
        evidence[0]["observed"].update(baseline_lap_time_s=baseline["lap_time_s"],
                                       delta_time_s=summary["baseline"]["delta_time_s"])
    sector_evidence = []
    for sector in sectors:
        if sector["start_time_s"] is None or sector["end_time_s"] is None:
            continue
        indices = [i for i, s in enumerate(samples) if sector["start_time_s"] <= s["time_s"] <= sector["end_time_s"]]
        if not indices:
            continue
        sector_evidence.append({"id": sector["id"], "kind": "sector", "start_time_s": sector["start_time_s"],
                                "end_time_s": sector["end_time_s"], "start_sample": indices[0], "end_sample": indices[-1],
                                "start_progress_m": sector["start_progress_m"], "end_progress_m": sector["end_progress_m"],
                                "observed": {"sector_time_s": sector["time_s"], "delta_time_s": sector.get("delta_time_s"),
                                             "actual_speed_min_mps": min((samples[i]["actual_speed"] for i in indices
                                                                           if samples[i].get("actual_speed") is not None), default=None),
                                             "reference_speed_min_mps": min((samples[i]["reference_speed"] for i in indices
                                                                              if samples[i].get("reference_speed") is not None), default=None),
                                             "reference_speed_max_mps": max((samples[i]["reference_speed"] for i in indices
                                                                              if samples[i].get("reference_speed") is not None), default=None),
                                             "cross_track_max_m": max((samples[i].get("cross_track_error") or 0 for i in indices), default=None)}})
    summary["evidence"] = (evidence[:1]+sector_evidence+evidence[1:])[:60]
    return summary
