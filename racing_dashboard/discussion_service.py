"""Saved per-lap conversations and independent, bounded background requests.

User confirmations describe a captured lap. They never become telemetry or
modify a controller; code proposals are checked against pinned source in memory.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from .discussion import validate_discussion_response, validate_unified_diff
from .laps import summarize_lap


PENDING = {"queued", "analyzing"}
FACT_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}")
REQUEST_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
MAX_MESSAGES = 40
MAX_CONTEXT_BYTES = 180000
MAX_DOCUMENT_BYTES = 1500000


def _json(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_discussion_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS lap_discussions (
        lap_id TEXT PRIMARY KEY REFERENCES laps(id) ON DELETE CASCADE,
        status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
        document TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        provider TEXT, model TEXT, reasoning_effort TEXT,
        updated_at TEXT NOT NULL
    )""")


def _empty():
    return {"messages": [], "confirmations": [], "comparison_lap_id": None}


class LapDiscussionMixin:
    def _discussion_get(self, db, identifier):
        row = db.execute("SELECT * FROM lap_discussions WHERE lap_id=?", (identifier,)).fetchone()
        if row is None:
            return {**_empty(), "status": "idle", "revision": 0, "error": None, "attempts": 0}
        document = json.loads(row["document"])
        # Queued input is frozen privately for retry/recovery, not duplicated in
        # each browser poll/export. The archive already includes its components.
        document.pop("pending_context", None)
        return {**document, "status": row["status"], "revision": row["revision"],
                "error": row["error"], "attempts": row["attempts"],
                "provider": row["provider"], "model": row["model"],
                "reasoning_effort": row["reasoning_effort"]}

    @staticmethod
    def _discussion_input(values):
        if not isinstance(values, dict) or set(values) - {
                "message", "confirmations", "remove_confirmations", "comparison_lap_id", "idempotency_key"}:
            raise ValueError("Send a discussion message, confirmations, or a lap comparison")
        message = values.get("message", "")
        if not isinstance(message, str) or len(message) > 6000:
            raise ValueError("Discussion messages must be at most 6000 characters")
        message = message.strip()
        confirmations = values.get("confirmations", [])
        removals = values.get("remove_confirmations", [])
        if not isinstance(confirmations, list) or len(confirmations) > 8:
            raise ValueError("Confirm up to eight settings at a time")
        if not isinstance(removals, list) or len(removals) > 8:
            raise ValueError("Remove up to eight confirmations at a time")
        seen, facts = set(), []
        for item in confirmations:
            if not isinstance(item, dict) or set(item) != {"key", "value"}:
                raise ValueError("Each confirmation needs a setting name and a value")
            key, value = item["key"], item["value"]
            if not isinstance(key, str) or not FACT_KEY.fullmatch(key) or key in seen:
                raise ValueError("Use unique setting names containing letters, numbers, dots, underscores or hyphens")
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError("A confirmed value must be text between 1 and 500 characters")
            seen.add(key)
            facts.append({"key": key, "value": value.strip()})
        if any(not isinstance(key, str) or not FACT_KEY.fullmatch(key) for key in removals) or set(removals) & seen:
            raise ValueError("Choose valid setting names to remove separately from new confirmations")
        if not message and not facts and not removals and not values.get("comparison_lap_id"):
            raise ValueError("Enter a question, answer a setting question, or select a comparison lap")
        if "comparison_lap_id" in values and not isinstance(values["comparison_lap_id"], (str, type(None))):
            raise ValueError("Choose a captured lap to compare")
        comparison = values.get("comparison_lap_id")
        if comparison and (len(comparison) > 300 or any(ord(char) < 32 for char in comparison)):
            raise ValueError("Choose a valid captured lap to compare")
        key = values.get("idempotency_key", uuid.uuid4().hex)
        if not isinstance(key, str) or not REQUEST_KEY.fullmatch(key):
            raise ValueError("Use a valid unique discussion request identifier")
        normalized = {"message": message, "confirmations": facts, "remove_confirmations": sorted(set(removals))}
        if "comparison_lap_id" in values:
            normalized["comparison_lap_id"] = comparison or None
        return normalized, key

    def discuss(self, identifier, values):
        values, request_key = self._discussion_input(values)
        digest = hashlib.sha256(_json(values).encode()).hexdigest()
        with self.lock:
            detail = self.get(identifier)
            with self._database() as db:
                row = db.execute("SELECT * FROM lap_discussions WHERE lap_id=?", (identifier,)).fetchone()
                document = json.loads(row["document"]) if row else _empty()
                previous = next((message for message in document["messages"]
                                 if message.get("request_key") == request_key), None)
                if previous:
                    if previous.get("request_digest") != digest:
                        raise ValueError("This request identifier was already used for a different message")
                    return {"ok": True, "id": identifier, "duplicate": True,
                            "discussion": self._discussion_get(db, identifier)}
                if not self.active:
                    raise ValueError("Switch to Live car to send a lap discussion message")
                if detail["lap"]["status"] != "valid":
                    raise ValueError("Discuss a valid full lap before proposing performance changes")
                if row and row["status"] in PENDING:
                    raise ValueError("A reply is already pending for this lap")
                if len(document["messages"]) > MAX_MESSAGES - 2:
                    raise ValueError("This discussion has reached its 20-turn limit. Continue with a later captured lap")
                timestamp = _now()
                facts = {item["key"]: item for item in document["confirmations"]}
                for key in values["remove_confirmations"]:
                    facts.pop(key, None)
                for item in values["confirmations"]:
                    facts[item["key"]] = {**item, "origin": "user_confirmed", "lap_id": identifier,
                                          "created_at": timestamp}
                if len(facts) > 40:
                    raise ValueError("Keep at most 40 current confirmations for one lap")
                document["confirmations"] = list(facts.values())
                if "comparison_lap_id" in values:
                    document["comparison_lap_id"] = values["comparison_lap_id"]
                # Capture a source once for this conversation. A source selected
                # after capture is explicitly separate from the lap's manifest.
                if "source_context" not in document:
                    captured = detail["source_context"]
                    document["source_context"] = copy.deepcopy(captured if captured.get("files") else self.source)
                    document["source_origin"] = "lap_capture" if captured.get("files") else "selected_at_discussion_start"
                message = {"id": uuid.uuid4().hex, "role": "user", "created_at": timestamp,
                           "content": values["message"] or "Review these updated confirmations and lap comparison.",
                           "confirmations": values["confirmations"], "remove_confirmations": values["remove_confirmations"],
                           "comparison_lap_id": document["comparison_lap_id"], "request_key": request_key,
                           "request_digest": digest}
                document["messages"].append(message)
                document["pending_context"] = self._discussion_context(detail, document)
                if len(_json(document).encode()) > MAX_DOCUMENT_BYTES:
                    raise ValueError("This conversation is too large. Continue with a later captured lap")
                db.execute("""INSERT INTO lap_discussions(lap_id,status,revision,document,updated_at)
                    VALUES(?,'queued',1,?,?) ON CONFLICT(lap_id) DO UPDATE SET
                    status='queued',revision=revision+1,document=excluded.document,error=NULL,attempts=0,
                    updated_at=excluded.updated_at""", (identifier, _json(document), timestamp))
                self._refresh(db)
                result = {"ok": True, "id": identifier, "discussion": self._discussion_get(db, identifier)}
        self.discussion_wake.set()
        return result

    def _discussion_context(self, detail, document):
        context = self._lap_evidence(detail)
        context.update({"source_context": document["source_context"], "source_origin": document["source_origin"],
                        "report": detail["report"], "confirmations": copy.deepcopy(document["confirmations"]),
                        "messages": [{key: item[key] for key in ("role", "content", "response", "confirmations", "remove_confirmations", "comparison_lap_id") if key in item}
                                     for item in document["messages"]], "comparison": None})
        comparison_id = document.get("comparison_lap_id")
        if comparison_id:
            if comparison_id == detail["lap"]["id"]:
                raise ValueError("Choose a different captured lap for comparison")
            saved = document.get("comparison_snapshot")
            if not saved or saved.get("lap", {}).get("id") != comparison_id:
                other = self.get(comparison_id)
                if other["lap"]["status"] != "valid" or not self._compatible(detail["lap"], other["lap"]):
                    raise ValueError("Comparison needs a valid lap with the same route, gate, map and measurement conventions")
                compared = self._lap_evidence(other)
                compared["metrics"] = summarize_lap(other["lap"], detail["lap"])
                # Keep bounded measured comparison context with the discussion,
                # even if retention later removes the other lap's raw archive.
                compared["metrics"].pop("evidence", None)
                compared.pop("source_context", None)
                saved = {**compared, "comparable": True,
                    "delta_time_s": other["lap"]["lap_time_s"] - detail["lap"]["lap_time_s"],
                    "configuration_verified": False,
                    "note": "Confirmations describe the original lap only. Different active settings and conditions on this comparison lap remain unverified."}
                document["comparison_snapshot"] = copy.deepcopy(saved)
            context["comparison"] = copy.deepcopy(saved)
        else:
            document.pop("comparison_snapshot", None)
        total_messages = len(context["messages"])
        context["messages"] = context["messages"][-10:]
        context["history_omitted_messages"] = total_messages - len(context["messages"])
        while len(_json(context).encode()) > MAX_CONTEXT_BYTES and len(context["messages"]) > 1:
            context["messages"].pop(0)
            context["history_omitted_messages"] += 1
        if len(_json(context).encode()) > MAX_CONTEXT_BYTES:
            raise ValueError("The lap discussion context is too large; use a shorter message or a smaller source manifest")
        return context

    def retry_discussion(self, identifier):
        with self.lock:
            self.get(identifier)
            if not self.active:
                raise ValueError("Switch to Live car to retry this discussion")
            with self._database() as db:
                row = db.execute("SELECT status,document FROM lap_discussions WHERE lap_id=?", (identifier,)).fetchone()
                if not row or row["status"] != "failed" or not json.loads(row["document"]).get("pending_context"):
                    raise ValueError("Only a failed discussion reply can be retried")
                db.execute("UPDATE lap_discussions SET status='queued',revision=revision+1,error=NULL,attempts=0,updated_at=? WHERE lap_id=?",
                           (_now(), identifier))
                self._refresh(db)
                result = {"ok": True, "id": identifier, "discussion": self._discussion_get(db, identifier)}
        self.discussion_wake.set()
        return result

    def discussion_patch(self, identifier, message_id, proposal_id):
        discussion = self.get(identifier)["discussion"]
        message = next((item for item in discussion["messages"] if item["id"] == message_id and item["role"] == "assistant"), None)
        proposal = next((item for item in message["response"]["proposals"] if item["id"] == proposal_id), None) if message else None
        if not proposal or proposal["kind"] != "code":
            raise KeyError("Code proposal not found")
        source = discussion.get("source_context", {})
        original = next((item for item in source.get("files", []) if item["path"] == proposal["source_file"]), None)
        if not original or source.get("commit") != proposal["source_commit"]:
            raise ValueError("The saved code proposal has no matching pinned source")
        validate_unified_diff(proposal["diff"], original["path"], original["content"])
        return proposal["diff"]

    def _work_discussion(self):
        connected = self.connection.state().get("connected", False)
        with self.lock:
            if not self.path.exists() or not self.active or not connected:
                return False
            with self._database() as db:
                row = db.execute("SELECT * FROM lap_discussions WHERE status='queued' ORDER BY updated_at,lap_id LIMIT 1").fetchone()
                if not row:
                    return False
                identifier = row["lap_id"]
                document = json.loads(row["document"])
                context = document["pending_context"]
                request_id = document["messages"][-1]["id"]
                db.execute("UPDATE lap_discussions SET status='analyzing',revision=revision+1,error=NULL,attempts=1 WHERE lap_id=?", (identifier,))
                self._refresh(db)
        identity = self.connection.state()
        response = error = None
        for attempt in range(3):
            try:
                if attempt:
                    with self.lock, self._database() as db:
                        db.execute("UPDATE lap_discussions SET attempts=attempts+1 WHERE lap_id=?", (identifier,))
                response = validate_discussion_response(self.connection.discuss(
                    context, on_start=lambda snapshot: identity.update(snapshot)), context)
                break
            except Exception as exc:
                error = str(exc)[:500] if isinstance(exc, ValueError) else "Discussion request failed; check the connection and retry."
                if ((isinstance(exc, ValueError) and not getattr(exc, "retryable", False))
                        or attempt == 2 or self.stop.wait(min(2 ** attempt, 4))):
                    break
        if self.stop.is_set():
            return False
        with self.lock, self._database() as db:
            current = db.execute("SELECT document FROM lap_discussions WHERE lap_id=?", (identifier,)).fetchone()
            if not current:
                return True
            document = json.loads(current["document"])
            if document["messages"][-1]["id"] != request_id:
                return True
            if response:
                document["messages"].append({"id": uuid.uuid4().hex, "role": "assistant", "created_at": _now(),
                    "response": response, "provider": identity.get("provider"), "model": identity.get("model"),
                    "reasoning_effort": identity.get("reasoning_effort"),
                    "confirmation_snapshot": copy.deepcopy(context["confirmations"]),
                    "comparison_lap_id": document.get("comparison_lap_id"),
                    "context_sha256": hashlib.sha256(_json(context).encode()).hexdigest()})
                document.pop("pending_context", None)
            db.execute("""UPDATE lap_discussions SET status=?,revision=revision+1,document=?,error=?,
                provider=?,model=?,reasoning_effort=?,updated_at=? WHERE lap_id=?""",
                ("idle" if response else "failed", _json(document), None if response else error,
                 identity.get("provider"), identity.get("model"), identity.get("reasoning_effort"), _now(), identifier))
            self._refresh(db)
        return True

    def _run_discussions(self):
        while not self.stop.is_set():
            try:
                if self._work_discussion():
                    continue
            except Exception:
                self.stop.wait(2)
            self.discussion_wake.wait(1)
            self.discussion_wake.clear()
