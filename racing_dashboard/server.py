"""Local HTTP application. Simulation lives in a separate spawned process."""
from __future__ import annotations

import argparse
import ast
from collections import deque
import copy
import ipaddress
import json
import math
import mimetypes
import multiprocessing as mp
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .analysis import AnalysisService
from .maps import load_map
from .recording import RunBuffer, import_csv, summarize, unpack_frame, validate_recording, validate_snapshot
from .simulation import TUNABLES, run_worker, validate_changes, validate_options

DEFAULT_CONFIG = {"MAX_SPEED": 6.0, "max_accel": 3.0, "profile_accel": 1.5, "profile_decel": 1.5}
HISTORY_FIELDS = ("time_s", "speed_command", "actual_speed", "steering", "cross_track_error", "planning_ms", "cycle_ms")
STATIC = Path(__file__).parent / "static"


def _source_selection(track, ref):
    if not isinstance(track, str) or track not in {"levine", "spielberg"}:
        raise ValueError("track must be levine or spielberg")
    if not isinstance(ref, str) or (ref and (not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", ref) or ref.startswith("-"))):
        raise ValueError("ref must be blank for the working tree or a branch, tag, or commit reference")
    return track, ref


def checkout_defaults(repo, track="levine", ref=""):
    """Read native MPC defaults, then track launch overrides, without execution.

    A blank reference means the selected working tree. An explicit reference is
    read as Git blobs, leaving the checkout and its active branch unchanged.
    These are source defaults, never evidence of the live car's active settings.
    """
    track, ref = _source_selection(track, ref)
    result = dict(DEFAULT_CONFIG)
    if not repo:
        return result
    root = Path(repo).expanduser()
    commit = None
    if ref:
        try:
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", ref+"^{commit}"],
                                    check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, timeout=5).stdout.strip()
        except (OSError, UnicodeError, subprocess.SubprocessError):
            return result

    def read_tree(relative):
        try:
            if ref:
                source = subprocess.run(["git", "-C", str(root), "show", "--no-ext-diff", f"{commit}:{relative}"],
                                        check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, timeout=5).stdout
            else:
                path = root/relative
                if path.stat().st_size > 1024*1024:
                    return None
                source = path.read_text(encoding="utf-8")
            if len(source) > 1024*1024:
                return None
            return ast.parse(source)
        except (OSError, UnicodeError, SyntaxError, subprocess.SubprocessError):
            return None

    def assignment(node, name):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return None
        return None

    def merge(values):
        if isinstance(values, dict):
            for key in DEFAULT_CONFIG:
                value = values.get(key)
                if type(value) in (int, float) and math.isfinite(value) and value > 0:
                    result[key] = value

    node_tree = read_tree("mpc/scripts/mpc_node.py")
    if node_tree:
        for cls in node_tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            if cls.name == "mpc_config":
                for node in cls.body:
                    value = assignment(node, "MAX_SPEED")
                    merge({"MAX_SPEED": value})
            elif cls.name == "MPC":
                for function in cls.body:
                    if isinstance(function, ast.FunctionDef) and function.name == "__init__":
                        for node in function.body:
                            merge(assignment(node, "defaults"))
    launch_tree = read_tree(f"mpc/launch/{track}_launch.py")
    if launch_tree:
        for node in launch_tree.body:
            merge(assignment(node, "PARAMETERS"))
    return result


class Dashboard:
    def __init__(self, setup=None, data_dir=None):
        self.setup = {"mode": "live", "repo": "", "track": "levine", "ref": "", "helper": "", "waypoints": "", "map_yaml": "",
                      "case": "lap", "seed": 7, "noise": .01, "control_dt": .1, "config": {}, "rate": 1.0}
        self.setup.update(setup or {})
        _source_selection(self.setup["track"], self.setup["ref"])
        self.defaults = checkout_defaults(self.setup.get("repo"), self.setup["track"], self.setup["ref"])
        self.data_dir = Path(data_dir or Path.home() / ".local/share/robertaris-racing-dashboard").expanduser()
        self.lock = threading.RLock()
        self.operations = threading.Lock()
        self.session = self.error = self.map_data = self.baseline = None
        self.map_path = None
        self.map_error = None
        self.last_live = None
        self.live_received = None
        self.recording = RunBuffer()
        self.trail, self.history = deque(maxlen=3000), deque(maxlen=3000)
        self.process = self.commands = self.output = None
        self.generation = 0
        self.closed = False
        self.replay = None
        self.replay_index = 0
        self.replay_playing = False
        self.replay_clock = 0.0
        self.replay_start_time = 0.0
        self.presets = self._read_presets()
        self.analysis = AnalysisService(self.data_dir, active=self.setup["mode"] == "live")
        if self.setup.get("map_yaml"):
            self._load_map(self.setup["map_yaml"])

    def _read_presets(self):
        result = {}
        folder = self.data_dir / "presets"
        if folder.exists():
            for path in sorted(folder.glob("*.json"))[:100]:
                try:
                    preset = json.loads(path.read_text())
                    result[preset["name"]] = validate_changes(preset["config"])
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return result

    def _load_map(self, path):
        if not path or self.map_path == path:
            return
        try:
            value = load_map(path)
            with self.lock:
                self.map_data, self.map_path, self.map_error = value, path, None
        except Exception as exc:
            with self.lock:
                self.map_data, self.map_path = None, path
                self.map_error = f"Could not display map: {exc}"

    def _new_recording(self):
        self.recording.clear()
        self.trail.clear()
        self.history.clear()

    def _accept_snapshot(self, snapshot, record=True):
        validate_snapshot(snapshot)
        with self.lock:
            previous = self.session
            self.session = snapshot
            stamp = snapshot.get("time_s")
            has_stamp = isinstance(stamp, (int, float)) and math.isfinite(stamp)
            # Keep chart time monotonic across a reset/adapter restart.
            if has_stamp and self.history and stamp < self.history[-1]["time_s"]:
                self._new_recording()
                self.recording.event({"event": "source_restarted", "time_s": stamp})
            if record and has_stamp:
                self.recording.append(snapshot)
            pose = snapshot.get("pose")
            if pose and (not self.trail or math.dist(self.trail[-1], pose[:2]) > .005):
                self.trail.append(list(pose[:2]))
            if has_stamp and (not self.history or stamp != self.history[-1]["time_s"]):
                self.history.append({name: snapshot.get(name) for name in HISTORY_FIELDS})
            if previous and previous.get("config") != snapshot.get("config") and snapshot.get("config"):
                self.recording.event({"event": "config_applied", "time_s": snapshot.get("time_s"),
                                      "config": snapshot["config"]})
        self._load_map(snapshot.get("map_yaml"))

    def _reader(self, output, generation):
        while not self.closed and generation == self.generation:
            try:
                message = output.get(timeout=.25)
            except queue.Empty:
                process = self.process
                if process and not process.is_alive() and generation == self.generation:
                    with self.lock:
                        self.error = f"Simulation worker exited (code {process.exitcode}); Reset to try again"
                        if self.session:
                            self.session["status"] = "error"
                    return
                continue
            except (EOFError, OSError, ValueError):
                return
            if generation != self.generation:
                return
            try:
                if message["type"] == "snapshot":
                    self._accept_snapshot(message["data"])
                elif message["type"] == "event":
                    with self.lock:
                        self.recording.event(message["data"])
                        if message["data"].get("event") == "config_applied":
                            self.error = None
                elif message["type"] == "error":
                    with self.lock:
                        self.error = message["message"]
                        if self.session and self.session.get("status") == "loading":
                            self.session["status"] = "error"
            except Exception as exc:
                with self.lock:
                    self.error = f"Invalid simulator snapshot: {exc}"

    def _stop_worker(self):
        self.generation += 1
        process, commands, output = self.process, self.commands, self.output
        self.process = self.commands = self.output = None
        if process:
            try:
                commands.put_nowait({"action": "stop"})
            except (queue.Full, OSError, ValueError):
                pass
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            for channel in (commands, output):
                channel.cancel_join_thread()
                channel.close()

    def _start_worker(self, run=False):
        options = validate_options(self.setup)
        self.defaults = checkout_defaults(options["repo"], options["track"], options["ref"])
        self._stop_worker()
        with self.lock:
            self._new_recording()
            self.error = None
            self.session = {"mode": "simulation", "status": "loading", "time_s": 0.0,
                            "diagnostics": {"message": "Loading the selected checkout and compiling simulator kernels"}}
        context = mp.get_context("spawn")
        self.commands, self.output = context.Queue(maxsize=32), context.Queue(maxsize=4)
        self.process = context.Process(target=run_worker, args=(self.commands, self.output, options), daemon=True)
        self.process.start()
        threading.Thread(target=self._reader, args=(self.output, self.generation), daemon=True).start()
        if run:
            self.commands.put_nowait({"action": "resume"})

    def _send(self, action, **payload):
        if not self.process or not self.process.is_alive():
            raise ValueError("Start the simulation before using this control")
        try:
            self.commands.put_nowait({"action": action, **payload})
        except queue.Full:
            raise ValueError("Simulation is busy; wait for the current update") from None

    def ingest_live(self, frame):
        validate_snapshot(frame)
        frame = copy.deepcopy(frame)
        frame["mode"] = "live"
        with self.lock:
            self.last_live, self.live_received = frame, time.monotonic()
            active = self.setup["mode"] == "live"
        if active:
            self._accept_snapshot(frame)
        return {"ok": True, "active": active}

    def _show_replay(self, index):
        if not self.replay:
            raise ValueError("Load a recording or telemetry CSV first")
        self.replay_index = max(0, min(int(index), len(self.replay["frames"]) - 1))
        frame = unpack_frame(self.replay["frames"][self.replay_index], self.replay.get("context", {}))
        frame["recorded_mode"] = frame.get("mode")
        frame["mode"] = "replay"
        self.session = frame
        # Scrubbing backwards should restore exactly the selected time window.
        start = max(0, self.replay_index - 2999)
        selection = self.replay["frames"][start:self.replay_index + 1]
        self.trail = deque((list(f["pose"][:2]) for f in selection if f.get("pose")), maxlen=3000)
        self.history = deque(({k: f.get(k) for k in HISTORY_FIELDS} for f in selection), maxlen=3000)

    def load_replay(self, data):
        data = validate_recording(data)
        self._stop_worker()
        with self.lock:
            self.replay, self.replay_playing = data, False
            self.setup["mode"] = "replay"
            self.map_data, self.map_path, self.map_error = data.get("map"), None, None
            self.error = None
            self._show_replay(0)
        self.analysis.set_active(False)
        return {"ok": True, "frames": len(data["frames"])}

    def command(self, request):
        if not isinstance(request, dict):
            raise ValueError("Command must be an object")
        action = request.get("action")
        with self.operations:
            if action == "configure":
                updates = request.get("setup", {})
                if not isinstance(updates, dict):
                    raise ValueError("setup must be an object")
                unknown = set(updates) - set(self.setup)
                if unknown:
                    raise ValueError("Unknown setup fields: " + ", ".join(sorted(unknown)))
                if "mode" in updates and updates["mode"] not in {"simulation", "live", "replay"}:
                    raise ValueError("mode must be simulation, live, or replay")
                _source_selection(updates.get("track", self.setup["track"]), updates.get("ref", self.setup["ref"]))
                if "config" in updates:
                    updates["config"] = validate_changes(updates["config"])
                mode_changed = updates.get("mode", self.setup["mode"]) != self.setup["mode"]
                structural = {"repo", "track", "ref", "helper", "waypoints", "map_yaml", "case", "seed", "noise", "control_dt"}
                source_changed = any(k in updates and updates[k] != self.setup[k] for k in structural)
                if mode_changed or source_changed:
                    self._stop_worker()
                with self.lock:
                    self.setup.update(updates)
                    self.defaults = checkout_defaults(self.setup.get("repo"), self.setup["track"], self.setup["ref"])
                    self.error = None
                    if mode_changed or source_changed:
                        self.baseline = None
                        if mode_changed:
                            self._new_recording()
                        else:
                            self.trail.clear()
                            self.history.clear()
                        self.session = None
                        self.replay_playing = False
                        self.map_data = self.map_path = None
                        self.map_error = None
                        if self.setup["mode"] == "live" and self.last_live:
                            self._accept_snapshot(self.last_live)
                        if self.setup["mode"] == "replay" and self.replay:
                            self.map_data = self.replay.get("map")
                            self._show_replay(self.replay_index)
                if self.setup["mode"] != "replay":
                    self._load_map(self.setup.get("map_yaml"))
                self.analysis.set_active(self.setup["mode"] == "live")
            elif action == "replay_load":
                return self.load_replay(request.get("recording"))
            elif action in {"replay_seek", "replay_play", "replay_pause"}:
                with self.lock:
                    if not self.replay:
                        raise ValueError("Load a recording first")
                    if action == "replay_seek":
                        self.replay_playing = False
                        self._show_replay(request.get("index", 0))
                    elif action == "replay_play":
                        if self.replay_index >= len(self.replay["frames"]) - 1:
                            self._show_replay(0)
                        self.replay_playing = True
                        self.replay_clock = time.monotonic()
                        self.replay_start_time = float(self.session.get("time_s", 0))
                    else:
                        self.replay_playing = False
            elif action in {"start", "resume", "pause", "step", "reset", "apply", "rate"}:
                if self.setup["mode"] == "live":
                    raise ValueError("Live Car is read-only. Tune in simulation and apply car presets between runs.")
                if self.setup["mode"] == "replay":
                    if action == "reset":
                        self.replay_playing = False
                        self._show_replay(0)
                    elif action == "step":
                        self.replay_playing = False
                        self._show_replay(self.replay_index + 1)
                    elif action in {"start", "resume", "pause"}:
                        if not self.replay:
                            raise ValueError("Load a recording first")
                        self.replay_playing = action != "pause"
                        self.replay_clock = time.monotonic()
                        self.replay_start_time = float(self.session.get("time_s", 0))
                    else:
                        raise ValueError("Replay shows recorded behavior; select Simulation to test changed settings")
                elif action in {"start", "resume"}:
                    if self.process and self.process.is_alive() and self.session and self.session.get("status") not in {"complete", "collision", "no_progress", "error"}:
                        self._send("resume")
                    else:
                        self._start_worker(run=True)
                elif action == "reset":
                    if "config" in request:
                        self.setup["config"] = validate_changes(request["config"])
                    self._start_worker()
                elif action == "apply":
                    config = validate_changes(request.get("config", {}))
                    self._send("apply", config=config)
                    self.setup["config"].update(config)
                    self.recording.event({"event": "config_requested", "time_s": (self.session or {}).get("time_s"), "config": config})
                elif action == "rate":
                    rate = float(request.get("rate", 1))
                    if not math.isfinite(rate) or not .25 <= rate <= 4:
                        raise ValueError("Simulation rate must be 0.25–4")
                    if self.process:
                        self._send("rate", rate=rate)
                    self.setup["rate"] = rate
                else:
                    self._send(action)
            elif action == "baseline":
                with self.lock:
                    if not self.session or not self.trail:
                        raise ValueError("Run or load a recording before saving a baseline")
                    frames = self.replay["frames"][:self.replay_index + 1] if self.setup["mode"] == "replay" else self.recording.frames
                    self.baseline = {"trace": list(self.trail), "summary": summarize(frames),
                                     "metadata": self.session.get("metadata", {}), "config": self.session.get("config", {})}
            elif action == "clear_baseline":
                self.baseline = None
            elif action == "preset_save":
                name = request.get("name", "")
                if not isinstance(name, str) or not re.fullmatch(r"[\w -]{1,64}", name, flags=re.ASCII):
                    raise ValueError("Preset name must be 1–64 letters, numbers, spaces, underscores, or hyphens")
                config = validate_changes(request.get("config", {}))
                folder = self.data_dir / "presets"
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{name}.json"
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"name": name, "config": config}, indent=2) + "\n")
                temporary.replace(path)
                self.presets[name] = config
            elif action == "preset_load":
                name = request.get("name")
                if name not in self.presets:
                    raise ValueError("Choose a saved preset")
                self.setup["config"] = copy.deepcopy(self.presets[name])
                return {"ok": True, "config": self.setup["config"]}
            else:
                raise ValueError(f"Unknown action: {action}")
        return {"ok": True}

    def state(self):
        # Provider authentication may involve network I/O. Never wait for it
        # while holding the telemetry lock.
        analysis_state = self.analysis.state()
        with self.lock:
            if self.setup["mode"] == "replay" and self.replay_playing and self.replay:
                target = self.replay_start_time + time.monotonic() - self.replay_clock
                index = self.replay_index
                frames = self.replay["frames"]
                while index + 1 < len(frames) and float(frames[index + 1].get("time_s", 0)) <= target:
                    index += 1
                if index != self.replay_index:
                    self._show_replay(index)
                if index == len(frames) - 1:
                    self.replay_playing = False
            session = copy.deepcopy(self.session)
            if session:
                session["trail"], session["history"] = list(self.trail), list(self.history)
                session["summary"] = summarize(self.replay["frames"][:self.replay_index + 1]) if self.setup["mode"] == "replay" and self.replay else summarize(self.recording.frames)
                session["events"] = self.replay.get("events", []) if self.setup["mode"] == "replay" and self.replay else list(self.recording.events)
                if self.setup["mode"] == "live" and self.live_received is not None:
                    age = time.monotonic() - self.live_received
                    session.setdefault("diagnostics", {})["transport_age_s"] = age
                    if age > 2:
                        session["status"] = "disconnected"
            return {
                "session": session, "setup": copy.deepcopy(self.setup), "error": self.error,
                "map_error": self.map_error, "defaults": self.defaults, "tunable_ranges": TUNABLES,
                "recording": {"frames": len(self.replay["frames"]) if self.setup["mode"] == "replay" and self.replay else len(self.recording.frames), "bytes": self.recording.bytes,
                              "dropped_frames": self.recording.dropped_frames},
                "presets": [{"name": name, "config": config} for name, config in sorted(self.presets.items())],
                "baseline": self.baseline, "map": self.map_data,
                "analysis": analysis_state,
                "replay": {"index": self.replay_index, "total": len(self.replay["frames"]) if self.replay else 0,
                           "playing": self.replay_playing},
            }

    def export(self):
        with self.lock:
            if self.setup["mode"] == "replay" and self.replay:
                return self.replay
            return self.recording.export(self.map_data)

    def close(self):
        self.closed = True
        self.analysis.close()
        self._stop_worker()


class Handler(BaseHTTPRequestHandler):
    server_version = "RobertArisRacingLab/0.1"

    def log_message(self, fmt, *args):
        # OAuth codes must not appear in request logs, even on a failed callback.
        if urlsplit(self.path).path == "/auth/callback":
            return
        if str(args[1]) not in {"200", "304"}:
            super().log_message(fmt, *args)

    def send_json(self, value, status=200, download=False, filename="robertaris-racing-run.json"):
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _trusted_host(self):
        try:
            host = urlsplit("//" + self.headers.get("Host", ""))
            valid = (ipaddress.ip_address(self.client_address[0]).is_loopback
                     and host.hostname in {"127.0.0.1", "localhost"} and not host.username
                     and not host.password and host.port == self.server.server_port)
        except ValueError:
            valid = False
        if not valid:
            self.send_json({"ok": False, "error": "Use this dashboard's loopback URL or an SSH tunnel on the same port"}, 403)
        return valid

    @staticmethod
    def _resource_id(value):
        value = unquote(value, errors="strict")
        if not value or any(character in value for character in ("/", "\\")) or any(ord(character) < 32 for character in value):
            raise ValueError("Invalid resource identifier")
        return value

    def do_GET(self):
        if not self._trusted_host():
            return
        path = urlsplit(self.path).path
        if path == "/auth/callback":
            query = {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items() if len(values) == 1}
            try:
                self.server.dashboard.analysis.connection.auth_callback(query)
                self.server.dashboard.analysis.wake.set()
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (ValueError, OSError):
                raw = ("<!doctype html><meta charset=utf-8><title>Connection incomplete</title>"
                       "<h1>ChatGPT connection incomplete</h1><p>Return to the dashboard and start sign-in again.</p>"
                       "<p><a href='/'>Return to dashboard</a></p>").encode()
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        elif path == "/api/analysis/models":
            try:
                self.send_json({"ok": True, "models": self.server.dashboard.analysis.connection.models()})
            except (ValueError, OSError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
        elif path.startswith("/api/laps/"):
            parts = path.split("/")
            try:
                identifier = self._resource_id(parts[3])
                analysis = self.server.dashboard.analysis
                if len(parts) == 4:
                    self.send_json(analysis.get(identifier))
                elif len(parts) == 5 and parts[4] in {"export", "recording"}:
                    recording = parts[4] == "recording"
                    self.send_json(analysis.export(identifier, recording=recording), download=True,
                                   filename="robertaris-lap-recording.json" if recording else "robertaris-lap-analysis.json")
                elif (len(parts) == 8 and parts[4] == "discussion" and parts[6] == "proposal"
                      and parts[7].endswith(".patch")):
                    message_id = self._resource_id(parts[5])
                    proposal_id = self._resource_id(parts[7][:-6])
                    patch = analysis.discussion_patch(identifier, message_id, proposal_id)
                    if not isinstance(patch, str):
                        raise ValueError("Proposal patch is unavailable")
                    raw = patch.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/x-diff; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Content-Disposition", 'attachment; filename="robertaris-mpc-proposal.patch"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    try:
                        self.wfile.write(raw)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
                    self.send_error(404)
            except KeyError:
                self.send_json({"ok": False, "error": "Lap or discussion resource not found"}, 404)
            except (ValueError, TypeError, OSError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
        elif path in {"/api/state", "/api/bootstrap"}:
            self.send_json(self.server.dashboard.state())
        elif path == "/api/recording":
            self.send_json(self.server.dashboard.export(), download=True)
        elif path == "/api/preset.yaml":
            import yaml
            dashboard = self.server.dashboard
            with dashboard.lock:
                config = validate_changes(dashboard.setup.get("config", {}))
                active = (dashboard.session or {}).get("config", {})
                config = {**{k: v for k, v in active.items() if k in TUNABLES}, **config}
            raw = ("# RobertAris Lab 7 MPC: load on mpc_node startup; review for your car.\n" +
                   yaml.safe_dump({"mpc_node": {"ros__parameters": config}}, sort_keys=False)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/yaml")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Content-Disposition", 'attachment; filename="robertaris-mpc-preset.yaml"')
            self.end_headers()
            self.wfile.write(raw)
        elif path == "/api/health":
            self.send_json({"ok": True, "app": "RobertAris Racing Lab", "version": "0.1.0"})
        elif path in {"/", "/index.html", "/app.js", "/styles.css", "/static/app.js", "/static/styles.css"}:
            file = STATIC / ("index.html" if path == "/" else path.rsplit("/", 1)[-1])
            if not file.is_file():
                self.send_error(404)
                return
            raw = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(file))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._trusted_host():
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            self.send_json({"ok": False, "error": "Cross-origin writes are disabled"}, 403)
            return
        path = urlsplit(self.path).path
        parts = path.split("/")
        discussion_route = len(parts) >= 5 and parts[:3] == ["", "api", "laps"] and parts[4] == "discussion"
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Send application/json")
            length = int(self.headers.get("Content-Length", "0"))
            maximum = 32*1024 if discussion_route else 68*1024*1024
            if length <= 0 or length > maximum:
                raise ValueError("Discussion request must be between 1 byte and 32 KiB" if discussion_route
                                 else "Request must be between 1 byte and 68 MiB")
            request = json.loads(self.rfile.read(length), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid number: {value}")))
            dashboard = self.server.dashboard
            if path == "/api/command":
                result = dashboard.command(request)
            elif path == "/api/live":
                result = dashboard.ingest_live(request)
            elif path == "/api/laps":
                result = dashboard.analysis.ingest(request)
            elif path == "/api/analysis/settings":
                result = {"ok": True, **dashboard.analysis.configure(request)}
            elif path == "/api/analysis/connect":
                callback = f"http://127.0.0.1:{self.server.server_port}/auth/callback"
                result = {"ok": True, **dashboard.analysis.connection.auth_start(callback)}
            elif path == "/api/analysis/disconnect":
                result = {"ok": True, "connection": dashboard.analysis.connection.disconnect()}
            elif discussion_route:
                identifier = self._resource_id(parts[3])
                if len(parts) == 5:
                    result = dashboard.analysis.discuss(identifier, request)
                elif len(parts) == 6 and parts[5] == "retry":
                    result = dashboard.analysis.retry_discussion(identifier)
                else:
                    self.send_json({"ok": False, "error": "Unknown discussion endpoint"}, 404)
                    return
            elif len(parts) == 5 and parts[:3] == ["", "api", "laps"] and parts[4] == "retry":
                result = dashboard.analysis.retry(self._resource_id(parts[3]))
            elif path == "/api/import":
                content, name = request.get("content", ""), request.get("name", "run.json")
                data = import_csv(content, name) if name.lower().endswith(".csv") else json.loads(content)
                with dashboard.operations:
                    result = dashboard.load_replay(data)
            elif path == "/api/map":
                path = request.get("path")
                dashboard._load_map(path)
                result = {"ok": dashboard.map_error is None, "error": dashboard.map_error}
            else:
                self.send_json({"ok": False, "error": "Unknown endpoint"}, 404)
                return
            self.send_json(result)
        except KeyError as exc:
            self.send_json({"ok": False, "error": "Lap or discussion resource not found" if discussion_route else str(exc)},
                           404 if discussion_route else 400)
        except (ValueError, TypeError, OSError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)


def make_server(dashboard, host="127.0.0.1", port=8765):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.dashboard = dashboard
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="RobertAris Lab 7 MPC — live lap engineering, simulation tuning, and replay")
    parser.add_argument("--repo", default="", help="Lab 7 MPC checkout to load (read-only)")
    parser.add_argument("--track", choices=("levine", "spielberg"), default="levine", help="Track launch defaults and waypoint selection")
    parser.add_argument("--ref", default="", help="Git branch, tag, or commit; blank uses the working tree")
    parser.add_argument("--helper", default="", help="Simulator helper providing build_env and rear_pose")
    parser.add_argument("--waypoints", default="", help="Full x,y,yaw,speed MPC route CSV override")
    parser.add_argument("--map", dest="map_yaml", default="", help="Saved ROS map YAML for display (and sim map selection)")
    parser.add_argument("--mode", choices=("live", "simulation", "replay"), default="live")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", help="Local presets and lap history; defaults to ~/.local/share/robertaris-racing-dashboard")
    args = parser.parse_args(argv)
    try:
        _source_selection(args.track, args.ref)
    except ValueError as error:
        parser.error(str(error))
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("Use a loopback host and SSH forwarding for remote access")
    setup = {key: getattr(args, key) for key in ("repo", "track", "ref", "helper", "waypoints", "map_yaml", "mode")}
    dashboard = Dashboard(setup, args.data_dir)
    try:
        server = make_server(dashboard, args.host, args.port)
    except OSError as exc:
        parser.exit(1, f"Could not open dashboard port {args.port}: {exc}\n")
    print(f"RobertAris Racing Lab: http://{args.host}:{server.server_port}", flush=True)
    print("Live car monitors ROS and analyzes completed laps when enabled. Simulation loads MPC on Start/Reset.", flush=True)
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        dashboard.close()
        server.server_close()


if __name__ == "__main__":
    main()
