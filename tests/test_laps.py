"""Lap reliability tests using acquisition samples, without ROS or an LLM."""
import copy
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from racing_dashboard.laps import LapConfig, LapDetector, RouteGeometry, summarize_lap, validate_lap
from racing_dashboard.lap_transport import LapOutbox


ROUTE = [[2*math.cos(i*math.pi/40), 2*math.sin(i*math.pi/40)] for i in range(80)]


def detector(**config):
    return LapDetector(ROUTE, config=LapConfig(gate_x=2, gate_y=0, gate_yaw=math.pi/2, **config))


def sample(angle, stamp):
    return {"time_s": stamp, "pose": [2*math.cos(angle), 2*math.sin(angle), angle+math.pi/2],
            "actual_speed": 2.0, "speed_command": 2.0, "steering": 0.2}


def completed_lap():
    laps = detector()
    result = []
    for i in range(150):
        result += laps.update(sample(-0.4+i*0.05, i*0.05+1))
    return next(lap for lap in result if lap["status"] == "valid")


class LapTests(unittest.TestCase):
    def test_first_crossing_arms_second_times_with_interpolation(self):
        laps = detector()
        bundles = []
        for i in range(145):
            bundles += laps.update(sample(-0.37+i*0.05, i*0.05+1))
        self.assertEqual(len(bundles), 1)
        lap = bundles[0]
        self.assertEqual(lap["status"], "valid")
        self.assertAlmostEqual(lap["lap_time_s"], 2*math.pi, places=3)
        self.assertTrue(lap["samples"][0]["interpolated_gate_crossing"])
        self.assertTrue(lap["samples"][-1]["interpolated_gate_crossing"])
        self.assertGreater(lap["samples"][-1]["progress_m"], 12.0)
        self.assertGreater(lap["metadata"]["ordered_coverage"], 0.99)
        validate_lap(lap)
        # Mutating detector state on the next crossing must not change evidence.
        original = json.dumps(lap, sort_keys=True)
        for i in range(145, 275):
            laps.update(sample(-0.37+i*0.05, i*0.05+1))
        self.assertEqual(json.dumps(lap, sort_keys=True), original)

    def test_starting_midlap_does_not_record_partial_first_lap(self):
        laps = detector()
        bundles = []
        for i in range(180):
            bundles += laps.update(sample(1.4+i*0.05, 1+i*0.05))
        self.assertEqual(bundles, [])
        self.assertTrue(laps.status()["recording"])

    def test_reversing_and_gate_jitter_cannot_create_valid_lap(self):
        laps = detector()
        bundles = []
        for i, angle in enumerate([-0.3, -0.1, 0.1, 0.2, 0.1, -0.1, -0.2, 0.1, -0.1, 0.1]):
            bundles += laps.update(sample(angle, 1+i*0.2))
        self.assertFalse(any(lap["status"] == "valid" for lap in bundles))
        self.assertTrue(any(lap["reason"] == "insufficient_ordered_traversal" for lap in bundles))

    def test_reverse_full_loop_does_not_arm_or_complete(self):
        laps = detector()
        bundles = []
        for i in range(300):
            bundles += laps.update(sample(0.3-i*0.05, 1+i*0.05))
        self.assertEqual(bundles, [])

    def test_gap_jump_clock_reset_and_source_change_invalidate(self):
        cases = [
            (sample(0.3, 4), "ros", "telemetry_gap"),
            (sample(3, 1.5), "ros", "localization_jump"),
            (sample(0.3, 1.1), "ros", "source_clock_reset"),
            (sample(0.3, 1.5), "adapter_monotonic_unstamped", "clock_source_changed"),
        ]
        for next_sample, clock, reason in cases:
            with self.subTest(reason=reason):
                laps = detector()
                for i in range(5):
                    laps.update(sample(-0.3+i*0.1, 1+i*0.1))
                result = laps.update(next_sample, clock_source=clock)
                self.assertEqual(result[0]["status"], "invalid")
                self.assertEqual(result[0]["reason"], reason)
                validate_lap(result[0])
                self.assertFalse(laps.status()["recording"])

    def test_route_change_invalidates_and_open_route_disables(self):
        laps = detector()
        for i in range(5):
            laps.update(sample(-0.3+i*0.1, 1+i*0.1))
        changed = [[x*1.1, y*1.1] for x, y in ROUTE]
        result = laps.set_route(changed)
        self.assertEqual(result[0]["reason"], "route_changed")
        old_hash = result[0]["route_hash"]
        self.assertNotEqual(old_hash, laps.geometry.hash)
        self.assertEqual(laps.set_route(changed, closed=False), [])
        self.assertFalse(laps.status()["enabled"])
        self.assertEqual(laps.update(sample(0, 3)), [])

    def test_far_parallel_gate_crossing_does_not_start_lap(self):
        laps = detector()
        for i in range(10):
            s = sample(-0.3+i*0.1, 1+i*0.1)
            s["pose"][0] += 4
            laps.update(s)
        self.assertFalse(laps.status()["recording"])

    def test_sample_limit_and_corridor_failure_are_explicit(self):
        laps = detector(max_samples=4)
        results = []
        for i in range(12):
            results += laps.update(sample(-0.3+i*0.1, 1+i*0.1))
        self.assertTrue(any(b["reason"] == "sample_limit_exceeded" for b in results))
        laps = detector(max_cross_track_m=0.2)
        for i in range(5):
            laps.update(sample(-0.3+i*0.1, 1+i*0.1))
        off = sample(0.2, 1.5)
        off["pose"][0] += 0.4
        self.assertEqual(laps.update(off)[0]["reason"], "outside_route_corridor")

    def test_validation_rejects_nonfinite_bad_clock_and_modified_route(self):
        lap = completed_lap()
        for mutate in (
            lambda b: b["samples"][2].update(actual_speed=float("nan")),
            lambda b: b["samples"][2].update(time_s=b["start_time_s"]-1),
            lambda b: b["route"][0].__setitem__(0, 99),
            lambda b: b.update(lap_time_s=0),
            lambda b: b["samples"][2].update(scan_hits=[[0, 0]]),
            lambda b: b["metadata"].update(ordered_coverage=0.1),
            lambda b: b["samples"][-1].update(progress_m=0.2),
        ):
            bad = copy.deepcopy(lap)
            mutate(bad)
            with self.assertRaises(ValueError):
                validate_lap(bad)

    def test_nearest_projection_switch_on_parallel_lane_invalidates(self):
        laps = LapDetector([[0, 0], [10, 0], [10, 0.2], [0, 0.2]],
                           config=LapConfig(gate_x=1, gate_y=0, gate_yaw=0))
        for i, xy in enumerate(((0.8, 0), (1.1, 0), (1.4, 0.02), (2, 0.08))):
            laps.update({"time_s": 1+i*0.1, "pose": [*xy, 0]})
        result = laps.update({"time_s": 1.4, "pose": [2, 0.12, 0]})
        self.assertEqual(result[0]["status"], "invalid")
        self.assertEqual(result[0]["reason"], "route_projection_discontinuity")

    def test_self_intersection_branch_switch_cannot_credit_shortcut(self):
        laps = LapDetector([[-2, -2], [2, 2], [-2, 2], [2, -2]],
                           config=LapConfig(gate_x=-1, gate_y=-1, gate_yaw=math.pi/4))
        for i, xy in enumerate(((-1.2, -1.2), (-0.9, -0.9), (-0.5, -0.5), (-0.02, -0.025))):
            laps.update({"time_s": 1+i*0.1, "pose": [*xy, math.pi/4]})
        result = laps.update({"time_s": 1.4, "pose": [-0.02, 0.025, math.pi/4]})
        self.assertEqual(result[0]["status"], "invalid")
        self.assertEqual(result[0]["reason"], "route_projection_discontinuity")

    def test_clean_lap_has_grounded_sector_evidence_and_gate_changes_prevent_comparison(self):
        lap = completed_lap()
        result = summarize_lap(lap)
        self.assertTrue(any(e["id"] == "lap-overview" for e in result["evidence"]))
        self.assertEqual(len([e for e in result["evidence"] if e["kind"] == "sector"]), len(result["sectors"]))
        other = copy.deepcopy(lap)
        other["metadata"]["gate"]["yaw"] += 0.1
        self.assertNotIn("baseline", summarize_lap(lap, other))

    def test_variable_rate_metrics_use_time_weighted_mean(self):
        lap = completed_lap()
        # Long slow interval and many rapid high-speed samples have different
        # sample and time means; trapezoidal integral is independently checked.
        for i, point in enumerate(lap["samples"]):
            point["actual_speed"] = 0 if i < 50 else 3
            point["time_s"] = lap["start_time_s"]+lap["lap_time_s"]*(i/(len(lap["samples"])-1))**1.5
        pairs = list(zip(lap["samples"], lap["samples"][1:]))
        expected = sum((b["time_s"]-a["time_s"])*(a["actual_speed"]+b["actual_speed"])/2 for a, b in pairs)/lap["lap_time_s"]
        self.assertAlmostEqual(summarize_lap(lap)["actual_speed_mps"]["mean"], expected)

    def test_reference_profile_is_interpolated_and_full_profile_stays_out_of_summary(self):
        records = [{"x": x, "y": y, "yaw": math.pi/2+i*math.pi/40, "speed": 1+i/80}
                   for i, (x, y) in enumerate(ROUTE)]
        laps = LapDetector(ROUTE, metadata={"route_profile": records},
                           config=LapConfig(gate_x=2, gate_y=0, gate_yaw=math.pi/2))
        result = []
        for i in range(150):
            result += laps.update(sample(-0.4+i*0.05, 1+i*0.05))
        lap = next(b for b in result if b["status"] == "valid")
        self.assertTrue(all(1 <= s["reference_speed"] <= 2 for s in lap["samples"]))
        summary = summarize_lap(lap)
        self.assertNotIn("route_profile", summary["metadata"])
        self.assertEqual(summary["metadata"]["route_profile_summary"]["waypoint_count"], 80)
        self.assertIsNotNone(summary["reference_speed_mps"])

    def test_summary_locates_measured_slowdown_and_computes_sector_deltas(self):
        base = completed_lap()
        lap = copy.deepcopy(base)
        lap["id"] += "-slow"
        for point in lap["samples"][20:40]:
            point["actual_speed"] = 0.1
        # Stretch acquisition time after a fixed route position by one second.
        for i, point in enumerate(lap["samples"]):
            point["time_s"] += min(1, max(0, (i-20)/20))
        lap["end_time_s"] += 1
        lap["lap_time_s"] += 1
        result = summarize_lap(lap, base)
        self.assertAlmostEqual(result["baseline"]["delta_time_s"], 1)
        self.assertTrue(any(s["delta_time_s"] > 0.1 for s in result["sectors"]))
        self.assertTrue(any(e["kind"] == "slow_motion" and e["start_sample"] == 20 for e in result["evidence"]))
        self.assertTrue(any(e["kind"] == "command_speed_gap" for e in result["evidence"]))
        self.assertFalse(result["baseline"]["configuration_comparable"])


def eventually(test, timeout=3):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if test():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.ack = [False]
        self.fail = [False]
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                bundle = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(bundle)
                if outer.fail[0]:
                    self.send_error(503)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "id": bundle["id"] if outer.ack[0] else "wrong-id"}).encode())
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.directory = tempfile.TemporaryDirectory()
        self.outboxes = []

    def tearDown(self):
        for outbox in self.outboxes:
            outbox.close()
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()

    def outbox(self, **kwargs):
        box = LapOutbox(self.url, self.directory.name, retry_s=0.05, timeout_s=0.3,
                        report=lambda text: None, **kwargs)
        self.outboxes.append(box)
        return box

    def test_missing_ack_retains_evidence_and_restart_retries_same_id(self):
        box = self.outbox()
        lap = completed_lap()
        self.assertTrue(box.submit(lap))
        eventually(lambda: len(self.requests) >= 2)
        self.assertEqual(len(list(Path(self.directory.name).glob("*.json"))), 1)
        box.close()
        self.ack[0] = True
        next_box = self.outbox()
        eventually(lambda: next_box.diagnostics()["delivered"] == 1)
        self.assertEqual(list(Path(self.directory.name).glob("*.json")), [])
        self.assertEqual({b["id"] for b in self.requests}, {lap["id"]})

    def test_network_failures_retain_evidence_until_success(self):
        self.fail[0] = True
        box = self.outbox()
        self.assertTrue(box.submit(completed_lap()))
        eventually(lambda: box.diagnostics()["delivery_failures"] > 0)
        self.assertEqual(len(list(Path(self.directory.name).glob("*.json"))), 1)
        self.fail[0], self.ack[0] = False, True
        eventually(lambda: box.diagnostics()["delivered"] == 1)

    def test_disk_limit_is_reported_and_old_evidence_never_evicted(self):
        box = self.outbox(max_files=1)
        first = completed_lap()
        box.submit(first)
        eventually(lambda: len(list(Path(self.directory.name).glob("*.json"))) == 1)
        other = copy.deepcopy(first)
        other["id"] += "-other"
        box.submit(other)
        eventually(lambda: box.diagnostics()["rejected"] == 1)
        stored = json.loads(next(Path(self.directory.name).glob("*.json")).read_text())
        self.assertEqual(stored["id"], first["id"])


if __name__ == "__main__":
    unittest.main()
