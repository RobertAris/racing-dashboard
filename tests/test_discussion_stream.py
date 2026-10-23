"""Exercise saved-turn retry through the real streaming and validation pipeline."""
import io
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from racing_dashboard.analysis import AnalysisService
from racing_dashboard.llm import API, LLMConnection
from tests.test_analysis import lap
from tests.test_discussion_service import answer


class DiscussionStreamTests(unittest.TestCase):
    def test_retry_preserves_question_and_accepts_final_json_after_commentary(self):
        requests = []

        def provider(request, *, timeout):
            # Intercept the HTTP boundary: no external provider receives a request.
            self.assertEqual(request.full_url, API + "/responses")
            payload = json.loads(request.data)
            self.assertEqual(payload["model"], "gpt-6.1-sol")
            self.assertEqual(payload["reasoning"], {"effort": "high"})
            self.assertTrue(payload["text"]["format"]["strict"])
            context = json.loads(payload["input"][0]["content"])
            requests.append(context)
            text = "invalid JSON" if len(requests) == 1 else json.dumps(answer(context))
            output = [
                {"id": "reasoning", "type": "reasoning", "summary": []},
                {"id": "progress", "type": "message", "role": "assistant", "status": "completed",
                 "phase": "commentary", "content": [{"type": "output_text", "text": "I will inspect this lap."}]},
                {"id": "final", "type": "message", "role": "assistant", "status": "completed",
                 "phase": "final_answer", "content": [{"type": "output_text", "text": text}]},
            ]
            events = [
                {"type": "response.output_text.delta", "item_id": "progress", "delta": "I will inspect this lap."},
                {"type": "response.output_item.done", "output_index": 2, "item": output[-1]},
                {"type": "response.completed", "response": {"status": "completed", "output": output}},
            ]
            return io.BytesIO("".join("data: " + json.dumps(event) + "\n\n" for event in events).encode())

        with tempfile.TemporaryDirectory() as directory, patch("racing_dashboard.llm.build_opener") as opener:
            opener.return_value.open.side_effect = provider
            connection = LLMConnection(directory)
            connection.configure({"provider": "openai", "api_key": "test-only-key"})
            service = AnalysisService(directory, connection=connection, active=True)
            try:
                item = lap()
                service.ingest(item)

                def wait_for(status):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        discussion = service.get(item["id"])["discussion"]
                        if discussion["status"] == status:
                            return discussion
                        threading.Event().wait(.01)
                    self.fail("Discussion did not reach " + status)

                question = "How can I reduce oscillations while maintaining lap time?"
                service.discuss(item["id"], {"message": question})
                failed = wait_for("failed")
                self.assertIn("not valid JSON", failed["error"])
                self.assertEqual(failed["attempts"], 1)
                self.assertEqual(len(requests), 1)
                self.assertEqual(len(failed["messages"]), 1)

                service.retry_discussion(item["id"])
                replied = wait_for("idle")
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[0], requests[1])
                self.assertIsNone(replied["error"])
                self.assertEqual(replied["messages"][0], failed["messages"][0])
                self.assertEqual(replied["messages"][0]["content"], question)
                self.assertEqual(replied["messages"][1]["response"]["questions"][0]["key"], "MAX_SPEED")
                self.assertEqual(replied["messages"][1]["model"], "gpt-6.1-sol")
                self.assertEqual(replied["confirmations"], [])

                service.discuss(item["id"], {"message": "I checked this lap's speed cap.",
                    "confirmations": [{"key": "MAX_SPEED", "value": "3.0"}]})
                confirmed = wait_for("idle")
                self.assertEqual(len(requests), 3)
                proposal = confirmed["messages"][-1]["response"]["proposals"][0]
                self.assertEqual((proposal["current_value"], proposal["proposed_value"]), ("3.0", "3.2"))
                self.assertEqual(len(confirmed["messages"]), 4)
                self.assertNotIn("I will inspect this lap.", json.dumps(service.export(item["id"])))
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
