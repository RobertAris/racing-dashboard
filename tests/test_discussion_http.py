"""Discussion HTTP boundaries and persisted human-in-the-loop workflow."""
import copy
import json
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from racing_dashboard.server import Dashboard, make_server
from racing_dashboard.analysis import AnalysisService
from tests.test_analysis import lap, report


PATCH = ("--- a/mpc/launch/levine_launch.py\n+++ b/mpc/launch/levine_launch.py\n"
         "@@ -1 +1 @@\n-PARAMETERS = {'MAX_SPEED': 3.0}\n+PARAMETERS = {'MAX_SPEED': 3.1}\n")


class RoutingAnalysis:
    """Route spy keeps malformed HTTP cases independent of model processing."""
    def __init__(self):
        self.calls = []

    def discuss(self, identifier, request):
        self.calls.append(("discuss", identifier, request))
        if identifier == "missing":
            raise KeyError(identifier)
        if request == {"invalid": True}:
            raise ValueError("Invalid discussion request")
        return {"ok": True, "id": identifier, "discussion": {"status": "queued"}}

    def retry_discussion(self, identifier):
        self.calls.append(("retry", identifier))
        if identifier == "missing":
            raise KeyError(identifier)
        return {"ok": True, "id": identifier, "discussion": {"status": "queued"}}

    def attach_discussion_source(self, identifier):
        self.calls.append(("source", identifier))
        if identifier == "missing":
            raise KeyError(identifier)
        return {"ok": True, "id": identifier,
                "discussion": {"status": "idle", "source_origin": "attached_after_discussion_start"}}

    def discussion_patch(self, identifier, message_id, proposal_id):
        self.calls.append(("patch", identifier, message_id, proposal_id))
        if "missing" in (identifier, message_id, proposal_id):
            raise KeyError("missing proposal")
        return PATCH


class DiscussionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.analysis = RoutingAnalysis()
        self.server = make_server(SimpleNamespace(analysis=self.analysis), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, body=None, headers=None, *, raw=False):
        request = Request(self.url+path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", **(headers or {})})
        with urlopen(request, timeout=3) as response:
            return (response.read().decode(), dict(response.headers)) if raw else json.load(response)

    def test_discussion_retry_and_source_route_to_separate_service_operations(self):
        body = {"message": "Could this be the reference speed cap?"}
        self.assertTrue(self.request("/api/laps/session%3A1/discussion", body)["ok"])
        self.assertTrue(self.request("/api/laps/session%3A1/discussion/retry", {})["ok"])
        self.assertTrue(self.request("/api/laps/session%3A1/discussion/source", {})["ok"])
        self.assertEqual(self.analysis.calls, [("discuss", "session:1", body), ("retry", "session:1"), ("source", "session:1")])

    def test_source_attachment_requires_empty_object_and_returns_missing_resource_404(self):
        for body in ({"source_repo": "/other"}, {"message": "Retry now"}, []):
            with self.subTest(body=body), self.assertRaises(HTTPError) as error:
                self.request("/api/laps/session/discussion/source", body)
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.analysis.calls, [])
        with self.assertRaises(HTTPError) as error:
            self.request("/api/laps/missing/discussion/source", {})
        self.assertEqual(error.exception.code, 404)

    def test_patch_download_is_a_fixed_name_attachment_and_does_not_apply(self):
        content, headers = self.request("/api/laps/session%3A1/discussion/message-1/proposal/proposal-1.patch", raw=True)
        self.assertEqual(content, PATCH)
        self.assertEqual(headers["Content-Type"], "text/x-diff; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="robertaris-mpc-proposal.patch"')
        self.assertEqual(self.analysis.calls, [("patch", "session:1", "message-1", "proposal-1")])

    def test_oversized_discussion_is_rejected_before_body_read_or_service_call(self):
        before = time.monotonic()
        with self.assertRaises(HTTPError) as error:
            self.request("/api/laps/session%3A1/discussion", {}, {"Content-Length": str(32*1024+1)})
        self.assertEqual(error.exception.code, 400)
        self.assertIn("32 KiB", json.load(error.exception)["error"])
        self.assertLess(time.monotonic()-before, 1)
        self.assertEqual(self.analysis.calls, [])

    def test_unknown_missing_and_invalid_resources_have_distinct_errors(self):
        for path, body in (("/api/laps/missing/discussion", {}),
                           ("/api/laps/missing/discussion/retry", {}),
                           ("/api/laps/session/discussion/missing/proposal/p.patch", None),
                           ("/api/laps/session/discussion/m/proposal/p.patch/extra", None),
                           ("/api/laps/session/discussion/extra", {})):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                self.request(path, body)
            self.assertEqual(error.exception.code, 404)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/laps/session/discussion", {"invalid": True})
        self.assertEqual(error.exception.code, 400)

    def test_decoded_path_separators_cannot_change_resource_routes(self):
        for path in ("/api/laps/session%2Fother/discussion", "/api/laps/session/discussion/m%2Fother/proposal/p.patch",
                     "/api/laps/session/discussion/m/proposal/p%5Cother.patch"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                self.request(path, {} if path.endswith("discussion") else None)
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.analysis.calls, [])

    def test_host_and_origin_guards_cover_paid_operations_and_patch_download(self):
        for endpoint in ("/api/laps/session/discussion", "/api/laps/session/discussion/retry", "/api/laps/session/discussion/source"):
            for headers in ({"Host": "untrusted.example"}, {"Origin": "https://untrusted.example"}):
                with self.subTest(endpoint=endpoint, headers=headers), self.assertRaises(HTTPError) as error:
                    self.request(endpoint, {}, headers)
                self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/laps/session/discussion/m/proposal/p.patch", headers={"Host": "untrusted.example"})
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.analysis.calls, [])


class FakeDiscussionConnection:
    def __init__(self):
        self.requests, self.analysis_requests = [], []
        self.started, self.release = threading.Event(), threading.Event()
        self.block = self.fail = False
        self.connected = True

    def state(self):
        return {"provider": "openai", "model": "gpt-6.1-sol", "reasoning_effort": "high", "connected": self.connected}

    def configure(self, settings):
        return self.state()

    def analyze(self, context, *, on_start=None):
        self.analysis_requests.append(copy.deepcopy(context))
        return report(context["metrics"]["evidence"][0]["id"])

    def discuss(self, context, *, on_start=None):
        if on_start:
            on_start(self.state())
        self.requests.append(copy.deepcopy(context))
        self.started.set()
        if self.block:
            self.release.wait(5)
        if self.fail:
            raise ValueError("Test provider could not generate a reply")
        confirmations = {item["key"]: item["value"] for item in context["confirmations"]}
        evidence = context["metrics"]["evidence"][0]["id"]
        proposal = {"id": "p-code", "kind": "code", "title": "Review one launch setting", "rationale": "Use the cited lap interval for a controlled experiment.",
                    "evidence_ids": [evidence], "confirmation_keys": ["deployed_source_commit"],
                    "parameter": "", "current_value": "", "proposed_value": "", "unit": "",
                    "source_file": "mpc/launch/levine_launch.py", "source_commit": context.get("source_context", {}).get("commit", ""),
                    "diff": PATCH, "validation": "Compare several laps under matching conditions.",
                    "rollback": "Restore the confirmed setting if measured performance degrades."}
        if "deployed_source_commit" in confirmations:
            return {"message": "A saved patch is ready for your review; it has not been applied.", "questions": [],
                    "proposals": [proposal], "limitations": ["Source confirmation does not verify active ROS overrides."]}
        if "MAX_SPEED" in confirmations:
            proposal.update({"id": "p-speed", "kind": "parameter", "parameter": "MAX_SPEED",
                             "current_value": confirmations["MAX_SPEED"], "proposed_value": "3.1", "unit": "m/s",
                             "source_file": "", "source_commit": "", "diff": "", "confirmation_keys": ["MAX_SPEED"]})
            return {"message": "The confirmed speed cap supports a controlled parameter experiment.", "questions": [],
                    "proposals": [proposal], "limitations": ["The confirmed setting remains a user statement."]}
        return {"message": "Which speed cap ran on this lap?", "questions": [
            {"id": "q-speed", "key": "MAX_SPEED", "question": "What MAX_SPEED value was deployed?",
             "suggested_answer": "3", "reason": "Launch defaults do not establish active settings."}],
            "proposals": [], "limitations": []}


class DiscussionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = FakeDiscussionConnection()
        self.dashboard = Dashboard({"mode": "live"}, self.temp.name)
        self.dashboard.analysis.close()
        self.dashboard.analysis = AnalysisService(self.temp.name, connection=self.connection, active=True)
        self.server = make_server(self.dashboard, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.item = lap()
        self.path = "/api/laps/"+quote(self.item["id"], safe="")

    def tearDown(self):
        self.connection.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.dashboard.close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, body=None, *, raw=False):
        request = Request(self.url+path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            return response.read().decode() if raw else json.load(response)

    def wait_discussion(self, status="idle", messages=None):
        deadline = time.monotonic()+5
        discussion = None
        while time.monotonic() < deadline:
            discussion = self.request(self.path)["discussion"]
            if discussion["status"] == status and (messages is None or len(discussion["messages"]) >= messages):
                return discussion
            if discussion["status"] == "failed" and status != "failed":
                self.fail("Discussion failed: "+str(discussion["error"]))
            time.sleep(.02)
        self.fail("Discussion did not reach expected status: "+json.dumps(discussion))

    def test_manual_discussion_keeps_user_confirmations_separate_and_persists_export(self):
        self.request("/api/laps", self.item)
        initial = self.request(self.path)
        self.assertEqual(initial["discussion"]["messages"], [])
        self.assertEqual(self.connection.requests, [])
        self.assertFalse(self.dashboard.analysis.settings["enabled"])
        self.request(self.path+"/discussion", {"message": "Why was this lap slow?", "idempotency_key": "first"})
        discussion = self.wait_discussion(messages=2)
        self.assertEqual(discussion["confirmations"], [])
        self.assertEqual(discussion["messages"][-1]["response"]["questions"][0]["suggested_answer"], "3")
        self.assertEqual(self.connection.analysis_requests, [])
        self.request(self.path+"/discussion", {"confirmations": [{"key": "MAX_SPEED", "value": "3"}]})
        discussion = self.wait_discussion(messages=4)
        fact = discussion["confirmations"][0]
        self.assertEqual((fact["key"], fact["value"], fact["origin"], fact["lap_id"]),
                         ("MAX_SPEED", "3", "user_confirmed", self.item["id"]))
        self.assertEqual(discussion["messages"][-1]["response"]["proposals"][0]["kind"], "parameter")
        exported = self.request(self.path+"/export")
        self.assertEqual(exported["discussion"]["confirmations"], discussion["confirmations"])
        self.assertEqual(self.request(self.path)["report"], initial["report"])
        self.dashboard.analysis.close()
        self.dashboard.analysis = AnalysisService(self.temp.name, connection=self.connection, active=True)
        restored = self.request(self.path)["discussion"]
        self.assertEqual(restored["messages"], discussion["messages"])
        self.assertEqual(restored["confirmations"], discussion["confirmations"])
        self.request(self.path+"/discussion", {"remove_confirmations": ["MAX_SPEED"]})
        removed = self.wait_discussion(messages=6)
        self.assertEqual(removed["confirmations"], [])
        self.assertEqual(removed["messages"][-1]["response"]["proposals"], [])

    def test_busy_rejects_new_turn_but_same_request_is_idempotent(self):
        self.request("/api/laps", self.item)
        self.connection.block = True
        body = {"message": "Explain this lap", "idempotency_key": "one-request"}
        self.request(self.path+"/discussion", body)
        self.assertTrue(self.connection.started.wait(3))
        self.assertTrue(self.request(self.path+"/discussion", body)["duplicate"])
        with self.assertRaises(HTTPError) as error:
            self.request(self.path+"/discussion", {"message": "Different turn", "idempotency_key": "second-request"})
        self.assertEqual(error.exception.code, 400)
        self.connection.release.set()
        discussion = self.wait_discussion(messages=2)
        self.assertTrue(self.request(self.path+"/discussion", body)["duplicate"])
        self.assertEqual(len(self.connection.requests), 1)
        self.assertEqual(len(discussion["messages"]), 2)

    def test_failed_reply_can_be_retried_without_duplicating_user_message(self):
        self.request("/api/laps", self.item)
        self.connection.fail = True
        self.request(self.path+"/discussion", {"message": "Explain this lap"})
        self.assertEqual(len(self.wait_discussion("failed")["messages"]), 1)
        self.connection.fail = False
        self.request(self.path+"/discussion/retry", {})
        discussion = self.wait_discussion(messages=2)
        self.assertEqual(len([m for m in discussion["messages"] if m["role"] == "user"]), 1)
        self.assertEqual(len(self.connection.requests), 2)

    def test_attach_source_updates_failed_context_without_inference_until_explicit_retry(self):
        self.request("/api/laps", self.item)
        capture = self.request(self.path)
        self.connection.fail = True
        self.request(self.path+"/discussion", {"message": "Explain this lap"})
        failed = self.wait_discussion("failed")
        source = {"commit": "a"*40, "files": [{"path": "mpc/scripts/mpc_node.py", "content": "x = 1\n"}]}
        self.dashboard.analysis.source = copy.deepcopy(source)
        attached = self.request(self.path+"/discussion/source", {})["discussion"]
        self.assertEqual(len(self.connection.requests), 1)
        self.assertEqual(attached["status"], "failed")
        self.assertEqual(attached["error"], failed["error"])
        self.assertEqual(attached["messages"], failed["messages"])
        self.assertEqual(attached["source_context"], source)
        self.assertEqual(attached["source_origin"], "attached_after_discussion_start")
        self.assertEqual(self.request(self.path)["source_context"], capture["source_context"])
        with self.assertRaises(HTTPError) as error:
            self.request(self.path+"/discussion/source", {})
        self.assertEqual(error.exception.code, 400)
        self.connection.fail = False
        self.request(self.path+"/discussion/retry", {})
        self.wait_discussion(messages=2)
        self.assertEqual(len(self.connection.requests), 2)
        self.assertEqual(self.connection.requests[-1]["source_context"], source)
        self.assertEqual(self.request(self.path)["source_context"], capture["source_context"])

    def test_manual_discussion_preserves_a_previously_generated_lap_report(self):
        self.request("/api/analysis/settings", {"enabled": True})
        self.request("/api/laps", self.item)
        deadline = time.monotonic()+5
        detail = self.request(self.path)
        while detail["analysis_status"] != "complete" and time.monotonic() < deadline:
            time.sleep(.02)
            detail = self.request(self.path)
        self.assertEqual(detail["analysis_status"], "complete")
        original = detail["report"]
        self.assertIsNotNone(original)
        self.request("/api/analysis/settings", {"enabled": False})
        self.request(self.path+"/discussion", {"message": "Explain the existing recommendation"})
        self.wait_discussion(messages=2)
        self.assertEqual(self.connection.requests[-1]["report"], original)
        self.assertEqual(self.request(self.path)["report"], original)
        self.assertEqual(len(self.connection.analysis_requests), 1)

    def test_invalid_inputs_and_missing_comparison_do_not_queue_a_model_request(self):
        self.request("/api/laps", self.item)
        for body in ({"message": "x"*6001}, {"confirmations": [{"key": "MAX_SPEED", "value": 3}]},
                     {"message": "Question", "unexpected": True}):
            with self.subTest(body_type=list(body)), self.assertRaises(HTTPError) as error:
                self.request(self.path+"/discussion", body)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request(self.path+"/discussion", {"message": "Compare", "comparison_lap_id": "missing"})
        self.assertEqual(error.exception.code, 404)
        self.assertEqual(self.request(self.path)["discussion"]["messages"], [])
        self.assertEqual(self.connection.requests, [])

    def test_comparison_does_not_transfer_confirmations_and_inactive_mode_blocks_turns(self):
        self.request("/api/laps", self.item)
        comparison = lap(2, 22)
        self.request("/api/laps", comparison)
        self.request(self.path+"/discussion", {"message": "Compare this later lap", "comparison_lap_id": comparison["id"],
                                                "confirmations": [{"key": "MAX_SPEED", "value": "3"}]})
        discussion = self.wait_discussion(messages=2)
        context = self.connection.requests[-1]
        self.assertEqual(context["comparison"]["lap"]["id"], comparison["id"])
        self.assertTrue(all(fact["lap_id"] == self.item["id"] for fact in context["confirmations"]))
        self.assertNotIn("evidence", context["comparison"]["metrics"])
        later = self.request("/api/laps/"+quote(comparison["id"], safe=""))["discussion"]
        self.assertEqual(later["confirmations"], [])
        self.request("/api/command", {"action": "configure", "setup": {"mode": "simulation"}})
        with self.assertRaises(HTTPError) as error:
            self.request(self.path+"/discussion", {"message": "Another question"})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(len(self.connection.requests), 1)

    def test_saved_patch_download_uses_pinned_source_without_touching_checkout(self):
        source = "PARAMETERS = {'MAX_SPEED': 3.0}\n"
        path = self.dashboard.data_dir/"checkout/mpc/launch/levine_launch.py"
        path.parent.mkdir(parents=True)
        path.write_text(source)
        commit = "a"*40
        self.dashboard.analysis.source = {"commit": commit, "deployment_verified": False,
                                          "files": [{"path": "mpc/launch/levine_launch.py", "content": source}]}
        self.request("/api/laps", self.item)
        self.request(self.path+"/discussion", {"confirmations": [{"key": "deployed_source_commit", "value": commit}]})
        discussion = self.wait_discussion(messages=2)
        message = discussion["messages"][-1]
        self.connection.connected = False
        download = self.path+"/discussion/"+message["id"]+"/proposal/p-code.patch"
        self.assertEqual(self.request(download, raw=True), PATCH)
        self.assertEqual(path.read_text(), source)
        self.assertEqual(len(self.connection.requests), 1)
        self.assertEqual(self.request(self.path)["lap"], self.item)
        with self.assertRaises(HTTPError) as error:
            self.request(self.path+"/discussion/"+message["id"]+"/proposal/missing.patch")
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
