import base64
import io
import json
import math
from pathlib import Path
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
            self.post("/api/command", {"action": "apply", "config": {"max_speed": 2}})
        self.assertIn("read-only", json.load(caught.exception)["error"])
        self.assertEqual(self.dashboard.state()["recording"]["frames"], 1)

    def test_preset_persistence_and_name_validation(self):
        self.post("/api/command", {"action": "preset_save", "name": "gentle", "config": {"max_speed": .8}})
        reloaded = Dashboard(data_dir=self.temp.name)
        self.assertEqual(reloaded.presets["gentle"]["max_speed"], .8)
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
        path = Path(self.temp.name) / "checkout/lab7_pkg/lab7_pkg"
        path.mkdir(parents=True)
        (path / "planner.py").write_text("raise RuntimeError('must not execute')\nclass PlannerConfig:\n    tracking_lookahead: float = 0.7\n")
        defaults = checkout_defaults(path.parents[1])
        self.assertEqual(defaults["tracking_lookahead"], .7)


if __name__ == "__main__":
    unittest.main()
