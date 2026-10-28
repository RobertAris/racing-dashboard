"""Read-only relay freshness, provenance and independently imported lap history."""
import base64
import copy
import io
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError

from PIL import Image

from racing_dashboard.upstream import UpstreamTelemetry, validate_telemetry_url


URL = "http://127.0.0.1:8765"


def source(sequence=1, age=.1):
    return {"setup": {"mode": "live"}, "session": {
        "status": "connected", "source_session_id": "car-session", "source_sequence": sequence,
        "source_time_s": 100 + sequence, "time_s": sequence,
        "pose": [1, 2, .1], "actual_speed": 2, "speed_command": 2.2, "steering": .1,
        "diagnostics": {"transport_age_s": age}, "history": [{"large": "not relayed"}],
        "trail": [[1, 2]], "summary": {"status": "connected"}, "events": [],
        "map_yaml": "/remote/host/map.yaml",
    }, "analysis": {"laps": []}, "map": None}


class FakeAnalysis:
    def __init__(self):
        self.imported = []

    def state(self):
        return {"laps": [{"id": row[0]["id"]} for row in self.imported]}

    def ingest(self, lap, *, source_context):
        duplicate = any(row[0]["id"] == lap["id"] for row in self.imported)
        if not duplicate:
            self.imported.append((copy.deepcopy(lap), copy.deepcopy(source_context)))
        return {"ok": True, "duplicate": duplicate}


class FakeDashboard:
    def __init__(self):
        self.lock = threading.RLock()
        self.setup = {"mode": "live", "telemetry_url": URL, "map_yaml": ""}
        self.closed = False
        self.listen_port = 8766
        self.analysis = FakeAnalysis()
        self.received = []
        self.map_data = self.map_path = self.map_error = None

    def ingest_live(self, frame, *, transport_age_s=0):
        self.received.append((copy.deepcopy(frame), transport_age_s))
        return {"ok": True, "active": True}


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = FakeDashboard()
        self.relay = UpstreamTelemetry(self.dashboard, autostart=False)
        self.addCleanup(self.relay.close)

    def test_url_validation_restricts_plain_http_and_credentials(self):
        self.assertEqual(validate_telemetry_url(" http://localhost:8765/ "), "http://localhost:8765")
        self.assertEqual(validate_telemetry_url("https://car.example"), "https://car.example")
        self.assertEqual(validate_telemetry_url(""), "")
        for value in ("http://car.example", "file:///tmp/foo", "https://name:secret@car.example",
                      "http://127.0.0.1:8765/api/state", "https://car.example?token=private", []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_telemetry_url(value)

    def test_fresh_session_strips_remote_history_and_preserves_sample_age(self):
        with patch.object(self.relay, "_read", return_value=source()) as read:
            self.assertTrue(self.relay.poll_once())
        self.assertEqual(read.call_args.args, (URL + "/api/state",))
        frame, age = self.dashboard.received[0]
        self.assertEqual(age, .1)
        self.assertEqual(frame["source_time_s"], 101)
        self.assertEqual(frame["source_sequence"], 1)
        self.assertEqual(frame["actual_speed"], 2)
        for name in ("history", "trail", "summary", "events", "map_yaml"):
            self.assertNotIn(name, frame)
        self.assertEqual(self.relay.state()["status"], "connected")

    def test_repeated_or_old_pose_never_refreshes_local_arrival(self):
        with patch.object(self.relay, "_read", return_value=source()):
            self.relay.poll_once()
            self.assertFalse(self.relay.poll_once())
            self.relay._last_received = time.monotonic() - 3
            self.assertFalse(self.relay.poll_once())
        self.assertEqual(len(self.dashboard.received), 1)
        self.assertEqual(self.relay.state()["status"], "stale")
        for age in (2.1, None, float("nan")):
            with patch.object(self.relay, "_read", return_value=source(sequence=2, age=age)):
                self.assertFalse(self.relay.poll_once())
        self.assertEqual(len(self.dashboard.received), 1)

    def test_session_restart_with_reset_sequence_is_a_new_sample(self):
        value = source()
        with patch.object(self.relay, "_read", return_value=value):
            self.relay.poll_once()
            value["session"]["source_session_id"] = "new-car-session"
            self.assertTrue(self.relay.poll_once())
        self.assertEqual(len(self.dashboard.received), 2)

    def test_imported_laps_preserve_source_and_oldest_first_order(self):
        value = source()
        value["analysis"]["laps"] = [{"id": "car:2"}, {"id": "car:1"}]
        manifest = {"status": "local_source", "commit": "a" * 40, "files": []}

        def read(url):
            if url.endswith("/api/state"):
                return value
            return {"lap": {"id": url.rsplit("/", 1)[-1].replace("%3A", ":")},
                    "source_context": manifest, "connection": {"access_token": "never copied"}}

        with patch.object(self.relay, "_read", side_effect=read):
            self.relay.poll_once()
            self.assertEqual(self.dashboard.analysis.imported, [])
            self.assertTrue(self.relay.import_one())
            self.assertTrue(self.relay.import_one())
            self.relay.poll_once()
            self.assertFalse(self.relay.import_one())
        self.assertEqual([row[0]["id"] for row in self.dashboard.analysis.imported], ["car:1", "car:2"])
        self.assertEqual(self.dashboard.analysis.imported[0][1], manifest)
        self.assertNotIn("never copied", json.dumps(self.dashboard.analysis.imported))
        self.assertEqual(self.relay.state()["imported_laps"], 2)

    def test_absent_manifest_stays_unknown_instead_of_attaching_local_source(self):
        value = source()
        value["analysis"]["laps"] = [{"id": "car:1"}]
        with patch.object(self.relay, "_read", side_effect=[value, {"lap": {"id": "car:1"}}]):
            self.relay.poll_once()
            self.relay.import_one()
        manifest = self.dashboard.analysis.imported[0][1]
        self.assertEqual(manifest["status"], "unavailable")
        self.assertFalse(manifest["deployment_verified"])

    def test_mode_change_while_request_is_inflight_discards_received_data(self):
        def read(url):
            self.dashboard.setup["mode"] = "simulation"
            return source()

        with patch.object(self.relay, "_read", side_effect=read):
            self.assertFalse(self.relay.poll_once())
        self.assertEqual(self.dashboard.received, [])
        with patch.object(self.relay, "_read") as read:
            self.assertFalse(self.relay.poll_once())
            read.assert_not_called()
        self.assertEqual(self.relay.state()["status"], "disabled")

    def test_connection_errors_and_bad_shapes_are_sanitized_and_recover(self):
        for value in ([], {"setup": {}}, {"setup": {"mode": "live"}, "session": []},
                      {"setup": {"mode": "live"}, "session": {"diagnostics": []}}):
            with patch.object(self.relay, "_read", return_value=value):
                self.assertFalse(self.relay.poll_once())
        with patch.object(self.relay, "_read", side_effect=URLError("private URL detail")):
            self.assertFalse(self.relay.poll_once())
        self.assertEqual(self.relay.state()["status"], "error")
        self.assertNotIn("private URL detail", self.relay.state()["error"])
        with patch.object(self.relay, "_read", return_value=source()):
            self.assertTrue(self.relay.poll_once())

    def test_map_uses_bounded_inline_png_and_never_a_remote_filesystem_path(self):
        buffer = io.BytesIO()
        Image.new("L", (2, 2), 205).save(buffer, format="PNG")
        value = source()
        value["map"] = {"image": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(),
                        "width": 2, "height": 2, "resolution": .05, "origin": [0, 0, 0],
                        "source": "/remote/map.yaml"}
        with patch.object(self.relay, "_read", return_value=value):
            self.relay.poll_once()
        self.assertEqual(self.dashboard.map_data["width"], 2)
        self.assertIsNone(self.dashboard.map_path)
        local = {"source": "local selected map"}
        self.dashboard.map_data = local
        self.dashboard.setup["map_yaml"] = "/local/map.yaml"
        self.relay._map_signature = None
        value["session"]["source_sequence"] = 2
        with patch.object(self.relay, "_read", return_value=value):
            self.relay.poll_once()
        self.assertIs(self.dashboard.map_data, local)

    def test_loop_or_closed_dashboard_never_requests_itself(self):
        self.dashboard.setup["telemetry_url"] = "http://127.0.0.1:8766"
        with patch.object(self.relay, "_read") as read:
            self.assertFalse(self.relay.poll_once())
            read.assert_not_called()

    def test_worker_recovers_after_an_unexpected_storage_or_decoder_error(self):
        calls = []

        def poll():
            calls.append(True)
            if len(calls) == 1:
                self.relay.wake.set()
                raise RuntimeError("private database detail")
            self.relay.stop.set()
            self.relay.wake.set()
            return False

        with patch.object(self.relay, "poll_once", side_effect=poll):
            self.relay.thread.start()
            self.relay.thread.join(2)
        self.assertFalse(self.relay.thread.is_alive())
        self.assertEqual(len(calls), 2)
        self.assertNotIn("private database detail", json.dumps(self.relay.state()))

    def test_disabling_upstream_clears_its_map_for_direct_live_input(self):
        self.relay._source_changed(URL)
        self.relay._map_owned = True
        self.dashboard.map_data = {"source": "upstream"}
        self.dashboard.setup["telemetry_url"] = ""
        with patch.object(self.relay, "_read") as read:
            self.assertFalse(self.relay.poll_once())
            read.assert_not_called()
        self.assertIsNone(self.dashboard.map_data)
        self.assertEqual(self.relay.state()["status"], "disabled")
        self.dashboard.closed = True
        with patch.object(self.relay, "_read") as read:
            self.assertFalse(self.relay.poll_once())
            read.assert_not_called()

    def test_slow_lap_download_does_not_block_new_live_samples(self):
        value = source()
        value["analysis"]["laps"] = [{"id": "car:1"}]
        started, release = threading.Event(), threading.Event()

        def read(url):
            if url.endswith("/api/state"):
                return value
            started.set()
            release.wait(2)
            return {"lap": {"id": "car:1"}}

        with patch.object(self.relay, "_read", side_effect=read):
            self.relay.poll_once()
            worker = threading.Thread(target=self.relay.import_one)
            worker.start()
            try:
                self.assertTrue(started.wait(1))
                value["session"]["source_sequence"] = 2
                start = time.monotonic()
                self.assertTrue(self.relay.poll_once())
                self.assertLess(time.monotonic() - start, .5)
            finally:
                release.set()
                worker.join(2)
        self.assertEqual(len(self.dashboard.received), 2)


if __name__ == "__main__":
    unittest.main()
