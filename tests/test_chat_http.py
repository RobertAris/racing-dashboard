"""The main chat works independently of lap selection, mode and telemetry I/O."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from racing_dashboard.server import Dashboard, make_server
from racing_dashboard.repository import resolve_repository_selection
from tests.test_analysis import lap


class ChatConnection:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.requests = []
        self.settings = []

    def state(self):
        return {"connected": True, "provider": "openai", "model": "fixture",
                "reasoning_effort": "high"}

    def configure(self, values):
        self.settings.append(values)
        return self.state()

    def chat(self, context, *, retrieve, on_start):
        self.requests.append(context)
        on_start(self.state())
        self.started.set()
        self.release.wait(5)
        return "Try `MAX_SPEED: 3.0 → 3.2 m/s` after checking the deployed value."


class ChatHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dashboard = Dashboard(data_dir=Path(self.temp.name) / "history")
        self.connection = ChatConnection()
        self.dashboard.analysis.connection = self.connection
        self.server = make_server(self.dashboard, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.connection.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.dashboard.close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, value=None, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = Request(self.url + path, headers=headers,
                          data=json.dumps(value).encode() if value is not None else None)
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def wait_for_reply(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            chat = self.request("/api/chat")
            if chat["status"] == "complete":
                return chat
            threading.Event().wait(.01)
        self.fail("Chat did not complete")

    def test_opening_chat_does_not_create_storage_or_inference(self):
        state = self.request("/api/chat")
        self.assertEqual(state["messages"], [])
        self.assertEqual(state["status"], "idle")
        self.assertFalse(self.dashboard.analysis.data_dir.exists())
        self.assertEqual(self.connection.requests, [])

    def test_chat_before_first_lap_works_in_replay_and_is_idempotent(self):
        self.request("/api/command", {"action": "configure", "setup": {"mode": "replay"}})
        message = {"message": "Explain my controller", "idempotency_key": "same-message"}
        self.request("/api/chat", message)
        self.assertTrue(self.connection.started.wait(2))
        self.assertTrue(self.request("/api/chat", message)["duplicate"])
        self.connection.release.set()
        reply = self.wait_for_reply()
        self.assertEqual([m["role"] for m in reply["messages"]], ["user", "assistant"])
        self.assertEqual(len(self.connection.requests), 1)
        self.assertEqual(self.connection.requests[0]["environment"]["mode"], "replay")
        self.assertEqual(reply["context"]["lap_count"], 0)
        self.assertEqual(self.request("/api/chat/reset", {})["chat"]["messages"], [])

    def test_pending_reply_leaves_telemetry_and_lap_ingestion_available(self):
        self.request("/api/chat", {"message": "What should I test?"})
        self.assertTrue(self.connection.started.wait(2))
        self.assertTrue(self.request("/api/health")["ok"])
        self.assertTrue(self.request("/api/laps", lap())["ok"])
        with self.assertRaises(HTTPError) as error:
            self.request("/api/chat/reset", {})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.request("/api/chat")["status"], "analyzing")

    def test_chat_write_validation_and_cross_origin_policy(self):
        for path, value in (("/api/chat", {"message": " "}),
                            ("/api/chat", {"message": "x" * 40000}),
                            ("/api/chat/reset", {"message": "extra"}),
                            ("/api/chat/retry", [])):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                self.request(path, value)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/chat", {"message": "hello"}, origin="https://unrelated.example")
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.connection.requests, [])

    def test_provider_setup_does_not_depend_on_a_valid_repository(self):
        self.dashboard.analysis.settings["source_repo"] = "/missing/unfinished-repository"
        configured = self.request("/api/analysis/provider", {"provider": "chatgpt", "model": "fixture"})
        self.assertTrue(configured["ok"])
        self.assertEqual(self.connection.settings, [{"provider": "chatgpt", "model": "fixture"}])
        self.assertEqual(configured["connection"]["source_repo"], "/missing/unfinished-repository")
        self.assertEqual(self.connection.requests, [])

    def test_arbitrary_lab_repository_and_branch_drive_chat_source(self):
        repo = Path(self.temp.name) / "lab8-mppi"
        repo.mkdir()

        def git(*args):
            return subprocess.run(["git", "-c", "core.hooksPath=/dev/null",
                "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "-C", str(repo), *args],
                check=True, capture_output=True, text=True).stdout.strip()

        git("init", "-q", "-b", "main")
        git("remote", "add", "origin", "git@github.com:Example/lab8-mppi.git")
        source = repo / "mppi" / "config" / "params.yaml"
        source.parent.mkdir(parents=True)
        source.write_text("temperature: 0.3\nrollouts: 1000\n")
        git("add", ".")
        git("commit", "-qm", "baseline")
        git("checkout", "-qb", "liam-trial-1")
        source.write_text("temperature: 0.7\nrollouts: 1000\n")
        git("commit", "-qam", "experiment")
        expected_commit = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")

        branches = self.request("/api/analysis/branches", {"source_repo": str(repo)})
        self.assertEqual(branches["current_branch"], "main")
        self.assertEqual(set(branches["branches"]), {"main", "liam-trial-1"})
        resolve = lambda repo, ref="": resolve_repository_selection(repo, ref, search_roots=[self.temp.name])
        github_url = "https://github.com/Example/lab8-mppi/tree/"
        with patch("racing_dashboard.server.resolve_repository_selection", side_effect=resolve):
            matched = self.request("/api/analysis/branches", {
                "source_repo": github_url, "source_ref": "liam-trial-1"})
        self.assertEqual(matched["resolved_repo"], str(repo.resolve()))
        self.assertEqual(matched["resolved_ref"], "liam-trial-1")
        self.assertTrue(matched["resolved_from_url"])
        with patch("racing_dashboard.analysis.resolve_repository_selection", side_effect=resolve):
            configured = self.request("/api/analysis/settings", {
                "source_repo": github_url, "source_ref": "liam-trial-1"})
        self.assertEqual(configured["connection"]["source_repo_name"], "lab8-mppi")
        self.assertEqual(configured["connection"]["source_repo"], str(repo.resolve()))
        self.connection.release.set()
        self.request("/api/chat", {"message": "Which MPPI temperature is configured on this branch?"})
        reply = self.wait_for_reply()
        source_context = self.connection.requests[-1]["source_context"]
        self.assertEqual(source_context["commit"], expected_commit)
        self.assertEqual(source_context["ref"], "liam-trial-1")
        self.assertIn("temperature: 0.7", source_context["files"][0]["content"])
        self.assertEqual(reply["context"]["source"]["repo_name"], "lab8-mppi")
        self.assertEqual(git("branch", "--show-current"), "main")
        self.assertIn("temperature: 0.3", source.read_text())

    def test_branch_listing_validates_input_and_cross_origin_writes(self):
        for value in ({}, {"source_repo": []}, {"source_repo": ""},
                      {"source_repo": "/missing/repo", "ref": "main"}):
            with self.subTest(value=value), self.assertRaises(HTTPError) as error:
                self.request("/api/analysis/branches", value)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/analysis/branches", {"source_repo": self.temp.name},
                         origin="https://unrelated.example")
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
