"""A saved conversation with read-only access to lap evidence and local source."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import re
import threading
import uuid

from .source_context import read_chat_source_context


MAX_CONTEXT_BYTES = 350000
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
PENDING = {"queued", "analyzing"}


def _json(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_chat_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS engineer_chat (
        id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0, document TEXT NOT NULL,
        error TEXT, updated_at TEXT NOT NULL
    )""")


def _empty():
    return {"messages": [], "context": {}, "history_omitted_messages": 0}


class ChatMixin:
    def init_chat(self):
        self.chat_wake = threading.Event()
        if self.path.exists():
            with self.lock, self._database() as db:
                db.execute("UPDATE engineer_chat SET status='queued',revision=revision+1 WHERE status='analyzing'")
        self.chat_thread = threading.Thread(target=self._run_chat, name="racing-chat", daemon=True)
        self.chat_thread.start()

    def _chat_get(self, db):
        row = db.execute("SELECT * FROM engineer_chat WHERE id=1").fetchone()
        if not row:
            return {**_empty(), "status": "idle", "revision": 0, "error": None}
        document = json.loads(row["document"])
        # Full evidence, source contents and server paths stay out of polling responses.
        return {key: document.get(key) for key in ("messages", "context", "history_omitted_messages")} | {
            "status": row["status"], "revision": row["revision"], "error": row["error"]}

    def chat_state(self):
        with self.lock:
            if not self.path.exists():
                result = {**_empty(), "status": "idle", "revision": 0, "error": None}
            else:
                with self._database() as db:
                    result = self._chat_get(db)
        if result["status"] == "queued" and not self.connection.state().get("connected"):
            result["status"] = "waiting_for_connection"
        return result

    def send_chat(self, values):
        if not isinstance(values, dict) or set(values) - {"message", "idempotency_key", "lap_id"}:
            raise ValueError("Send a chat message and an optional captured lap")
        content = values.get("message")
        if not isinstance(content, str) or not content.strip() or len(content) > 12000:
            raise ValueError("Chat messages must contain between 1 and 12000 characters")
        content = content.strip()
        lap_id = values.get("lap_id") or None
        if lap_id is not None and (not isinstance(lap_id, str) or len(lap_id) > 300):
            raise ValueError("Choose a saved lap for chat context")
        key = values.get("idempotency_key", uuid.uuid4().hex)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", key):
            raise ValueError("Use a valid unique chat request identifier")
        digest = hashlib.sha256(_json({"message": content, "lap_id": lap_id}).encode()).hexdigest()
        with self.lock, self._database() as db:
            row = db.execute("SELECT * FROM engineer_chat WHERE id=1").fetchone()
            document = json.loads(row["document"]) if row else _empty()
            previous = next((m for m in document["messages"] if m.get("request_key") == key), None)
            if previous:
                if previous.get("request_digest") != digest:
                    raise ValueError("This request identifier was already used for another message")
                return {"ok": True, "duplicate": True, "chat": self._chat_get(db)}
            if row and row["status"] in PENDING:
                raise ValueError("Wait for the current chat reply before sending another message")
            if row and row["status"] == "failed":
                raise ValueError("Retry the failed reply or start a new chat before sending another message")
            if lap_id:
                self.get(lap_id)
            message = {"id": uuid.uuid4().hex, "role": "user", "content": content,
                       "created_at": _now(), "lap_id": lap_id,
                       "request_key": key, "request_digest": digest}
            document["messages"].append(message)
            if "lap_id" in values:
                document["selected_lap_id"] = lap_id
            document.pop("pending_context", None)
            document.pop("source_selection", None)
            if len(_json(document).encode()) > MAX_DOCUMENT_BYTES:
                raise ValueError("This chat has reached its storage limit. Start a new chat to continue")
            db.execute("""INSERT INTO engineer_chat(id,status,revision,document,updated_at)
                VALUES(1,'queued',1,?,?) ON CONFLICT(id) DO UPDATE SET status='queued',
                revision=revision+1,document=excluded.document,error=NULL,updated_at=excluded.updated_at""",
                (_json(document), _now()))
            result = {"ok": True, "chat": self._chat_get(db)}
        self.chat_wake.set()
        return result

    def retry_chat(self):
        with self.lock, self._database() as db:
            row = db.execute("SELECT status FROM engineer_chat WHERE id=1").fetchone()
            if not row or row["status"] != "failed":
                raise ValueError("Only a failed chat reply can be retried")
            db.execute("UPDATE engineer_chat SET status='queued',revision=revision+1,error=NULL,updated_at=? WHERE id=1",
                       (_now(),))
            result = {"ok": True, "chat": self._chat_get(db)}
        self.chat_wake.set()
        return result

    def reset_chat(self):
        with self.lock, self._database() as db:
            row = db.execute("SELECT status FROM engineer_chat WHERE id=1").fetchone()
            if row and row["status"] in PENDING:
                raise ValueError("Wait for the current chat reply before starting a new chat")
            db.execute("""INSERT INTO engineer_chat(id,status,revision,document,updated_at)
                VALUES(1,'idle',1,?,?) ON CONFLICT(id) DO UPDATE SET status='idle',
                revision=revision+1,document=excluded.document,error=NULL,updated_at=excluded.updated_at""",
                (_json(_empty()), _now()))
            return {"ok": True, "chat": self._chat_get(db)}

    def _chat_lap_evidence(self, identifier, query=""):
        detail = self.get(identifier)
        result = self._lap_evidence(detail)
        captured = result.pop("source_context", {})
        result["captured_source"] = {key: captured.get(key) for key in
                                     ("status", "mode", "repo_name", "branch", "ref", "commit", "snapshot_sha256", "deployment_verified")}
        result["report"] = detail["report"]
        result["confirmations"] = detail.get("discussion", {}).get("confirmations", [])
        # Raw telemetry and route can be inspected in bounded chunks on demand.
        chunk = re.fullmatch(r"(samples|route):(\d+):(\d+)", query.strip())
        if chunk:
            name, start, count = chunk[1], int(chunk[2]), min(int(chunk[3]), 100)
            values = detail["lap"][name]
            result["raw_chunk"] = {"kind": name, "start_index": start,
                                   "total_count": len(values), "values": values[start:start + count]}
        return result

    def _build_chat_context(self, document):
        # Dashboard commands can hold the dashboard lock before taking this service
        # lock, so the optional environment callback must run outside our lock.
        environment = {}
        callback = getattr(self, "chat_environment", None)
        if callable(callback):
            environment = callback()
        with self.lock:
            settings = dict(self.settings)
            with self._database() as db:
                rows = db.execute("SELECT id,summary,comparison_key,source_context FROM laps ORDER BY created_at DESC,rowid DESC").fetchall()
        catalog = []
        for row in rows:
            summary, source = json.loads(row["summary"]), json.loads(row["source_context"])
            catalog.append({"id": row["id"], **{key: summary.get(key) for key in
                ("source_session_id", "lap_number", "status", "reason", "lap_time_s", "route_hash")},
                "comparison_group": row["comparison_key"], "captured_source_commit": source.get("commit")})
        selected = document.get("selected_lap_id")
        query = document["messages"][-1]["content"]
        requested = [item["id"] for item in catalog if item["id"] in query or
                     re.search(r"\blap\s*#?\s*" + re.escape(str(item["lap_number"])) + r"\b", query, re.I)]
        valid = [item for item in catalog if item["status"] == "valid"]
        reference = next((item for item in catalog if item["id"] == selected), None) or (valid[0] if valid else None)
        comparable = [item for item in valid if reference and item["comparison_group"] == reference["comparison_group"]]
        best = min(comparable, key=lambda item: item["lap_time_s"]) if comparable else None
        identifiers = list(dict.fromkeys(([selected] if selected else []) + requested +
            ([catalog[0]["id"]] if catalog else []) + ([best["id"]] if best else [])))[:6]
        details = [self._chat_lap_evidence(identifier) for identifier in identifiers]
        if selected:
            chosen = next((item for item in catalog if item["id"] == selected), None)
            for detail in details:
                target = next((item for item in catalog if item["id"] == detail["lap"]["id"]), None)
                detail["comparable_to_selected"] = bool(chosen and target and chosen["comparison_group"] == target["comparison_group"])
        repo = settings["source_repo"] or environment.get("controller_repo", "")
        ref = settings["source_ref"] if settings["source_repo"] else environment.get("controller_ref", "")
        try:
            source = read_chat_source_context(repo, ref, query=query)
        except (ValueError, OSError) as exc:
            source = {"status": "unavailable", "files": [], "note": str(exc)[:500], "deployment_verified": False}
        # An explicit revision remains pinned throughout model-requested retrieval.
        source_selection = {"repo": repo, "ref": source.get("commit") if ref and source.get("commit") else ref,
                            "hashes": {item["path"]: item["sha256"] for item in source.get("files", [])}}
        history = [{"role": item["role"], "content": item["content"]} for item in document["messages"][-30:]]
        context = {"environment": environment, "lap_catalog": catalog, "laps": details,
                   "selected_lap_id": selected, "source_context": source, "messages": history,
                   "history_omitted_messages": document["history_omitted_messages"] + len(document["messages"]) - len(history)}
        while len(_json(context).encode()) > MAX_CONTEXT_BYTES and len(history) > 1:
            history.pop(0)
            context["history_omitted_messages"] += 1
        while len(_json(context).encode()) > MAX_CONTEXT_BYTES and len(context["laps"]) > 1:
            context["laps"].pop()
        while len(_json(context).encode()) > MAX_CONTEXT_BYTES and source.get("files"):
            omitted = source["files"].pop()
            source.setdefault("omitted_files", []).append({"path": omitted["path"], "reason": "chat context budget"})
        if len(_json(context).encode()) > MAX_CONTEXT_BYTES:
            raise ValueError("The requested lap context is too large. Ask about a smaller interval or another lap")
        preview = {"lap_count": len(catalog), "detailed_lap_ids": [item["lap"]["id"] for item in context["laps"]],
                   "selected_lap_id": selected, "history_omitted_messages": context["history_omitted_messages"],
                   "source": {key: source.get(key) for key in ("status", "mode", "repo_name", "branch", "ref", "commit", "snapshot_sha256")}}
        preview["source"].update(file_count=len(source.get("files", [])), omitted_file_count=len(source.get("omitted_files", [])))
        return context, source_selection, preview

    def _chat_retrieve(self, selection, catalog_ids, kind, identifier, query):
        if kind == "lap":
            if identifier not in catalog_ids:
                return {"error": "Choose a lap ID from the supplied lap catalog"}
            try:
                return self._chat_lap_evidence(identifier, query)
            except KeyError:
                return {"error": "This lap is no longer retained"}
        if kind == "source":
            if identifier:
                from .source_context import read_chat_source_file
                match = re.fullmatch(r"(?:start_line:)?(\d+)", query.strip())
                start = int(match[1]) if match else 1
                result = read_chat_source_file(selection["repo"], selection["ref"], identifier, start_line=start)
                expected = selection.get("hashes", {}).get(identifier)
                if expected and expected != result.get("sha256"):
                    return {"error": "This working file changed after the chat context was prepared. Send a new message to refresh it.",
                            "path": identifier}
                return result
            result = read_chat_source_context(selection["repo"], selection["ref"], query=query)
            changed = [item["path"] for item in result.get("files", [])
                       if item["path"] in selection.get("hashes", {}) and selection["hashes"][item["path"]] != item["sha256"]]
            if changed:
                return {"error": "Working source changed after this turn started. Send a new message to refresh it.", "changed_files": changed}
            return result
        return {"error": "Only lap and source retrieval are available"}

    def _chat_work_one(self):
        if not self.connection.state().get("connected"):
            return False
        with self.lock:
            if not self.path.exists() or self.stop.is_set():
                return False
            with self._database() as db:
                row = db.execute("SELECT * FROM engineer_chat WHERE id=1").fetchone()
                if not row or row["status"] != "queued":
                    return False
                document = json.loads(row["document"])
                db.execute("UPDATE engineer_chat SET status='analyzing',revision=revision+1,error=NULL WHERE id=1")
        identity = self.connection.state()
        answer, error = None, None
        try:
            # Retry uses the same frozen evidence once prepared; each new user
            # message refreshes source and laps automatically.
            context = document.get("pending_context")
            selection = document.get("source_selection")
            if not context:
                context, selection, preview = self._build_chat_context(document)
                document.update(pending_context=context, source_selection=selection, context=preview)
                with self.lock, self._database() as db:
                    db.execute("UPDATE engineer_chat SET document=?,revision=revision+1 WHERE id=1", (_json(document),))
            ids = {item["id"] for item in context["lap_catalog"]}
            def retrieve(kind, identifier, query):
                result = self._chat_retrieve(selection, ids, kind, identifier, query)
                document["context"].setdefault("retrievals", []).append({
                    "kind": kind, "identifier": identifier, "query": query,
                    "result_sha256": hashlib.sha256(_json(result).encode()).hexdigest(),
                    **{key: result[key] for key in ("path", "sha256", "start_line", "end_line", "error") if key in result}})
                return result
            answer = self.connection.chat(context,
                retrieve=retrieve,
                on_start=lambda snapshot: identity.update(snapshot))
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 64000:
                raise ValueError("The provider returned an empty or oversized chat reply")
        except Exception as exc:
            error = str(exc)[:500] if isinstance(exc, ValueError) else "Chat request failed. Check the connection and retry."
        if self.stop.is_set():
            return False
        with self.lock, self._database() as db:
            if answer and error is None:
                document["messages"].append({"id": uuid.uuid4().hex, "role": "assistant", "content": answer,
                    "created_at": _now(), "context": document["context"],
                    **{key: identity.get(key) for key in ("provider", "model", "reasoning_effort")}})
                document.pop("pending_context", None)
                document.pop("source_selection", None)
            db.execute("UPDATE engineer_chat SET status=?,revision=revision+1,document=?,error=?,updated_at=? WHERE id=1",
                       ("complete" if answer and error is None else "failed", _json(document), error, _now()))
        return True

    def _run_chat(self):
        while not self.stop.is_set():
            try:
                if self._chat_work_one():
                    continue
            except Exception:
                # Storage faults must never interrupt telemetry or spin inference.
                self.stop.wait(1)
            self.chat_wake.wait(1)
            self.chat_wake.clear()
