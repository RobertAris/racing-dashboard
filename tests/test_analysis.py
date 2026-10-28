"""Durability, isolation and report grounding across the live analysis pipeline."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from racing_dashboard.analysis import AnalysisService, validate_report
from racing_dashboard.laps import RouteGeometry
from racing_dashboard.recording import validate_recording
from racing_dashboard.source_context import read_source_context


def lap(number=1, duration=20, route=None):
    route = route or [[0, 0], [10, 0], [10, 10], [0, 10]]
    geometry = RouteGeometry(route)
    samples = []
    for i in range(161):
        progress = geometry.length * i / 160
        edge = min(3, int(progress / (geometry.length / 4)))
        a, b = route[edge], route[(edge + 1) % 4]
        fraction = progress / (geometry.length / 4) - edge
        samples.append({"time_s": (number - 1) * duration + duration * i / 160,
                        "pose": [a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction, 0],
                        "progress_m": progress, "cross_track_error": .05,
                        "actual_speed": geometry.length / duration, "speed_command": geometry.length / duration,
                        "steering": .01})
    return {"id": f"test-session:{number}", "source_session_id": "test-session", "lap_number": number,
            "status": "valid", "start_time_s": samples[0]["time_s"], "end_time_s": samples[-1]["time_s"],
            "lap_time_s": duration, "route_hash": geometry.hash, "route": route,
            "metadata": {"map_frame": "map", "route_closed": True, "ordered_coverage": 1.0,
                         "clock_source": "ros", "pose_offset": 0}, "samples": samples}


def report(evidence_id):
    return {"summary": "Measured sector comparison is available.", "findings": [
        {"title": "Inspect the reference speed profile", "observation": "The cited interval provides a baseline.",
         "hypothesis": "A configuration limit may explain the speed profile; effective settings are unknown.",
         "confidence": "low", "evidence_ids": [evidence_id],
         "recommendation": "Verify active speed settings before changing them.",
         "validation": "Compare several laps under the same conditions."}],
        "limitations": ["Solver diagnostics are unavailable."]}


class FakeConnection:
    def __init__(self):
        self.requests = []
        self.started, self.release = threading.Event(), threading.Event()
        self.block = False
        self.bad_reference = False

    def state(self):
        return {"provider": "chatgpt", "model": "gpt-6-sol", "reasoning_effort": "high", "connected": True}

    def configure(self, settings):
        return self.state()

    def analyze(self, evidence, *, on_start=None):
        if on_start:
            on_start(self.state())
        self.requests.append(copy.deepcopy(evidence))
        self.started.set()
        if self.block:
            self.release.wait(5)
        return report("invented-evidence" if self.bad_reference else evidence["metrics"]["evidence"][0]["id"])


class AnalysisTests(unittest.TestCase):
    def test_import_preserves_capture_source_without_replacing_selected_source(self):
        selected = {"status": "local_source", "commit": "current-selection"}
        self.service.source = copy.deepcopy(selected)
        captured = {"status": "local_source", "commit": "original-capture",
                    "files": [{"path": "controller.py", "content": "gain = 1.0\n"}]}
        original = copy.deepcopy(captured)
        item = lap()
        self.service.ingest(item, source_context=captured)
        captured["commit"] = "changed-after-import"
        self.assertEqual(self.service.get(item["id"])["source_context"], original)
        self.assertEqual(self.service.source, selected)
        self.assertTrue(self.service.ingest(item, source_context={})["duplicate"])
        self.assertEqual(self.service.get(item["id"])["source_context"], original)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = FakeConnection()
        self.service = AnalysisService(self.temp.name, connection=self.connection)

    def tearDown(self):
        self.connection.release.set()
        self.service.close()
        self.temp.cleanup()

    def wait_status(self, identifier, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.service.get(identifier)["analysis_status"] == status:
                return
            threading.Event().wait(.01)
        self.fail("Analysis did not reach " + status)

    def test_no_files_or_inference_until_a_lap_or_configuration(self):
        self.assertFalse(self.service.path.exists())
        self.assertEqual(self.service.state()["laps"], [])
        self.assertEqual(self.connection.requests, [])

    def test_duplicate_ack_and_restart_preserve_immutable_evidence(self):
        first = lap()
        self.service.source = {"commit": "original", "deployment_verified": False}
        self.service.ingest(first)
        first["samples"][0]["pose"][0] = 999
        self.assertTrue(self.service.ingest(lap())["duplicate"])
        self.assertNotEqual(self.service.get(first["id"])["lap"]["samples"][0]["pose"][0], 999)
        self.service.close()
        self.service = AnalysisService(self.temp.name, connection=self.connection)
        self.assertEqual(self.service.get(first["id"])["source_context"]["commit"], "original")
        self.assertEqual(len(self.service.state()["laps"]), 1)

    def test_live_mode_and_explicit_enable_gate_model_requests(self):
        item = lap()
        self.service.ingest(item)
        self.service.set_active(True)
        threading.Event().wait(.05)
        self.assertEqual(self.connection.requests, [])
        self.service.set_active(False)
        self.service.configure({"enabled": True})
        threading.Event().wait(.05)
        self.assertEqual(self.connection.requests, [])
        self.service.set_active(True)
        self.wait_status(item["id"], "complete")
        self.assertEqual(len(self.connection.requests), 1)
        self.assertEqual(self.service.get(item["id"])["model"], "gpt-6-sol")
        self.assertNotIn("samples", self.connection.requests[0]["lap"])

    def test_slow_model_does_not_block_lap_ingestion_or_state(self):
        self.connection.block = True
        self.service.configure({"enabled": True})
        self.service.set_active(True)
        self.service.ingest(lap())
        self.assertTrue(self.connection.started.wait(3))
        before = time.monotonic()
        self.service.ingest(lap(2))
        self.assertEqual(self.service.state()["pending"], 2)
        self.assertLess(time.monotonic() - before, 1)
        self.service.set_active(False)
        self.connection.release.set()
        self.wait_status("test-session:1", "complete")
        self.assertEqual(self.service.get("test-session:2")["analysis_status"], "queued")

    def test_unknown_model_evidence_fails_without_repeated_paid_calls(self):
        self.connection.bad_reference = True
        self.service.ingest(lap())
        self.service.configure({"enabled": True})
        self.service.set_active(True)
        self.wait_status("test-session:1", "failed")
        self.assertEqual(len(self.connection.requests), 1)
        self.assertIsNone(self.service.get("test-session:1")["report"])

    def test_invalid_lap_is_preserved_but_never_analyzed(self):
        item = lap()
        item["status"], item["reason"] = "invalid", "telemetry_gap"
        self.service.ingest(item)
        self.service.configure({"enabled": True})
        self.service.set_active(True)
        self.assertEqual(self.service.get(item["id"])["analysis_status"], "invalid")
        with self.assertRaises(ValueError):
            self.service.retry(item["id"])

    def test_baselines_require_matching_map_and_pose_reference(self):
        self.service.ingest(lap())
        second = lap(2, 22)
        self.service.ingest(second)
        self.assertEqual(self.service.get(second["id"])["metrics"]["baseline"]["lap_id"], "test-session:1")
        third = lap(3)
        third["metadata"]["map_frame"] = "other-map"
        self.service.ingest(third)
        self.assertNotIn("baseline", self.service.get(third["id"])["metrics"])

    def test_exports_replay_and_queue_backpressure(self):
        self.service.retention = 1
        self.service.ingest(lap())
        validate_recording(self.service.export("test-session:1", recording=True))
        self.assertEqual(self.service.export("test-session:1")["format"], "robertaris-lap-analysis")
        with self.assertRaisesRegex(ValueError, "backlog"):
            self.service.ingest(lap(2))
        self.assertTrue(self.service.ingest(lap())["duplicate"])

    def test_fastest_baseline_uses_matching_pose_reference_and_gate(self):
        faster = lap(1, 18)
        faster["metadata"]["pose_offset_m"] = .2
        self.service.ingest(faster)
        matching = lap(2, 20)
        matching["metadata"]["pose_offset_m"] = 0
        self.service.ingest(matching)
        current = lap(3, 22)
        current["metadata"]["pose_offset_m"] = 0
        self.service.ingest(current)
        self.assertEqual(self.service.get(current["id"])["metrics"]["baseline"]["lap_id"], matching["id"])

    def test_retryable_provider_failure_is_bounded_and_then_completes(self):
        from racing_dashboard.llm import LLMError
        original = self.connection.analyze
        remaining = [True]
        def flaky(evidence, **kwargs):
            if remaining:
                remaining.pop()
                error = LLMError("Temporary provider outage")
                error.retryable = True
                raise error
            return original(evidence, **kwargs)
        self.connection.analyze = flaky
        self.service.ingest(lap())
        self.service.configure({"enabled": True})
        self.service.set_active(True)
        self.wait_status("test-session:1", "complete")
        self.assertEqual(self.service.get("test-session:1")["attempts"], 2)

    def test_report_evidence_membership_is_enforced(self):
        with self.assertRaises(ValueError):
            validate_report(report("fake"), [{"id": "real"}])


class SourceContextTests(unittest.TestCase):
    def test_source_is_pinned_read_only_and_never_claimed_deployed(self):
        with tempfile.TemporaryDirectory() as folder:
            def run(command, **kwargs):
                from subprocess import CompletedProcess
                if "rev-parse" in command:
                    content = b"0123456789abcdef\n"
                elif "ls-tree" in command:
                    content = b"100644 blob aaaa 41\tcontroller/main.py\0"
                elif "symbolic-ref" in command:
                    content = b"main\n"
                else:
                    content = b"raise RuntimeError('must never execute')\n"
                return CompletedProcess(command, 0, content, b"")
            with patch("racing_dashboard.source_context.subprocess.run", side_effect=run):
                context = read_source_context(folder, "origin/controller-experiment")
            self.assertEqual(context["commit"], "0123456789abcdef")
            self.assertFalse(context["deployment_verified"])
            self.assertEqual(context["mode"], "git_revision")
            self.assertEqual(context["ref"], "origin/controller-experiment")
            self.assertEqual(context["files"][0]["path"], "controller/main.py")
            self.assertEqual(context["files"][0]["content"], "raise RuntimeError('must never execute')\n")
            with self.assertRaises(ValueError):
                read_source_context(folder, "--output=/tmp/bad")

    def test_git_timeout_is_a_recoverable_configuration_error(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            with patch("racing_dashboard.source_context.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
                with self.assertRaisesRegex(ValueError, "timed out"):
                    read_source_context(folder)


class HTTPAnalysisTests(unittest.TestCase):
    def setUp(self):
        from racing_dashboard.server import Dashboard, make_server
        self.temp = tempfile.TemporaryDirectory()
        self.dashboard = Dashboard({"mode": "live"}, self.temp.name)
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

    def request(self, path, body=None, headers=None):
        from urllib.request import Request, urlopen
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", **(headers or {})})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_live_lap_http_archive_survives_recording_reset_and_replay(self):
        item = lap()
        ack = self.request("/api/laps", item)
        self.assertEqual(ack["id"], item["id"])
        self.assertTrue(self.request("/api/laps", item)["duplicate"])
        self.dashboard._new_recording()
        detail = self.request("/api/laps/" + item["id"])
        self.assertEqual(detail["lap"]["lap_time_s"], 20)
        recording = self.request("/api/laps/" + item["id"] + "/recording")
        validate_recording(recording)
        self.dashboard.load_replay(recording)
        self.assertFalse(self.request("/api/state")["analysis"]["active"])
        self.assertEqual(len(self.request("/api/state")["analysis"]["laps"]), 1)

    def test_private_connection_is_not_bundled_or_exported(self):
        settings = {"provider": "openai", "model": "gpt-6-sol", "reasoning_effort": "high",
                    "api_key": "test-private-key-never-export", "enabled": False}
        self.request("/api/analysis/settings", settings)
        self.request("/api/laps", lap())
        public = self.request("/api/state")
        exported = self.request("/api/laps/test-session:1/export")
        self.assertNotIn(settings["api_key"], json.dumps([public, exported]))
        self.assertTrue(public["analysis"]["connection"]["api_key_set"])
        self.assertEqual(self.request("/api/laps/test-session:1")["analysis_status"], "queued")

    def test_host_and_cross_origin_cannot_trigger_model_operations(self):
        from urllib.error import HTTPError
        for headers in ({"Host": "untrusted.example"}, {"Origin": "https://untrusted.example"}):
            with self.assertRaises(HTTPError) as error:
                self.request("/api/analysis/connect", {}, headers)
            self.assertEqual(error.exception.code, 403)

    def test_chatgpt_connect_uses_canonical_loopback_callback(self):
        with patch.object(self.dashboard.analysis.connection, "auth_start", return_value={"url": "https://auth.openai.com/example"}) as begin:
            response = self.request("/api/analysis/connect", {})
        self.assertTrue(response["url"].startswith("https://auth.openai.com/"))
        begin.assert_called_once_with(self.url + "/auth/callback")


if __name__ == "__main__":
    unittest.main()
