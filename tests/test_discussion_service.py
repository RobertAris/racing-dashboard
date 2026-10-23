"""Durable manual discussions stay independent of live telemetry and reports."""
import copy
import json
import tempfile
import threading
import time
import unittest

from racing_dashboard.analysis import AnalysisService
from racing_dashboard.llm import LLMError
from tests.test_analysis import FakeConnection, lap


def answer(context):
    facts = {item["key"]: item["value"] for item in context["confirmations"]}
    turn = {"message": "Confirm the cap used for this recorded lap.", "questions": [], "proposals": [], "limitations": []}
    if "MAX_SPEED" not in facts:
        turn["questions"] = [{"id": "cap", "key": "MAX_SPEED", "question": "What speed cap was active?",
                              "suggested_answer": "3.0", "reason": "Source defaults are not active settings."}]
    else:
        turn["message"] = "Test one speed-cap change and compare later laps."
        turn["proposals"] = [{"id": "cap-trial", "kind": "parameter", "title": "Compare a modest cap change",
            "rationale": "The original lap provides measured evidence.",
            "evidence_ids": [context["metrics"]["evidence"][0]["id"]], "confirmation_keys": ["MAX_SPEED"],
            "parameter": "MAX_SPEED", "current_value": facts["MAX_SPEED"], "proposed_value": "3.2",
            "unit": "m/s", "source_file": "", "source_commit": "", "diff": "",
            "validation": "Compare sector times and route deviation under the same conditions.",
            "rollback": "Restore the confirmed cap if tracking worsens."}]
    return turn


class DiscussionConnection(FakeConnection):
    def __init__(self):
        super().__init__()
        self.discussions = []
        self.connected = True
        self.discussion_started, self.discussion_release = threading.Event(), threading.Event()
        self.discussion_block = False
        self.failures = 0
        self.invalid = False

    def state(self):
        return {**super().state(), "connected": self.connected}

    def discuss(self, context, *, on_start=None):
        if on_start:
            on_start(self.state())
        self.discussions.append(copy.deepcopy(context))
        self.discussion_started.set()
        if self.discussion_block:
            self.discussion_release.wait(5)
        if self.failures:
            self.failures -= 1
            raise LLMError("Provider temporarily unavailable", retryable=True)
        result = answer(context)
        if self.invalid:
            result["proposals"] = answer({**context, "confirmations": [{"key": "MAX_SPEED", "value": "3.0"}]})["proposals"]
        return result


class DiscussionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = DiscussionConnection()
        self.service = AnalysisService(self.temp.name, connection=self.connection, active=True)
        self.item = lap()
        self.service.ingest(self.item)

    def tearDown(self):
        self.connection.discussion_release.set()
        self.connection.release.set()
        self.service.close()
        self.temp.cleanup()

    def wait_reply(self, identifier=None, expected="idle"):
        identifier = identifier or self.item["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            discussion = self.service.get(identifier)["discussion"]
            if discussion["status"] == expected:
                return discussion
            threading.Event().wait(.01)
        self.fail("Discussion did not reach " + expected)

    def send(self, **values):
        self.service.discuss(self.item["id"], {"message": "Discuss the slow sectors", **values})
        return self.wait_reply()

    def test_manual_question_answer_numeric_proposal_and_immutable_report(self):
        original = self.service.get(self.item["id"])
        first = self.send()
        self.assertEqual(first["messages"][-1]["response"]["questions"][0]["key"], "MAX_SPEED")
        self.assertEqual(first["confirmations"], [])
        second = self.send(message="This cap was active for this lap", confirmations=[{"key": "MAX_SPEED", "value": "3.0"}])
        self.assertEqual(second["confirmations"][0]["origin"], "user_confirmed")
        self.assertEqual(second["confirmations"][0]["lap_id"], self.item["id"])
        self.assertEqual(second["messages"][-1]["response"]["proposals"][0]["proposed_value"], "3.2")
        self.assertEqual(len(second["messages"]), 4)
        self.assertEqual(self.connection.requests, [])  # automatic analysis remains paused
        self.assertNotIn("samples", self.connection.discussions[-1]["lap"])
        after = self.service.get(self.item["id"])
        for key in ("lap", "metrics", "report", "analysis_status", "source_context"):
            self.assertEqual(after[key], original[key])

    def test_idempotency_deduplicates_retry_and_rejects_different_payload(self):
        values = {"message": "Ask about the cap", "idempotency_key": "request-1"}
        self.service.discuss(self.item["id"], values)
        self.wait_reply()
        self.assertTrue(self.service.discuss(self.item["id"], values)["duplicate"])
        self.assertEqual(len(self.connection.discussions), 1)
        with self.assertRaisesRegex(ValueError, "different message"):
            self.service.discuss(self.item["id"], {**values, "message": "Another question"})

    def test_slow_discussion_does_not_block_capture_or_report_worker(self):
        self.connection.discussion_block = True
        self.service.discuss(self.item["id"], {"message": "Explain the cap"})
        self.assertTrue(self.connection.discussion_started.wait(2))
        started = time.monotonic()
        self.service.ingest(lap(2))
        self.service.state()
        self.assertLess(time.monotonic() - started, .5)
        self.service.configure({"enabled": True})
        self.assertTrue(self.connection.started.wait(2))
        self.assertEqual(self.service.get(self.item["id"])["discussion"]["status"], "analyzing")
        with self.assertRaisesRegex(ValueError, "already pending"):
            self.service.discuss(self.item["id"], {"message": "Duplicate concurrent request"})
        self.connection.discussion_release.set()
        self.wait_reply()

    def test_disconnected_queue_and_restart_recover_one_pending_turn(self):
        self.connection.connected = False
        self.service.discuss(self.item["id"], {"message": "Discuss after sign-in"})
        self.assertEqual(self.service.state()["laps"][0]["discussion_status"], "waiting_for_connection")
        self.assertEqual(self.connection.discussions, [])
        with self.service.lock, self.service._database() as db:
            db.execute("UPDATE lap_discussions SET status='analyzing' WHERE lap_id=?", (self.item["id"],))
        self.service.close()
        self.service = AnalysisService(self.temp.name, connection=self.connection, active=False)
        self.assertEqual(self.service.get(self.item["id"])["discussion"]["status"], "queued")
        self.connection.connected = True
        self.service.set_active(True)
        discussion = self.wait_reply()
        self.assertEqual(len(discussion["messages"]), 2)
        self.assertEqual(len(self.connection.discussions), 1)

    def test_confirmations_are_replaced_removed_and_never_transferred_to_other_laps(self):
        self.send(confirmations=[{"key": "MAX_SPEED", "value": "3.0"}])
        changed = self.send(confirmations=[{"key": "MAX_SPEED", "value": "2.8"}])
        self.assertEqual(len(changed["confirmations"]), 1)
        self.assertEqual(changed["confirmations"][0]["value"], "2.8")
        removed = self.send(remove_confirmations=["MAX_SPEED"])
        self.assertEqual(removed["confirmations"], [])
        self.assertEqual(removed["messages"][-1]["response"]["proposals"], [])
        self.service.ingest(lap(2))
        self.assertEqual(self.service.get("test-session:2")["discussion"]["confirmations"], [])
        self.assertEqual(removed["messages"][1]["confirmation_snapshot"][0]["value"], "3.0")

    def test_compare_later_lap_passes_measured_deltas_without_transferring_facts(self):
        self.service.ingest(lap(2, 19))
        self.send(confirmations=[{"key": "MAX_SPEED", "value": "3.0"}], comparison_lap_id="test-session:2")
        context = self.connection.discussions[-1]
        self.assertEqual(context["comparison"]["delta_time_s"], -1)
        self.assertFalse(context["comparison"]["configuration_verified"])
        self.assertNotIn("evidence", context["comparison"]["metrics"])
        self.assertEqual(context["confirmations"][0]["lap_id"], self.item["id"])
        self.send(comparison_lap_id="")
        self.assertIsNone(self.connection.discussions[-1]["comparison"])

    def test_bad_comparison_and_input_are_atomic(self):
        other = lap(2)
        other["metadata"]["map_hash"] = "different"
        self.service.ingest(other)
        for values in ({"message": "Compare", "comparison_lap_id": other["id"]},
                       {"message": "Compare", "comparison_lap_id": self.item["id"]},
                       {"message": "Confirm", "confirmations": [{"key": "MAX_SPEED", "value": 3}]},
                       {"message": "x" * 6001}, {"message": ""},
                       {"message": "hello", "confirmations": [{"key": "MAX_SPEED", "value": "3", "origin": "verified"}]}):
            with self.assertRaises(ValueError):
                self.service.discuss(self.item["id"], values)
        self.assertEqual(self.service.get(self.item["id"])["discussion"]["messages"], [])
        self.assertEqual(self.connection.discussions, [])

    def test_invalid_model_proposal_fails_once_and_saved_turn_can_be_retried(self):
        self.connection.invalid = True
        self.service.discuss(self.item["id"], {"message": "Ask, do not assume settings"})
        self.wait_reply(expected="failed")
        self.assertEqual(len(self.connection.discussions), 1)
        self.assertEqual(len(self.service.get(self.item["id"])["discussion"]["messages"]), 1)
        self.connection.invalid = False
        self.service.retry_discussion(self.item["id"])
        reply = self.wait_reply()
        self.assertEqual(len(reply["messages"]), 2)
        self.assertEqual(len(self.connection.discussions), 2)

    def test_temporary_failures_retry_without_duplicate_messages(self):
        self.connection.failures = 1
        result = self.send()
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(len(self.connection.discussions), 2)

    def test_live_mode_valid_lap_and_missing_resources_are_enforced(self):
        self.service.set_active(False)
        with self.assertRaisesRegex(ValueError, "Live car"):
            self.send()
        self.service.set_active(True)
        invalid = lap(2)
        invalid["status"], invalid["reason"] = "invalid", "sample_gap"
        self.service.ingest(invalid)
        with self.assertRaisesRegex(ValueError, "valid full lap"):
            self.service.discuss(invalid["id"], {"message": "Tune this"})
        with self.assertRaises(KeyError):
            self.service.discuss("missing", {"message": "Tune this"})
        with self.assertRaises(ValueError):
            self.service.retry_discussion(self.item["id"])

    def test_export_includes_conversation_and_source_stays_pinned(self):
        source = {"commit": "a" * 40, "files": [{"path": "mpc/scripts/mpc_node.py", "content": "x = 1\n"}]}
        self.service.source = source
        self.send()
        source["files"][0]["content"] = "x = 999\n"
        self.service.source = {"commit": "b" * 40, "files": []}
        self.send()
        self.assertEqual(self.connection.discussions[-1]["source_context"]["commit"], "a" * 40)
        self.assertEqual(self.connection.discussions[-1]["source_context"]["files"][0]["content"], "x = 1\n")
        exported = self.service.export(self.item["id"])
        self.assertEqual(len(exported["discussion"]["messages"]), 4)
        self.assertNotIn("pending_context", json.dumps(exported))
        self.service.close()
        self.service = AnalysisService(self.temp.name, connection=self.connection, active=False)
        self.assertEqual(self.service.get(self.item["id"])["discussion"]["messages"], exported["discussion"]["messages"])

    def test_retention_keeps_failed_turn_for_retry_then_cascades_finished_discussion(self):
        self.service.retention = 1
        with self.service.lock, self.service._database() as db:
            db.execute("UPDATE laps SET analysis_status='complete' WHERE id=?", (self.item["id"],))
            self.service._refresh(db)
        self.connection.invalid = True
        self.service.discuss(self.item["id"], {"message": "Ask before proposing"})
        self.wait_reply(expected="failed")
        with self.assertRaisesRegex(ValueError, "backlog"):
            self.service.ingest(lap(2))
        self.assertEqual(len(self.service.get(self.item["id"])["discussion"]["messages"]), 1)
        self.connection.invalid = False
        self.service.retry_discussion(self.item["id"])
        self.wait_reply()
        self.service.ingest(lap(2))
        with self.assertRaises(KeyError):
            self.service.get(self.item["id"])
        with self.service.lock, self.service._database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM lap_discussions").fetchone()[0], 0)

    def test_saved_comparison_survives_other_lap_retention(self):
        self.service.ingest(lap(2, 19))
        self.service.discuss("test-session:2", {"message": "Compare with the earlier baseline", "comparison_lap_id": self.item["id"]})
        self.wait_reply("test-session:2")
        with self.service.lock, self.service._database() as db:
            db.execute("UPDATE laps SET analysis_status='complete'")
            self.service._refresh(db)
        self.service.retention = 2
        self.service.ingest(lap(3))
        with self.assertRaises(KeyError):
            self.service.get(self.item["id"])
        self.service.discuss("test-session:2", {"message": "Recheck the recorded comparison"})
        self.wait_reply("test-session:2")
        comparison = self.connection.discussions[-1]["comparison"]
        self.assertEqual(comparison["lap"]["id"], self.item["id"])
        self.assertEqual(comparison["lap"]["lap_time_s"], 20)
        self.assertEqual(comparison["delta_time_s"], 1)


if __name__ == "__main__":
    unittest.main()
