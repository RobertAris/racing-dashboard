import base64
import io
import json
import math
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
from racing_dashboard.maps import load_map
from racing_dashboard.recording import RunBuffer, import_csv, pack_frame, unpack_frame, validate_recording
from racing_dashboard.server import Dashboard, checkout_defaults, make_server


def frame(stamp=0):
    return {"mode": "simulation", "status": "paused", "time_s": stamp,
            "pose": [stamp, 2, .2], "speed_command": 1, "actual_speed": .8,
            "steering": .1, "planning_ms": 12, "cycle_ms": 14,
            "cross_track_error": .1, "route": [[0, 0], [1, 1]],
            "metadata": {"branch": "test"},
            "grid": {"width": 2, "height": 2, "resolution": .1,
                     "origin": [1, 2, .2], "data": [-1, 0, 100, 0]}}


class RecordingTests(unittest.TestCase):
    def test_grid_roundtrip_and_context(self):
        packed = pack_frame(frame())
        self.assertNotIn("data", packed["grid"])
        restored = unpack_frame(packed, {"route": [[0, 0]], "metadata": {"branch": "a"}})
        self.assertEqual(restored["grid"]["data"], [-1, 0, 100, 0])
        self.assertEqual(restored["route"], [[0, 0]])
        self.assertEqual(restored["metadata"]["branch"], "a")

    def test_buffer_is_bounded_and_duplicate_status_is_recorded(self):
        buffer = RunBuffer(max_frames=2)
        for stamp in range(4):
            buffer.append(frame(stamp))
        self.assertEqual(len(buffer.frames), 2)
        self.assertEqual(buffer.dropped_frames, 2)
        changed = frame(3)
        changed["status"] = "complete"
        buffer.append(changed)
        self.assertEqual(buffer.export()["summary"]["status"], "complete")
        validate_recording(buffer.export())

    def test_corrupt_grid_cannot_expand_past_dimensions(self):
        packed = pack_frame(frame())
        packed["grid"]["width"] = 1
        with self.assertRaises(ValueError):
            unpack_frame(packed)

    def test_csv_import_is_honest_about_missing_telemetry(self):
        result = import_csv("time_s,x_m,y_m,yaw_rad,speed_command_m_s\n0,1,2,0,1.5\n")
        f = result["frames"][0]
        self.assertEqual(f["speed_command"], 1.5)
        self.assertIsNone(f["actual_speed"])
        self.assertIn("CSV", f["diagnostics"]["limitations"][0])
        with self.assertRaises(ValueError):
            import_csv("time_s,x_m,y_m,yaw_rad\n0,nan,2,0\n")


class MapTests(unittest.TestCase):
    def test_rotated_map_crop_keeps_world_position(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            image = Image.new("L", (100, 100), 205)
            image.putpixel((50, 40), 0)
            image.save(path / "map.png")
            (path / "map.yaml").write_text("image: map.png\nresolution: 0.1\norigin: [10, 20, 1.5707963267948966]\n")
            result = load_map(str(path / "map.yaml"))
            # Crop includes 1m padding: left40 top30 right61 bottom51.
            self.assertEqual((result["width"], result["height"]), (21, 21))
            self.assertAlmostEqual(result["origin"][0], 5.1)
            self.assertAlmostEqual(result["origin"][1], 24)
            decoded = base64.b64decode(result["image"].split(",", 1)[1])
            self.assertEqual(Image.open(io.BytesIO(decoded)).size, (21, 21))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dashboard = Dashboard(data_dir=self.temp.name)
        self.server = make_server(self.dashboard, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dashboard.close()
        self.thread.join(2)
        self.temp.cleanup()

    def post(self, path, data, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = Request(self.url + path, data=json.dumps(data).encode(), headers=headers)
        with urlopen(request) as response:
            return json.load(response)

    def test_http_and_cross_origin_write_rejected(self):
        with urlopen(self.url + "/api/health") as response:
            self.assertTrue(json.load(response)["ok"])
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/command", {"action": "configure", "setup": {"mode": "live"}}, "https://unrelated.example")
        self.assertEqual(caught.exception.code, 403)

    def test_live_mode_is_read_only_and_records(self):
        self.post("/api/command", {"action": "configure", "setup": {"mode": "live"}})
        self.post("/api/live", frame(1))
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/command", {"action": "apply", "config": {"MAX_SPEED": 2}})
        self.assertIn("read-only", json.load(caught.exception)["error"])
        self.assertEqual(self.dashboard.state()["recording"]["frames"], 1)

    def test_preset_persistence_and_name_validation(self):
        self.post("/api/command", {"action": "preset_save", "name": "gentle", "config": {"MAX_SPEED": .8}})
        reloaded = Dashboard(data_dir=self.temp.name)
        self.addCleanup(reloaded.close)
        self.assertEqual(reloaded.presets["gentle"]["MAX_SPEED"], .8)
        with self.assertRaises(ValueError):
            self.dashboard.command({"action": "preset_save", "name": "../bad", "config": {}})

    def test_replay_scrub_restores_frame_and_grid(self):
        buffer = RunBuffer()
        buffer.append(frame(0))
        buffer.append(frame(1))
        self.post("/api/command", {"action": "replay_load", "recording": buffer.export()})
        self.post("/api/command", {"action": "replay_seek", "index": 1})
        state = self.dashboard.state()
        self.assertEqual(state["session"]["pose"][0], 1)
        self.assertEqual(state["session"]["grid"]["data"], [-1, 0, 100, 0])
        self.assertEqual(len(state["session"]["trail"]), 2)

    def test_loading_snapshot_does_not_poison_timestamp_history(self):
        self.dashboard._accept_snapshot({"mode": "simulation", "status": "loading"})
        self.dashboard._accept_snapshot(frame(0))
        self.dashboard._accept_snapshot(frame(1))
        self.assertEqual([f["time_s"] for f in self.dashboard.history], [0, 1])
        self.assertEqual(len(self.dashboard.recording.frames), 2)

    def test_static_assets_match_html_urls(self):
        for asset in ("/static/app.js", "/static/styles.css"):
            with urlopen(self.url + asset) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 100)

    def test_checkout_change_invalidates_old_session(self):
        self.dashboard.setup["repo"] = "/old"
        self.dashboard.session = frame()
        with patch.object(self.dashboard, "_stop_worker") as stop:
            self.dashboard.command({"action": "configure", "setup": {"repo": "/new"}})
        stop.assert_called_once()
        self.assertIsNone(self.dashboard.session)

    def test_live_restart_starts_monotonic_recording(self):
        self.dashboard.command({"action": "configure", "setup": {"mode": "live"}})
        self.dashboard.ingest_live(frame(10))
        self.dashboard.ingest_live(frame(.1))
        self.assertEqual([f["time_s"] for f in self.dashboard.recording.frames], [.1])

    def test_replay_does_not_inherit_unrelated_map(self):
        self.dashboard.map_data = {"source": "different track"}
        data = import_csv("time_s,x_m,y_m,yaw_rad\n0,1,2,0\n")
        self.dashboard.load_replay(data)
        self.assertIsNone(self.dashboard.state()["map"])

    def test_checkout_defaults_are_read_without_executing_source(self):
        repo = self.make_checkout()
        defaults = checkout_defaults(repo)
        self.assertEqual(defaults, {"MAX_SPEED": 3, "max_accel": 2.8, "profile_accel": 2.2, "profile_decel": 1.3})
        self.assertFalse((repo/"source-executed").exists())
        spielberg = checkout_defaults(repo, track="spielberg")
        self.assertEqual(spielberg["MAX_SPEED"], 3.5)
        self.assertEqual(spielberg["profile_accel"], 1.2)

    def make_checkout(self):
        repo = Path(self.temp.name)/"checkout"
        (repo/"mpc/scripts").mkdir(parents=True)
        (repo/"mpc/launch").mkdir()
        (repo/"mpc/scripts/mpc_node.py").write_text(
            "from pathlib import Path\nPath(__file__).parents[2].joinpath('source-executed').touch()\n"
            "raise RuntimeError('must not execute')\n"
            "class mpc_config:\n    MAX_SPEED: float = 5.0\n"
            "class MPC:\n    def __init__(self):\n        defaults = {"
            "'max_accel': 2.8, 'profile_accel': 1.2, 'profile_decel': 1.3, 'pose_topic': '/test'}\n")
        (repo/"mpc/launch/levine_launch.py").write_text("raise RuntimeError('must not execute launch')\nPARAMETERS = {'MAX_SPEED': 3.0, 'profile_accel': 2.2, 'track': 'levine'}\n")
        (repo/"mpc/launch/spielberg_launch.py").write_text("PARAMETERS = {'MAX_SPEED': 3.5, 'track': 'spielberg'}\n")
        return repo

    def test_git_ref_defaults_ignore_working_tree_and_leave_branch_unchanged(self):
        repo = self.make_checkout()
        def git(*args):
            return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "source fixture")
        git("update-ref", "refs/remotes/origin/liam-trial-2", "HEAD")
        branch = git("symbolic-ref", "HEAD")
        (repo/"mpc/launch/levine_launch.py").write_text("PARAMETERS = {'MAX_SPEED': 4.0}\n")
        self.assertEqual(checkout_defaults(repo)["MAX_SPEED"], 4)
        self.assertEqual(checkout_defaults(repo, ref="origin/liam-trial-2")["MAX_SPEED"], 3)
        self.assertEqual(git("symbolic-ref", "HEAD"), branch)
        self.assertIn("4.0", (repo/"mpc/launch/levine_launch.py").read_text())
        self.assertFalse((repo/"source-executed").exists())

    def test_default_mode_and_native_mpc_parameters(self):
        state = self.dashboard.state()
        self.assertEqual(state["setup"]["mode"], "live")
        self.assertEqual(state["setup"]["track"], "levine")
        self.assertEqual(state["setup"]["ref"], "")
        self.assertEqual(state["setup"]["control_dt"], .1)
        self.assertEqual(state["defaults"], {"MAX_SPEED": 6, "max_accel": 3, "profile_accel": 1.5, "profile_decel": 1.5})

    def test_track_and_ref_changes_refresh_defaults_and_invalidate_source(self):
        repo = self.make_checkout()
        self.dashboard.command({"action": "configure", "setup": {"repo": str(repo)}})
        self.dashboard.session = frame()
        with patch.object(self.dashboard, "_stop_worker") as stop:
            self.dashboard.command({"action": "configure", "setup": {"track": "spielberg"}})
        stop.assert_called_once()
        self.assertEqual(self.dashboard.defaults["MAX_SPEED"], 3.5)
        self.assertIsNone(self.dashboard.session)
        with patch("racing_dashboard.server.checkout_defaults", return_value={"MAX_SPEED": 4}) as defaults:
            self.dashboard.command({"action": "configure", "setup": {"ref": "origin/liam-trial-2"}})
        defaults.assert_called_once_with(str(repo), "spielberg", "origin/liam-trial-2")
        self.assertEqual(self.dashboard.defaults["MAX_SPEED"], 4)

    def test_source_and_mode_changes_clear_baseline_but_config_only_preserves_it(self):
        self.dashboard.ingest_live(frame(1))
        self.dashboard.command({"action": "baseline"})
        baseline = self.dashboard.baseline
        self.assertIsNotNone(baseline)
        self.dashboard.command({"action": "configure", "setup": {"config": {"MAX_SPEED": 2.5}}})
        self.assertIs(self.dashboard.baseline, baseline)
        self.dashboard.command({"action": "configure", "setup": {"track": "spielberg"}})
        self.assertIsNone(self.dashboard.baseline)
        for update in ({"map_yaml": "/missing/map.yaml"}, {"repo": "/different/repo"},
                       {"ref": "origin/liam-trial-2"}, {"mode": "simulation"}):
            with self.subTest(update=update):
                self.dashboard.baseline = baseline
                self.dashboard.command({"action": "configure", "setup": update})
                self.assertIsNone(self.dashboard.baseline)

    def test_invalid_track_and_ref_do_not_change_setup(self):
        for update in ({"track": "other"}, {"track": []}, {"ref": "--output=/tmp/bad"},
                       {"ref": "main:file.py"}, {"ref": "bad\nref"}, {"ref": 2},
                       {"ref": "HEAD~1"}, {"ref": "main^"}, {"ref": "main+other"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.dashboard.command({"action": "configure", "setup": update})
        self.assertEqual(self.dashboard.setup["track"], "levine")
        self.assertEqual(self.dashboard.setup["ref"], "")

    def test_exported_preset_targets_mpc_node_with_native_parameters(self):
        self.post("/api/command", {"action": "configure", "setup": {"config": {"MAX_SPEED": 2.5, "max_accel": 3}}})
        with urlopen(self.url+"/api/preset.yaml") as response:
            text = response.read().decode()
            self.assertIn("robertaris-mpc-preset.yaml", response.headers["Content-Disposition"])
        self.assertIn("mpc_node:", text)
        self.assertIn("MAX_SPEED: 2.5", text)
        self.assertIn("max_accel: 3", text)
        self.assertNotIn("rrt_node", text)


if __name__ == "__main__":
    unittest.main()
