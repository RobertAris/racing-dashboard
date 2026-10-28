"""Conversational context, persistence and model work stay separate from telemetry."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from racing_dashboard.analysis import AnalysisService
from racing_dashboard.llm import CHAT_SCHEMA, LLMConnection, LLMError
from tests.test_analysis import FakeConnection, lap


class ChatConnection(FakeConnection):
    def __init__(self):
        super().__init__()
        self.connected = True
        self.chats = []
        self.chat_started, self.chat_release = threading.Event(), threading.Event()
        self.chat_block = False
        self.failures = 0
        self.lookup = None
        self.lookup_result = None

    def state(self):
        return {**super().state(), "connected": self.connected}

    def chat(self, context, *, retrieve=None, on_start=None):
        if on_start:
            on_start(self.state())
        self.chats.append(copy.deepcopy(context))
        self.chat_started.set()
        if self.chat_block:
            self.chat_release.wait(5)
        if self.failures:
            self.failures -= 1
            raise LLMError("Provider unavailable")
        if self.lookup:
            self.lookup_result = retrieve(*self.lookup)
        return "Try `MAX_SPEED` **3.0 → 3.2 m/s** using the source default as an assumption.\n\n```python\nMAX_SPEED = 3.2\n```"


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = ChatConnection()
        self.service = AnalysisService(self.temp.name, connection=self.connection, active=False)

    def tearDown(self):
        self.connection.chat_release.set()
        self.connection.release.set()
        self.service.close()
        self.temp.cleanup()

    def wait_status(self, status="complete"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = self.service.chat_state()
            if value["status"] == status:
                return value
            threading.Event().wait(.01)
        self.fail("Chat did not reach " + status)

    def ask(self, message="How can I improve the controller?", **extra):
        self.service.send_chat({"message": message, **extra})
        return self.wait_status()

    def test_chat_before_any_lap_any_mode_is_lazy_and_keeps_markdown(self):
        self.assertEqual(self.service.chat_state()["status"], "idle")
        self.assertFalse(self.service.path.exists())
        self.assertFalse(Path(self.temp.name, ".lap_engine.lock").exists())
        state = self.ask()
        self.assertEqual([item["role"] for item in state["messages"]], ["user", "assistant"])
        self.assertIn("```python", state["messages"][-1]["content"])
        self.assertEqual(state["context"]["lap_count"], 0)
        self.assertEqual(self.connection.requests, [])

    def test_new_turn_automatically_refreshes_source_and_retains_previous_messages(self):
        checkout = Path(self.temp.name, "controller")
        checkout.mkdir()
        source = checkout / "controller.py"
        source.write_text("MAX_SPEED = 3.0\n")
        self.service.configure({"source_repo": str(checkout), "source_ref": ""})
        self.ask("What speed cap does the code use?")
        self.assertIn("3.0", self.connection.chats[-1]["source_context"]["files"][0]["content"])
        source.write_text("MAX_SPEED = 3.2\n")
        state = self.ask("I made that change. What next?")
        self.assertEqual(len(state["messages"]), 4)
        self.assertEqual(len(self.connection.chats[-1]["messages"]), 3)
        self.assertIn("3.2", self.connection.chats[-1]["source_context"]["files"][0]["content"])
        self.assertEqual(state["context"]["source"]["mode"], "working_tree")

    def test_catalog_has_all_laps_and_default_best_matches_latest_route(self):
        first = lap(1, duration=10)
        self.service.ingest(first)
        latest = lap(2, duration=20, route=[[0, 0], [20, 0], [20, 20], [0, 20]])
        self.service.ingest(latest)
        self.service.ingest(lap(3, duration=22, route=latest["route"]))
        state = self.ask()
        context = self.connection.chats[-1]
        self.assertEqual(len(context["lap_catalog"]), 3)
        self.assertEqual(state["context"]["lap_count"], 3)
        self.assertEqual(set(state["context"]["detailed_lap_ids"]), {"test-session:2", "test-session:3"})
        self.connection.lookup = ("lap", "test-session:1", "samples:5:3")
        self.ask("Inspect the earlier short route")
        self.assertEqual(self.connection.lookup_result["raw_chunk"]["start_index"], 5)
        self.assertEqual(len(self.connection.lookup_result["raw_chunk"]["values"]), 3)

    def test_explicit_lap_context_persists_on_natural_followup(self):
        self.service.ingest(lap(1))
        self.service.ingest(lap(2))
        self.ask("Why was this lap slower?", lap_id="test-session:1")
        self.ask("Which parameter would help it?")
        self.assertEqual(self.connection.chats[-1]["selected_lap_id"], "test-session:1")
        self.ask("Return to the overall session", lap_id=None)
        self.assertIsNone(self.connection.chats[-1]["selected_lap_id"])

    def test_environment_fallback_supplies_source_and_simulation_parameters(self):
        checkout = Path(self.temp.name, "controller")
        checkout.mkdir()
        (checkout / "controller.py").write_text("MAX_SPEED = 4.0\n")
        self.service.chat_environment = lambda: {"mode": "simulation", "controller_repo": str(checkout),
            "controller_ref": "", "simulation_parameters": {"MAX_SPEED": 3.5}}
        self.ask()
        context = self.connection.chats[-1]
        self.assertEqual(context["environment"]["simulation_parameters"]["MAX_SPEED"], 3.5)
        self.assertEqual(context["source_context"]["files"][0]["path"], "controller.py")

    def test_switching_lab_repositories_refreshes_source_in_the_same_chat(self):
        first = Path(self.temp.name, "lab7-controller")
        second = Path(self.temp.name, "lab8-mppi")
        first.mkdir()
        second.mkdir()
        (first / "controller.py").write_text("MAX_SPEED = 3.0\n")
        (second / "mppi.py").write_text("temperature = 0.4\nrollouts = 1000\n")
        self.service.configure({"source_repo": str(first), "source_ref": ""})
        self.ask("Review this controller's speed settings")
        self.service.configure({"source_repo": str(second), "source_ref": ""})
        state = self.ask("I switched to Lab 8. Review its MPPI tuning parameters")
        source = self.connection.chats[-1]["source_context"]
        self.assertEqual(source["repo_name"], "lab8-mppi")
        self.assertEqual({item["path"] for item in source["files"]}, {"mppi.py"})
        self.assertEqual(state["context"]["source"]["repo_name"], "lab8-mppi")
        self.assertEqual(len(state["messages"]), 4)

    def test_idempotency_retry_and_failed_reply_preserve_one_user_message(self):
        self.connection.failures = 1
        values = {"message": "Inspect the cap", "idempotency_key": "request1"}
        self.service.send_chat(values)
        failed = self.wait_status("failed")
        self.assertEqual(len(failed["messages"]), 1)
        self.assertTrue(self.service.send_chat(values)["duplicate"])
        with self.assertRaises(ValueError):
            self.service.send_chat({**values, "message": "Different question"})
        self.service.retry_chat()
        complete = self.wait_status()
        self.assertEqual(len(complete["messages"]), 2)
        self.assertEqual(self.connection.chats[0], self.connection.chats[1])
        self.assertTrue(self.service.send_chat(values)["duplicate"])

    def test_queue_survives_restart_and_reset_revision_increases(self):
        self.connection.connected = False
        self.service.send_chat({"message": "What should I test next?"})
        queued = self.wait_status("waiting_for_connection")
        self.service.close()
        self.connection.connected = True
        self.service = AnalysisService(self.temp.name, connection=self.connection)
        completed = self.wait_status()
        self.assertEqual(len(self.connection.chats), 1)
        self.assertGreater(completed["revision"], queued["revision"])
        reset = self.service.reset_chat()["chat"]
        self.assertEqual(reset["messages"], [])
        self.assertGreater(reset["revision"], completed["revision"])
        self.assertEqual(len(self.connection.chats), 1)

    def test_slow_model_does_not_block_capture_or_allow_duplicate_work(self):
        self.connection.chat_block = True
        self.service.send_chat({"message": "Analyze the controller"})
        self.assertTrue(self.connection.chat_started.wait(3))
        before = time.monotonic()
        self.service.ingest(lap())
        self.service.state()
        self.service.chat_state()
        self.assertLess(time.monotonic() - before, 1)
        for operation in (lambda: self.service.send_chat({"message": "Another question"}), self.service.reset_chat):
            with self.assertRaises(ValueError):
                operation()
        self.connection.chat_release.set()
        self.wait_status()

    def test_retrieval_rejects_unknown_lap_paths_and_changed_working_file(self):
        checkout = Path(self.temp.name, "controller")
        checkout.mkdir()
        source = checkout / "controller.py"
        source.write_text("MAX_SPEED = 3.0\n")
        selection = {"repo": str(checkout), "ref": "", "hashes": {"controller.py": "old-hash"}}
        result = self.service._chat_retrieve(selection, set(), "lap", "unknown", "")
        self.assertIn("error", result)
        result = self.service._chat_retrieve(selection, set(), "source", "controller.py", "start_line:1")
        self.assertIn("changed", result["error"])
        with self.assertRaises(ValueError):
            self.service._chat_retrieve(selection, set(), "source", "../outside.py", "")

    def test_saved_transcript_not_deleted_when_model_history_is_bounded(self):
        self.connection.connected = False
        self.service.send_chat({"message": "seed"})
        with self.service.lock, self.service._database() as db:
            row = db.execute("SELECT document FROM engineer_chat WHERE id=1").fetchone()
            document = json.loads(row[0])
            document["messages"] = [{"id": str(index), "role": "user" if index % 2 == 0 else "assistant",
                                     "content": "older message " + str(index)} for index in range(204)]
            db.execute("UPDATE engineer_chat SET status='complete',document=? WHERE id=1", (json.dumps(document),))
        self.connection.connected = True
        state = self.ask("Continue our conversation")
        self.assertEqual(len(state["messages"]), 206)
        self.assertEqual(len(self.connection.chats[-1]["messages"]), 30)
        self.assertEqual(state["context"]["history_omitted_messages"], 175)


class ChatProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = LLMConnection(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_one_normal_reply_preserves_markdown_and_existing_transport(self):
        with patch.object(self.connection, "_structured_response", return_value={"message": "Use `3.2 m/s`.", "requests": []}) as request:
            result = self.connection.chat({"messages": [{"role": "user", "content": "Suggest a cap"}]})
        self.assertEqual(result, "Use `3.2 m/s`.")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs["schema"], CHAT_SCHEMA)
        self.assertNotIn("tools", request.call_args.kwargs)

    def test_model_can_retrieve_omitted_file_then_answer(self):
        responses = [{"message": "", "requests": [{"kind": "source", "identifier": "mpc/helpers/profile.py", "query": "start_line:10"}]},
                     {"message": "Change the acceleration clamp at `mpc/helpers/profile.py:10`.", "requests": []}]
        contexts = []
        def respond(context, **kwargs):
            contexts.append(copy.deepcopy(context))
            return responses.pop(0)
        retrieve = unittest.mock.Mock(return_value={"path": "mpc/helpers/profile.py", "content": "speed = min(speed, cap)"})
        with patch.object(self.connection, "_structured_response", side_effect=respond):
            answer = self.connection.chat({"messages": []}, retrieve=retrieve)
        retrieve.assert_called_once_with("source", "mpc/helpers/profile.py", "start_line:10")
        self.assertIn("profile.py:10", answer)
        self.assertEqual(contexts[1]["retrieved_context"][0]["result"]["content"], "speed = min(speed, cap)")

    def test_actual_api_and_compatible_payloads_use_saved_provider_without_native_tools(self):
        reply = {"message": "Try `MAX_SPEED = 3.2` as one experiment.", "requests": []}
        for provider in ("openai", "compatible"):
            with self.subTest(provider=provider):
                settings = {"provider": provider, "api_key": "fixture-key", "reasoning_effort": ""}
                if provider == "compatible":
                    settings["base_url"] = "http://127.0.0.1:11434/v1"
                self.connection.configure(settings)
                response = {"status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps(reply)}]}]} if provider == "openai" else {
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(reply)}}]}
                with patch.object(self.connection, "_request", return_value=response) as request:
                    answer = self.connection.chat({"messages": [{"role": "user", "content": "Recommend a change"}]})
                self.assertEqual(answer, reply["message"])
                payload = request.call_args.kwargs["payload"]
                self.assertNotIn("tools", payload)
                self.assertNotIn("fixture-key", json.dumps(payload))
                self.assertTrue(request.call_args.args[0].endswith("/responses" if provider == "openai" else "/chat/completions"))

    def test_invalid_or_unbounded_model_retrieval_is_rejected(self):
        for result in ({"message": "", "requests": []}, {"message": "Hi", "requests": [{"kind": "shell", "identifier": "", "query": "rm"}]}):
            with self.subTest(result=result), patch.object(self.connection, "_structured_response", return_value=result), self.assertRaises(LLMError):
                self.connection.chat({}, retrieve=lambda *args: {})
        repeated = {"message": "", "requests": [{"kind": "lap", "identifier": "a", "query": ""}]}
        with patch.object(self.connection, "_structured_response", return_value=repeated) as request, self.assertRaises(LLMError):
            self.connection.chat({}, retrieve=lambda *args: {})
        self.assertEqual(request.call_count, 4)

    def test_connection_changes_between_retrieval_rounds_abort_chat(self):
        result = {"message": "", "requests": [{"kind": "lap", "identifier": "a", "query": ""}]}
        def retrieve(*args):
            self.connection._generation += 1
            return {}
        with patch.object(self.connection, "_structured_response", return_value=result) as request, self.assertRaises(LLMError):
            self.connection.chat({}, retrieve=retrieve)
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
