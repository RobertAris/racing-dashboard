import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import ssl
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from racing_dashboard.llm import (API, APP_NAME, AUTHORIZE_ENDPOINT, DISCOVERY, ISSUER,
                                  TOKEN_ENDPOINT, LLMConnection, LLMError, _NoRedirect,
                                  _verified_ssl_context, validate_report)
from racing_dashboard.discussion import DISCUSSION_SCHEMA


METADATA = {"issuer": ISSUER, "authorization_endpoint": AUTHORIZE_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT, "jwks_uri": ISSUER + "/.well-known/jwks.json",
            "revocation_endpoint": ISSUER + "/api/accounts/oauth/revoke"}
REPORT = {"summary": "One slow sector merits a controlled experiment.", "findings": [{
    "title": "Steering oscillation", "observation": "Sector 2 contains repeated steering reversals.",
    "hypothesis": "Tracking may be underdamped.", "confidence": "medium", "evidence_ids": ["sector-2"],
    "recommendation": "Confirm active MPC settings before choosing a single tuning change.",
    "validation": "Compare sector time and steering variability over comparable laps."}],
    "limitations": ["Solver status was not published."]}
DISCUSSION = {"message": "Confirm the deployed speed cap before choosing a number.",
              "questions": [{"id": "q1", "key": "MAX_SPEED", "question": "What speed cap ran on this lap?",
                             "suggested_answer": "3", "reason": "Launch defaults do not prove runtime settings."}],
              "proposals": [], "limitations": ["User-confirmed facts are not runtime-verified."]}


def completion(report=REPORT):
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(report)}]}]}


def sse(events):
    return io.BytesIO("".join("data: " + json.dumps(event) + "\n\n" for event in events).encode())


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "private"
        self.connection = LLMConnection(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def connect_fixture(self, expires=3600, scopes="openid chatgpt.tokens.use.direct"):
        self.connection.configure({})
        record = {"issuer": ISSUER, "subject": "subject-1", "client_id": "oaiapp_fixture",
                  "name": "Example", "email": "person@example.test", "access_token": "private-access",
                  "refresh_token": "private-refresh", "id_token": "private-id",
                  "expires_at": time.time() + expires, "scopes": scopes.split()}
        self.connection._doc["registrations"]["fixture"] = record
        self.connection._doc["active_account_id"] = "fixture"
        self.connection._doc["host_id"] = "urn:uuid:11111111-1111-4111-8111-111111111111"
        self.connection._save()
        return record

    def start(self):
        with patch.dict("sys.modules", {"jwt": types.ModuleType("jwt")}), \
                patch.object(self.connection, "_request", return_value=METADATA):
            result = self.connection.auth_start("http://127.0.0.1:8765/auth/callback")
        return parse_qs(urlsplit(result["url"]).query)

    def test_lazy_default_and_personal_model(self):
        self.assertFalse(self.folder.exists())
        state = self.connection.state()
        self.assertEqual((state["provider"], state["model"], state["reasoning_effort"]),
                         ("chatgpt", "gpt-6.1-sol", "high"))
        self.assertFalse(state["connected"])
        self.assertFalse(self.folder.exists())

    def test_api_key_protected_private_and_endpoint_bound(self):
        state = self.connection.configure({"provider": "openai", "api_key": "private-api-key"})
        self.assertTrue(state["connected"])
        self.assertTrue(state["api_key_set"])
        self.assertNotIn("private-api-key", json.dumps(state))
        self.assertEqual(self.connection.path.stat().st_mode & 0o777, 0o600)
        restarted = LLMConnection(self.folder)
        self.assertTrue(restarted.state()["api_key_set"])
        self.connection.configure({"api_key": ""})
        self.assertTrue(self.connection.state()["api_key_set"])
        self.connection.configure({"provider": "compatible", "base_url": "https://example.test/v1"})
        with patch.object(self.connection, "_request", return_value={"data": []}) as request:
            self.connection.models()
        self.assertIsNone(request.call_args.kwargs["token"])
        self.connection.configure({"provider": "openai", "base_url": API})
        self.assertTrue(self.connection.state()["api_key_set"])
        self.connection.configure({"clear_api_key": True})
        self.assertFalse(self.connection.state()["connected"])

    def test_reject_unsafe_or_ambiguous_settings(self):
        for url in ("http://remote.test/v1", "https://name:password@example.test/v1",
                    "https://example.test/v1?secret=yes", "file:///tmp/endpoint"):
            with self.subTest(url=url), self.assertRaises(LLMError):
                self.connection.configure({"provider": "compatible", "base_url": url})
        for settings in ({"provider": "unknown"}, {"model": ""}, {"reasoning_effort": "whatever"},
                         {"api_key": "key\nheader"}, {"api_key": ["key"]}, {"unknown": True}):
            with self.subTest(settings=settings), self.assertRaises(LLMError):
                self.connection.configure(settings)
        self.connection.configure({"provider": "compatible", "base_url": "http://127.0.0.1:11434/v1",
                                   "reasoning_effort": ""})
        self.assertTrue(self.connection.state()["connected"])

    def test_repository_credential_dir_rejected_before_write(self):
        target = Path(__file__).resolve().parent.parent / "never-create-private-credentials"
        with self.assertRaisesRegex(LLMError, "outside the repository"):
            LLMConnection(target).configure({})
        self.assertFalse(target.exists())

    def test_refuses_reading_public_or_symlink_credentials(self):
        self.connection.configure({})
        self.connection.path.chmod(0o644)
        with self.assertRaisesRegex(LLMError, "owner-only"):
            LLMConnection(self.folder)
        self.connection.path.chmod(0o600)
        link_dir = Path(self.temp.name) / "linked"
        link_dir.mkdir()
        (link_dir / "llm_credentials.json").symlink_to(self.connection.path)
        with self.assertRaises(LLMError):
            LLMConnection(link_dir)

    def test_signin_pkce_stable_host_and_fresh_state(self):
        params = self.start()
        self.assertEqual(params["client_id"], ["dynamic_agent_client"])
        self.assertEqual(params["agent_name_hint"], [APP_NAME])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertIn("chatgpt.tokens.use.direct", params["scope"][0])
        self.assertNotIn(self.connection._pending["verifier"], str(params))
        next_params = self.start()
        self.assertEqual(params["ext_agent_host_id"], next_params["ext_agent_host_id"])
        self.assertNotEqual(params["state"], next_params["state"])
        self.assertNotEqual(params["nonce"], next_params["nonce"])
        self.assertNotIn(params["state"][0], self.connection.path.read_text())
        for callback in ("http://localhost:8765/auth/callback", "http://127.0.0.1:8765/callback",
                         "https://remote.test/auth/callback", "http://127.0.0.1/auth/callback"):
            with patch.dict("sys.modules", {"jwt": types.ModuleType("jwt")}), self.assertRaises(LLMError):
                self.connection.auth_start(callback)

    def test_callback_requires_state_one_use_issued_client_and_verified_identity(self):
        params = self.start()
        good = {"state": params["state"][0], "client_id": "oaiapp_new", "code": "authorization-code"}
        with patch.object(self.connection, "_request") as network:
            with self.assertRaises(LLMError):
                self.connection.auth_callback({**good, "state": "wrong"})
            network.assert_not_called()
        tokens = {"access_token": "secret-access", "refresh_token": "secret-refresh", "id_token": "signed-id",
                  "expires_in": 3600, "token_type": "Bearer", "scope": "openid chatgpt.tokens.use.direct"}
        with patch.object(self.connection, "_request", return_value=tokens) as network, \
                patch.object(self.connection, "_validate_id_token", return_value={"sub": "verified-sub"}) as verify:
            state = self.connection.auth_callback(good)
            verify.assert_called_once_with("signed-id", "oaiapp_new", params["nonce"][0])
            sent = network.call_args.kwargs["form"]
            self.assertEqual(sent["client_id"], "oaiapp_new")
            self.assertEqual(sent["redirect_uri"], "http://127.0.0.1:8765/auth/callback")
            self.assertTrue(state["connected"])
            self.assertNotIn("secret", json.dumps(state))
            with self.assertRaises(LLMError):
                self.connection.auth_callback(good)

    def test_declined_missing_or_expired_callback_does_not_exchange(self):
        for case in ("declined", "missing-client", "expired"):
            params = self.start()
            query = {"state": params["state"][0], "code": "code"}
            if case == "declined":
                query["error"] = "access_denied"
            if case == "expired":
                self.connection._pending["expires_at"] = 0
            with self.subTest(case=case), patch.object(self.connection, "_request") as network:
                with self.assertRaises(LLMError):
                    self.connection.auth_callback(query)
                network.assert_not_called()

    def test_returning_registration_rejects_changed_client_or_subject(self):
        self.connect_fixture()
        params = self.start()
        self.assertEqual(params["client_id"], ["oaiapp_fixture"])
        self.assertEqual(params["id_token_hint"], ["private-id"])
        self.assertNotIn("agent_name_hint", params)
        with self.assertRaises(LLMError):
            self.connection.auth_callback({"state": params["state"][0], "client_id": "another", "code": "code"})
        params = self.start()
        with patch.object(self.connection, "_request", return_value={"id_token": "wrong-account"}), \
                patch.object(self.connection, "_validate_id_token", return_value={"sub": "another-subject"}):
            with self.assertRaisesRegex(LLMError, "original ChatGPT account"):
                self.connection.auth_callback({"state": params["state"][0], "code": "code"})
        self.assertEqual(self.connection._doc["registrations"]["fixture"]["access_token"], "private-access")

    def test_missing_usage_scope_does_not_authorize_inference(self):
        params = self.start()
        tokens = {"access_token": "identity-only", "id_token": "id", "token_type": "Bearer",
                  "expires_in": 3600, "scope": "openid email profile"}
        with patch.object(self.connection, "_request", return_value=tokens), \
                patch.object(self.connection, "_validate_id_token", return_value={"sub": "verified"}):
            state = self.connection.auth_callback({"state": params["state"][0], "code": "code", "client_id": "issued"})
        self.assertFalse(state["connected"])
        self.assertIsNotNone(state["account"])
        with patch.object(self.connection, "_request") as network, self.assertRaises(LLMError):
            self.connection.analyze({})
        network.assert_not_called()

    def test_refresh_rotates_once_across_instances(self):
        self.connect_fixture(expires=-1)
        second = LLMConnection(self.folder)
        renewed = {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "token_type": "Bearer",
                   "expires_in": 3600, "scope": "openid chatgpt.tokens.use.direct"}
        requests = []
        def network(url, **kwargs):
            requests.append(kwargs)
            return renewed
        with patch.object(self.connection, "_request", side_effect=network), \
                patch.object(second, "_request", side_effect=network):
            results = []
            def retrieve(client):
                with client._lock:
                    results.append(client._bearer())
            threads = [threading.Thread(target=retrieve, args=(client,)) for client in (self.connection, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
        self.assertEqual(results, ["rotated-access", "rotated-access"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["form"]["refresh_token"], "private-refresh")
        saved = LLMConnection(self.folder)._doc["registrations"]["fixture"]
        self.assertEqual((saved["access_token"], saved["refresh_token"]), ("rotated-access", "rotated-refresh"))

    def test_invalid_rotated_refresh_requires_reconnect_and_exposes_sanitized_error(self):
        self.connect_fixture(expires=-1)
        with patch.object(self.connection, "_request", return_value={"access_token": "new", "token_type": "Bearer",
                   "expires_in": 3600}), self.assertRaises(LLMError):
            self.connection.models()
        self.assertNotIn("refresh_token", self.connection._doc["registrations"]["fixture"])
        self.assertFalse(self.connection.state()["connected"])
        self.assertNotIn("private", json.dumps(self.connection.state()))
        self.assertIsNotNone(self.connection.state()["error"])

    def test_terminal_refresh_rejection_disconnects_but_temporary_failure_preserves_session(self):
        for retryable in (False, True):
            self.connect_fixture(expires=-1)
            error = LLMError("The provider session was rejected." if not retryable else "Temporary connection failure.",
                             retryable=retryable)
            with self.subTest(retryable=retryable), patch.object(self.connection, "_request", side_effect=error):
                with self.assertRaises(LLMError):
                    self.connection.models()
            state = self.connection.state()
            self.assertEqual(state["connected"], retryable)
            record = LLMConnection(self.folder)._doc["registrations"]["fixture"]
            self.assertEqual(bool(record.get("refresh_token")), retryable)
            if retryable:
                self.assertEqual(record["refresh_token"], "private-refresh")
                self.assertEqual(record["access_token"], "private-access")
            else:
                for key in ("access_token", "refresh_token", "id_token", "expires_at", "scopes"):
                    self.assertNotIn(key, record)
                with patch.object(self.connection, "_request") as network, self.assertRaises(LLMError):
                    self.connection.models()
                network.assert_not_called()

    def test_disconnect_revokes_and_clears_all_tokens_retains_registration_host(self):
        self.connect_fixture()
        with patch.object(self.connection, "_request", side_effect=[METADATA, {}]) as network:
            state = self.connection.disconnect()
        self.assertFalse(state["connected"])
        self.assertEqual(network.call_args.kwargs["form"], {
            "token": "private-refresh", "token_type_hint": "refresh_token", "client_id": "oaiapp_fixture"})
        wire = self.connection.path.read_text()
        self.assertNotIn("private-access", wire)
        self.assertNotIn("private-refresh", wire)
        self.assertNotIn("private-id", wire)
        self.assertIn("oaiapp_fixture", wire)
        self.assertIn("urn:uuid:", wire)

    def test_account_models_are_filtered_without_inference(self):
        self.connect_fixture()
        catalog = {"models": [{"slug": "gpt-6-sol", "display_name": "GPT-6 Sol", "visibility": "list"},
                              {"slug": "internal", "visibility": "hidden"}]}
        with patch.object(self.connection, "_request", return_value=catalog) as network:
            self.assertEqual(self.connection.models(), [{"id": "gpt-6-sol", "display_name": "GPT-6 Sol"}])
            self.assertEqual(network.call_args.args, (API + "/models",))

    def test_chatgpt_request_contract_and_strict_report(self):
        self.connect_fixture()
        with patch.object(self.connection, "_request", return_value=completion()) as network:
            result = self.connection.analyze({"evidence": [{"id": "sector-2"}]})
        self.assertEqual(result, REPORT)
        payload = network.call_args.kwargs["payload"]
        self.assertEqual(payload["model"], "gpt-6.1-sol")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertFalse(payload["store"])
        self.assertTrue(payload["stream"])
        self.assertIsInstance(payload["input"], list)
        self.assertTrue(payload["text"]["format"]["strict"])
        for unsupported in ("max_output_tokens", "temperature", "previous_response_id", "tools"):
            self.assertNotIn(unsupported, payload)
        self.assertNotIn("private-access", json.dumps(payload))

    def test_chatgpt_discussion_uses_strict_turn_schema_high_reasoning_and_exact_provenance(self):
        self.connect_fixture()
        supplied = {"lap": {"id": "lap-1"}, "metrics": {"evidence": [{"id": "sector-2"}]},
                    "source_context": {}, "report": REPORT, "confirmations": [],
                    "messages": [{"role": "user", "content": "Why did sector 2 slow down?"}], "comparison": None}
        captured = []
        with patch.object(self.connection, "_request", return_value=completion(DISCUSSION)) as network:
            result = self.connection.discuss(supplied, on_start=captured.append)
        self.assertEqual(result, DISCUSSION)
        payload = network.call_args.kwargs["payload"]
        self.assertEqual(payload["model"], "gpt-6.1-sol")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["text"]["format"], {"type": "json_schema", "name": "lap_discussion",
                                                   "strict": True, "schema": DISCUSSION_SCHEMA})
        self.assertEqual(json.loads(payload["input"][0]["content"]), supplied)
        self.assertIn("user-confirmed", payload["instructions"])
        self.assertFalse(payload["store"])
        self.assertTrue(payload["stream"])
        self.assertEqual(captured[0]["model"], "gpt-6.1-sol")
        for forbidden in ("tools", "max_output_tokens", "temperature", "previous_response_id"):
            self.assertNotIn(forbidden, payload)
        self.assertNotIn("private-access", json.dumps(payload))
        def strict_objects(schema):
            if isinstance(schema, dict):
                if schema.get("type") == "object":
                    self.assertFalse(schema["additionalProperties"])
                    self.assertEqual(set(schema["required"]), set(schema["properties"]))
                for value in schema.values():
                    strict_objects(value)
            elif isinstance(schema, list):
                for value in schema:
                    strict_objects(value)
        strict_objects(DISCUSSION_SCHEMA)

    def test_responses_selects_final_phase_without_concatenating_commentary_or_other_items(self):
        self.connect_fixture()
        serialized = json.dumps(DISCUSSION)
        final = {"type": "message", "role": "assistant", "phase": "final_answer", "status": "completed",
                 "content": [{"type": "output_text", "text": serialized[:25]},
                             {"type": "output_text", "text": serialized[25:]}]}
        response = {"status": "completed", "output": [
            {"type": "reasoning", "content": [{"type": "output_text", "text": "not a final response"}]},
            {"type": "message", "role": "user", "phase": "final_answer",
             "content": [{"type": "output_text", "text": "not the assistant"}]},
            {"type": "message", "role": "assistant", "phase": "commentary", "status": "completed",
             "content": [{"type": "output_text", "text": "I will check the slow sector and the active speed cap."}]},
            final,
            {"type": "function_call", "content": [{"type": "output_text", "text": "ignore this item"}]},
        ]}
        with patch.object(self.connection, "_request", return_value=response):
            self.assertEqual(self.connection.discuss({}), DISCUSSION)
        # Initial lap analysis uses the same extraction path and strict validator.
        response["output"][3] = {**final, "content": [{"type": "output_text", "text": json.dumps(REPORT)}]}
        with patch.object(self.connection, "_request", return_value=response):
            self.assertEqual(self.connection.analyze({}), REPORT)

    def test_responses_uses_last_final_or_last_legacy_unphased_message(self):
        self.connect_fixture()
        legacy = completion(DISCUSSION)["output"][0]  # Legacy fixture omits role/phase/status.
        first = {**legacy, "content": [{"type": "output_text", "text": "Earlier draft"}]}
        for items in ([first, legacy],
                      [{**first, "phase": "commentary"}, legacy],
                      [{**first, "phase": "final_answer"}, {**legacy, "phase": "final_answer"}],
                      [{**legacy, "phase": "final_answer"}, first]):
            with self.subTest(items=items), patch.object(self.connection, "_request", return_value={"status": "completed", "output": items}):
                self.assertEqual(self.connection.discuss({}), DISCUSSION)

    def test_responses_rejects_selected_empty_refusal_incomplete_and_malformed_final(self):
        self.connect_fixture()
        good = {**completion(DISCUSSION)["output"][0], "phase": "final_answer", "role": "assistant"}
        cases = [({**good, "content": []}, "empty final"),
                 ({**good, "content": [{"type": "output_text", "text": "  "}]}, "empty final"),
                 ({**good, "content": [{"type": "refusal", "refusal": "private provider explanation"}]}, "declined"),
                 ({**good, "status": "incomplete"}, "did not complete"),
                 ({**good, "status": "in_progress"}, "did not complete"),
                 ({**good, "content": [{"type": "output_text", "text": "Preamble " + json.dumps(DISCUSSION)}]}, "not valid JSON"),
                 ({**good, "content": [{"type": "output_text", "text": "```json\n" + json.dumps(DISCUSSION) + "\n```"}]}, "not valid JSON"),
                 ({**good, "content": [{"type": "output_text", "text": '{"message":'}]}, "not valid JSON")]
        for selected, message in cases:
            # A valid earlier final must not hide a failed/empty newer answer.
            with self.subTest(message=message), patch.object(self.connection, "_request", return_value={"status": "completed", "output": [good, selected]}):
                with self.assertRaisesRegex(LLMError, message) as caught:
                    self.connection.discuss({})
                self.assertNotIn("private provider explanation", str(caught.exception))
        untyped = {key: value for key, value in good.items() if key != "type"}
        for items in ([], [untyped], [{**good, "phase": "commentary"}], [{**good, "role": "user"}]):
            with self.subTest(items=items), patch.object(self.connection, "_request", return_value={"status": "completed", "output": items}):
                with self.assertRaisesRegex(LLMError, "without a final"):
                    self.connection.discuss({})

    def test_compatible_discussion_uses_chat_completion_schema_and_own_provider(self):
        self.connection.configure({"provider": "compatible", "model": "chosen-model", "base_url": "https://example.test/v1",
                                   "api_key": "own-key", "reasoning_effort": ""})
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(DISCUSSION)}}]}
        with patch.object(self.connection, "_request", return_value=response) as network:
            self.assertEqual(self.connection.discuss({}), DISCUSSION)
        self.assertEqual(network.call_args.args, ("https://example.test/v1/chat/completions",))
        self.assertEqual(network.call_args.kwargs["token"], "own-key")
        payload = network.call_args.kwargs["payload"]
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], DISCUSSION_SCHEMA)
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "lap_discussion")
        self.assertEqual(payload["model"], "chosen-model")

    def test_discussion_rejects_unvalidated_turn_refusal_incomplete_and_oversized_context(self):
        self.connect_fixture()
        for response in (completion({**DISCUSSION, "unrecognized": "field"}),
                         {"status": "incomplete", "output": []},
                         {"status": "completed", "output": [{"content": [{"type": "refusal", "refusal": "no"}]}]}):
            with self.subTest(response=response), patch.object(self.connection, "_request", return_value=response), self.assertRaises(LLMError):
                self.connection.discuss({})
        with patch.object(self.connection, "_request") as network:
            with self.assertRaises(LLMError):
                self.connection.discuss({"messages": [{"role": "user", "content": "x" * 240001}]})
            with self.assertRaises(LLMError):
                self.connection.discuss({"bad": float("nan")})
            network.assert_not_called()

    def test_compatible_provider_and_incomplete_result_rejected(self):
        self.connection.configure({"provider": "compatible", "model": "user-model", "base_url": "https://example.test/v1",
                                   "api_key": "own-key", "reasoning_effort": ""})
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(REPORT)}}]}
        with patch.object(self.connection, "_request", return_value=response) as network:
            self.assertEqual(self.connection.analyze({}), REPORT)
            self.assertEqual(network.call_args.args, ("https://example.test/v1/chat/completions",))
            self.assertNotIn("reasoning_effort", network.call_args.kwargs["payload"])
        response["choices"][0]["finish_reason"] = "length"
        with patch.object(self.connection, "_request", return_value=response), self.assertRaises(LLMError):
            self.connection.analyze({})

    def test_disconnect_or_reconfigure_during_analysis_discards_result(self):
        self.connection.configure({"provider": "openai", "api_key": "own-key"})
        def delayed(*args, **kwargs):
            self.connection.configure({"model": "another"})
            return completion()
        with patch.object(self.connection, "_request", side_effect=delayed), self.assertRaisesRegex(LLMError, "changed"):
            self.connection.analyze({})

    def test_state_is_nonblocking_and_immutable_during_slow_auth(self):
        self.connect_fixture()
        original = self.connection.state()
        held, release = threading.Event(), threading.Event()
        def slow_operation():
            with self.connection._lock:
                held.set()
                release.wait(3)
        thread = threading.Thread(target=slow_operation)
        thread.start()
        self.assertTrue(held.wait(1))
        started = time.monotonic()
        state = self.connection.state()
        elapsed = time.monotonic() - started
        release.set()
        thread.join(2)
        self.assertLess(elapsed, .5)
        self.assertEqual(state, original)
        state["account"]["email"] = "modified"
        state["accounts"].clear()
        self.assertEqual(self.connection.state(), original)

    def test_analysis_reports_exact_connection_snapshot_without_secrets(self):
        self.connection.configure({"provider": "openai", "api_key": "private-api-key"})
        captured = []
        with patch.object(self.connection, "_request", return_value=completion()):
            self.connection.analyze({}, on_start=captured.append)
        self.assertEqual(captured[0]["model"], "gpt-6.1-sol")
        self.assertEqual(captured[0]["provider"], "openai")
        self.assertNotIn("private-api-key", json.dumps(captured))

    def test_stream_requires_terminal_completed_and_redacts_provider_errors(self):
        response = completion()
        self.assertEqual(LLMConnection._read_stream(sse([
            {"type": "response.output_text.delta", "delta": "untrusted partial"},
            {"type": "response.completed", "response": response}])), response)
        for events in ([{"type": "response.output_text.delta", "delta": "partial"}],
                       [{"type": "response.failed", "response": {"error": {"message": "private-token",
                           "code": "subscription_sharing_usage_limit_exceeded"}}}],
                       [{"type": "response.incomplete", "response": {}}],
                       [{"type": "response.completed", "response": {"status": "incomplete"}}],
                       [["malformed"]]):
            with self.subTest(events=events), self.assertRaises(LLMError) as caught:
                LLMConnection._read_stream(sse(events))
            self.assertNotIn("private-token", str(caught.exception))

    def test_stream_accepts_complete_terminal_frame_at_eof_without_blank_separator(self):
        response = completion(DISCUSSION)
        response["output"][0]["phase"] = "final_answer"
        events = [{"type": "response.output_text.delta", "delta": "untrusted partial"},
                  {"type": "response.completed", "response": response}]
        wire = sse(events).getvalue()
        for suffix in (b"\n\n", b"\n", b""):
            with self.subTest(suffix=suffix):
                stream = io.BytesIO(wire.rstrip(b"\n") + suffix)
                self.assertEqual(LLMConnection._read_stream(stream), response)

    def test_stream_eof_never_promotes_fragments_done_or_output_item_to_response_completion(self):
        final = {**completion(DISCUSSION)["output"][0], "phase": "final_answer", "status": "completed"}
        cases = [sse([{"type": "response.output_text.delta", "delta": json.dumps(DISCUSSION)}]).getvalue(),
                 sse([{"type": "response.output_text.done", "text": json.dumps(DISCUSSION)}]).getvalue(),
                 sse([{"type": "response.output_item.done", "item": final, "output_index": 0}]).getvalue(),
                 b"data: [DONE]", b'data: {"type":"response.completed","response":']
        for wire in cases:
            with self.subTest(wire=wire[:100]), self.assertRaises(LLMError):
                LLMConnection._read_stream(io.BytesIO(wire.rstrip(b"\n")))
        # A populated terminal response is authoritative; completed-item events
        # are not a fallback for an empty final response that needs investigation.
        empty = {"status": "completed", "output": []}
        self.assertEqual(LLMConnection._read_stream(sse([
            {"type": "response.output_item.done", "item": final, "output_index": 0},
            {"type": "response.completed", "response": empty}])), empty)

    def test_redirects_disabled_http_errors_sanitized_and_empty_revocation_accepted(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))
        error = HTTPError(API, 401, "private-token", {}, io.BytesIO(b"private-token"))
        with patch("racing_dashboard.llm.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(LLMError) as caught:
                self.connection._request(API + "/models", token="private-token")
            self.assertNotIn("private-token", str(caught.exception))
            self.assertFalse(caught.exception.retryable)
        with patch("racing_dashboard.llm.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b"")
            self.assertEqual(self.connection._request(METADATA["revocation_endpoint"], form={"token": "secret"}), {})

    def test_request_classifies_temporary_status_and_network_failures_for_bounded_retry(self):
        self.assertFalse(LLMError("Invalid request").retryable)
        for status in (400, 401, 403, 404, 429, 500, 502, 503, 504):
            with self.subTest(status=status), patch("racing_dashboard.llm.build_opener") as opener:
                opener.return_value.open.side_effect = HTTPError(API, status, "private-message", {}, io.BytesIO(b"private-body"))
                with self.assertRaises(LLMError) as caught:
                    self.connection._request(API + "/responses", payload={})
                self.assertEqual(caught.exception.retryable, status == 429 or status >= 500)
                self.assertNotIn("private", str(caught.exception))
        for failure in (URLError("private-endpoint"), TimeoutError("private-timeout"), OSError("private-network")):
            with self.subTest(failure=type(failure).__name__), patch("racing_dashboard.llm.build_opener") as opener:
                opener.return_value.open.side_effect = failure
                with self.assertRaises(LLMError) as caught:
                    self.connection._request(API + "/models")
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn("private", str(caught.exception))
        with patch("racing_dashboard.llm.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b"not JSON")
            with self.assertRaises(LLMError) as caught:
                self.connection._request(API + "/models")
            self.assertFalse(caught.exception.retryable)

    def test_certificate_failures_are_actionable_sanitized_and_not_retried(self):
        certificate_error = ssl.SSLCertVerificationError(1, "private-certificate-diagnostic")
        for failure in (certificate_error, URLError(certificate_error)):
            with self.subTest(failure=type(failure).__name__), patch("racing_dashboard.llm.build_opener") as opener:
                opener.return_value.open.side_effect = failure
                with self.assertRaises(LLMError) as caught:
                    self.connection._request(DISCOVERY)
                self.assertIn("certificate", str(caught.exception))
                self.assertIn("SSL_CERT_FILE", str(caught.exception))
                self.assertNotIn("private", str(caught.exception))
                self.assertFalse(caught.exception.retryable)

    def test_https_transport_uses_verified_context_and_redirect_protection(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch("racing_dashboard.llm._verified_ssl_context", return_value=context), \
                patch("racing_dashboard.llm.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b'{"ok":true}')
            self.assertTrue(self.connection._request(DISCOVERY)["ok"])
        redirect_handler, https_handler = opener.call_args.args
        self.assertIsInstance(redirect_handler, _NoRedirect)
        self.assertIs(https_handler._context, context)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_report_validation_bounds_and_grounding_shape(self):
        self.assertEqual(validate_report(REPORT), REPORT)
        for change in ({"findings": REPORT["findings"] * 4}, {"summary": 5}, {"extra": "unexpected"}):
            with self.subTest(change=change), self.assertRaises(LLMError):
                validate_report({**REPORT, **change})
        broken = copy.deepcopy(REPORT)
        broken["findings"][0]["evidence_ids"] = []
        with self.assertRaises(LLMError):
            validate_report(broken)
        self.connection.configure({"provider": "openai", "api_key": "key"})
        with self.assertRaises(LLMError):
            self.connection.analyze({"speed": float("nan")})


class TLSContextTests(unittest.TestCase):
    def test_existing_default_trust_is_preserved(self):
        context = Mock()
        context.cert_store_stats.return_value = {"x509_ca": 2}
        with patch("racing_dashboard.llm.ssl.create_default_context", return_value=context), \
                patch.dict(os.environ, {}, clear=True):
            self.assertIs(_verified_ssl_context(), context)
        context.load_verify_locations.assert_not_called()

    def test_empty_framework_python_trust_loads_system_ca_bundle(self):
        context = Mock()
        context.cert_store_stats.return_value = {"x509_ca": 0}

        def loaded(**kwargs):
            context.cert_store_stats.return_value = {"x509_ca": 140}

        context.load_verify_locations.side_effect = loaded
        with patch("racing_dashboard.llm.ssl.create_default_context", return_value=context), \
                patch("racing_dashboard.llm.Path.is_file", return_value=True), \
                patch.dict(os.environ, {}, clear=True):
            self.assertIs(_verified_ssl_context(), context)
        context.load_verify_locations.assert_called_once_with(cafile="/etc/ssl/cert.pem")

    def test_explicit_custom_trust_is_not_expanded_with_other_roots(self):
        for variable in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
            context = Mock()
            context.cert_store_stats.return_value = {"x509_ca": 0}
            with self.subTest(variable=variable), \
                    patch("racing_dashboard.llm.ssl.create_default_context", return_value=context), \
                    patch.dict(os.environ, {variable: "/custom/ca-location"}, clear=True):
                self.assertIs(_verified_ssl_context(), context)
            context.load_verify_locations.assert_not_called()

    def test_no_available_ca_bundle_fails_closed_with_actionable_error(self):
        context = Mock()
        context.cert_store_stats.return_value = {"x509_ca": 0}
        with patch("racing_dashboard.llm.ssl.create_default_context", return_value=context), \
                patch("racing_dashboard.llm.Path.is_file", return_value=False), \
                patch.dict(os.environ, {}, clear=True), self.assertRaises(LLMError) as caught:
            _verified_ssl_context()
        self.assertIn("trusted HTTPS certificates", str(caught.exception))
        self.assertFalse(caught.exception.retryable)
        context.load_verify_locations.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec("jwt"), "Install .[llm] to test real JWT signatures")
class IdentitySignatureTests(unittest.TestCase):
    def test_valid_signature_issuer_audience_expiry_nonce_and_subject(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
        jwk["kid"] = "fixture-key"
        with tempfile.TemporaryDirectory() as folder:
            client = LLMConnection(folder)
            client._metadata = METADATA
            client._jwks = {"keys": [jwk]}
            client._jwks_at = time.monotonic()
            claims = {"iss": ISSUER, "aud": "issued", "sub": "person", "exp": int(time.time()) + 300,
                      "iat": int(time.time()), "nonce": "expected"}
            def signed(value):
                return jwt.encode(value, private, algorithm="RS256", headers={"kid": "fixture-key"})
            self.assertEqual(client._validate_id_token(signed(claims), "issued", "expected")["sub"], "person")
            for changes in ({"iss": "https://imposter.test"}, {"aud": "another-client"},
                            {"exp": int(time.time()) - 60}, {"nonce": "wrong"}, {"sub": ""}):
                with self.subTest(changes=changes), self.assertRaises(LLMError):
                    client._validate_id_token(signed({**claims, **changes}), "issued", "expected")
            impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            forged = jwt.encode(claims, impostor, algorithm="RS256", headers={"kid": "fixture-key"})
            with self.assertRaises(LLMError):
                client._validate_id_token(forged, "issued", "expected")


if __name__ == "__main__":
    unittest.main()
