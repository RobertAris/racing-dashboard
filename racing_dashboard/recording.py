"""Bounded recordings, lossless grid compression, and existing Lab 6 CSV import."""
from __future__ import annotations

from array import array
import base64
import copy
import csv
from datetime import datetime, timezone
import io
import json
import math
import statistics
import zlib

FORMAT = "robertaris-racing-dashboard"
MAX_FRAMES = 12000
MAX_RECORDING_BYTES = 64 * 1024 * 1024


def pack_frame(frame: dict) -> dict:
    result = {k: copy.deepcopy(v) for k, v in frame.items() if k not in {"trail", "history", "route", "metadata"}}
    grid = result.get("grid")
    if grid and "data" in grid:
        raw = array("b", grid.pop("data")).tobytes()
        grid["data_b64"] = base64.b64encode(zlib.compress(raw, 3)).decode()
        grid["encoding"] = "zlib-int8-base64"
    return result


def unpack_frame(frame: dict, context: dict | None = None) -> dict:
    result = copy.deepcopy(frame)
    for key in ("route", "metadata"):
        if key not in result and context and key in context:
            result[key] = context[key]
    grid = result.get("grid")
    if grid and "data_b64" in grid:
        if grid.get("encoding") != "zlib-int8-base64":
            raise ValueError("Unsupported recording grid encoding")
        count = int(grid["width"]) * int(grid["height"])
        if not 0 < count <= 4_000_000:
            raise ValueError("Recording grid dimensions are too large")
        encoded = base64.b64decode(grid.pop("data_b64"), validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(encoded, count + 1)
        if len(raw) != count or not decoder.eof or decoder.unconsumed_tail:
            raise ValueError("Recording grid data does not match its dimensions")
        values = array("b")
        values.frombytes(raw)
        grid["data"] = values.tolist()
        grid.pop("encoding", None)
    return result


def percentile(values, fraction=.95):
    values = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    return values[min(len(values) - 1, math.ceil(fraction * len(values)) - 1)] if values else None


def summarize(frames) -> dict:
    frames = list(frames)
    if not frames:
        return {}
    errors = [abs(float(f["cross_track_error"])) for f in frames if f.get("cross_track_error") is not None]
    return {
        "status": frames[-1].get("status", "recorded"),
        "time_s": frames[-1].get("time_s"),
        "progress_m": frames[-1].get("progress_m"),
        "mean_error_m": statistics.fmean(errors) if errors else None,
        "p95_error_m": percentile(errors),
        "planning_p95_ms": percentile(f.get("planning_ms") for f in frames),
        "cycle_p95_ms": percentile(f.get("cycle_ms") for f in frames),
        "frames": len(frames),
    }


class RunBuffer:
    def __init__(self, max_frames=MAX_FRAMES, max_bytes=MAX_RECORDING_BYTES):
        self.max_frames, self.max_bytes = max_frames, max_bytes
        self.clear()

    def clear(self):
        self.frames = []
        self.sizes = []
        self.context = {}
        self.events = []
        self.bytes = 0
        self.dropped_frames = 0
        self.last_key = None
        self.created_at = datetime.now(timezone.utc).isoformat()

    def append(self, frame):
        key = (frame.get("mode"), frame.get("time_s"), frame.get("status"), frame.get("config"))
        if key == self.last_key:
            return
        self.last_key = copy.deepcopy(key)
        for name in ("route", "metadata"):
            if frame.get(name):
                self.context[name] = copy.deepcopy(frame[name])
        packed = pack_frame(frame)
        size = len(json.dumps(packed, separators=(",", ":"), allow_nan=False))
        self.frames.append(packed)
        self.sizes.append(size)
        self.bytes += size
        while len(self.frames) > 1 and (len(self.frames) > self.max_frames or self.bytes > self.max_bytes):
            self.frames.pop(0)
            self.bytes -= self.sizes.pop(0)
            self.dropped_frames += 1

    def event(self, event):
        self.events.append(copy.deepcopy(event))
        self.events = self.events[-1000:]

    def export(self, map_data=None):
        return {
            "format": FORMAT, "version": 1, "created_at": self.created_at,
            "author": "RobertAris", "context": copy.deepcopy(self.context),
            "map": map_data, "frames": copy.deepcopy(self.frames),
            "events": copy.deepcopy(self.events), "summary": summarize(self.frames),
            "dropped_frames": self.dropped_frames,
        }


def validate_recording(data):
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("version") != 1:
        raise ValueError("Choose a RobertAris Racing Lab recording (version 1) or a Lab 6 CSV")
    frames = data.get("frames")
    if not isinstance(frames, list) or not frames or len(frames) > MAX_FRAMES:
        raise ValueError("Recording must contain 1–12000 frames")
    for frame in frames:
        restored = unpack_frame(frame, data.get("context", {}))
        validate_snapshot(restored)
    return data


def validate_snapshot(frame):
    if not isinstance(frame, dict):
        raise ValueError("Snapshot must be an object")
    pose = frame.get("pose")
    if pose is not None and (len(pose) != 3 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in pose)):
        raise ValueError("Pose must be finite [x, y, yaw]")
    stamp = frame.get("time_s", 0)
    if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
        raise ValueError("time_s must be finite")
    for name in ("speed_command", "actual_speed", "steering", "cross_track_error", "planning_ms", "cycle_ms"):
        value = frame.get(name)
        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError(f"{name} must be finite or null")
    return frame


def import_csv(content, name="run.csv"):
    reader = csv.DictReader(io.StringIO(content))
    required = {"time_s", "x_m", "y_m", "yaw_rad"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError("CSV needs time_s, x_m, y_m, yaw_rad columns (use the Lab 6 runner --log)")
    frames = []
    def number(row, key):
        return float(row[key]) if row.get(key, "").strip() else None
    for row in reader:
        if len(frames) >= MAX_FRAMES:
            raise ValueError("CSV has more than 12000 rows; export a shorter section")
        frame = {
            "mode": "replay", "status": "recorded", "time_s": float(row["time_s"]),
            "pose": [float(row[k]) for k in ("x_m", "y_m", "yaw_rad")],
            "speed_command": number(row, "speed_command_m_s"),
            "actual_speed": number(row, "actual_speed_m_s"),
            "steering": number(row, "steering_rad"), "progress_m": number(row, "progress_m"),
            "path": [], "tree": [], "route": [], "command_arc": [], "footprints": [],
            "diagnostics": {"limitations": ["CSV contains pose and commands only; scan, grid, and planner decisions were not recorded."]},
        }
        validate_snapshot(frame)
        frames.append(frame)
    return validate_recording({"format": FORMAT, "version": 1, "frames": frames,
                               "context": {"metadata": {"source": name}}, "events": [], "map": None})
