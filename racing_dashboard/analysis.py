"""Durable lap evidence and background recommendation jobs, independent of live I/O."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import zlib

from .laps import summarize_lap, validate_lap
from .llm import LLMConnection
from .discussion_service import LapDiscussionMixin, ensure_discussion_schema
from .source_context import read_source_context


def _json(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def validate_report(report, evidence):
    """Model structure is not enough: every finding must cite supplied evidence."""
    if not isinstance(report, dict) or not isinstance(report.get("summary"), str):
        raise ValueError("Model report is missing its summary")
    findings = report.get("findings")
    if not isinstance(findings, list) or len(findings) > 3:
        raise ValueError("Model report must contain at most three findings")
    available = {item["id"] for item in evidence if isinstance(item, dict) and "id" in item}
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("Model finding must be an object")
        for key in ("title", "observation", "hypothesis", "recommendation", "validation"):
            if not isinstance(finding.get(key), str) or not 1 <= len(finding[key]) <= 12000:
                raise ValueError("Model finding has an invalid " + key)
        if finding.get("confidence") not in {"low", "medium", "high"}:
            raise ValueError("Model finding has invalid confidence")
        references = finding.get("evidence_ids")
        if not isinstance(references, list) or not references or any(not isinstance(key, str) or key not in available for key in references):
            raise ValueError("Model finding cites missing or unknown evidence")
    if not isinstance(report.get("limitations"), list) or any(not isinstance(value, str) for value in report["limitations"]):
        raise ValueError("Model report has invalid limitations")
    if len(_json(report)) > 100000:
        raise ValueError("Model report is too large")
    return copy.deepcopy(report)


class AnalysisService(LapDiscussionMixin):
    def __init__(self, data_dir, *, connection=None, active=False):
        self.data_dir = Path(data_dir).expanduser()
        self.path = self.data_dir / "laps.sqlite3"
        self.settings_path = self.data_dir / "analysis_settings.json"
        self.connection = connection or LLMConnection(self.data_dir)
        self.lock = threading.RLock()
        self._store_fd = None
        self._store_closed = False
        self._store_finalizer = None
        self.config_lock = threading.Lock()
        self.wake = threading.Event()
        self.discussion_wake = threading.Event()
        self.stop = threading.Event()
        self.active = active
        self.settings = {"enabled": False, "source_repo": "", "source_ref": "HEAD"}
        self.source = read_source_context("")
        self.source_error = None
        self.summaries = []
        self.pending = 0
        self.retention = 200
        try:
            saved = json.loads(self.settings_path.read_text())
            if isinstance(saved, dict):
                for key in self.settings:
                    if key in saved and isinstance(saved[key], type(self.settings[key])):
                        self.settings[key] = saved[key]
        except (OSError, ValueError, TypeError):
            pass
        try:
            self.source = read_source_context(self.settings["source_repo"], self.settings["source_ref"])
        except (ValueError, OSError) as exc:
            self.source_error = str(exc)
        if self.path.exists():
            try:
                with self.lock, self._database() as db:
                    db.execute("UPDATE laps SET analysis_status='queued' WHERE analysis_status='analyzing'")
                    db.execute("UPDATE lap_discussions SET status='queued',revision=revision+1 WHERE status='analyzing'")
                    self._refresh(db)
            except BaseException:
                self.stop.set()
                self._release_store()
                raise
        self.thread = threading.Thread(target=self._run, name="lap-engineer", daemon=True)
        self.thread.start()
        self.discussion_thread = threading.Thread(target=self._run_discussions, name="lap-discussion", daemon=True)
        self.discussion_thread.start()

    def _acquire_store(self):
        """Own this history for the service lifetime; caller holds self.lock."""
        if self._store_closed or self.stop.is_set():
            raise ValueError("The lap history service is closed")
        if self._store_fd is not None:
            return
        import fcntl
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = None
        try:
            descriptor = os.open(self.data_dir/".lap_engine.lock",
                                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if descriptor is not None:
                os.close(descriptor)
            raise ValueError("This lap history is already open in another dashboard. Close it or choose a different data directory.") from None
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            raise ValueError("The lap history lock could not be opened safely") from None
        self._store_fd = descriptor

    def _release_store(self):
        with self.lock:
            if self._store_fd is not None:
                import fcntl
                descriptor = self._store_fd
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    try:
                        os.close(descriptor)
                    finally:
                        self._store_fd = None

    @contextmanager
    def _database(self):
        # Keep close/ownership release outside any SQLite transaction, including
        # callers that do not already hold the reentrant service lock.
        with self.lock:
            self._acquire_store()
            db = sqlite3.connect(self.path, timeout=5)
            try:
                os.chmod(self.path, 0o600)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("""CREATE TABLE IF NOT EXISTS laps (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
                    analysis_status TEXT NOT NULL, lap BLOB NOT NULL, metrics TEXT NOT NULL,
                    summary TEXT NOT NULL, report TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    provider TEXT, model TEXT, reasoning_effort TEXT,
                    source_context TEXT NOT NULL DEFAULT '{}',
                    comparison_key TEXT, lap_time_s REAL
                )""")
                columns = {row[1] for row in db.execute("PRAGMA table_info(laps)")}
                if "source_context" not in columns:
                    db.execute("ALTER TABLE laps ADD COLUMN source_context TEXT NOT NULL DEFAULT '{}'")
                if "comparison_key" not in columns:
                    db.execute("ALTER TABLE laps ADD COLUMN comparison_key TEXT")
                    db.execute("ALTER TABLE laps ADD COLUMN lap_time_s REAL")
                    for row in db.execute("SELECT id,lap FROM laps").fetchall():
                        old_lap = json.loads(zlib.decompress(row["lap"]))
                        db.execute("UPDATE laps SET comparison_key=?,lap_time_s=? WHERE id=?",
                                   (self._comparison_key(old_lap), old_lap["lap_time_s"], row["id"]))
                db.execute("CREATE INDEX IF NOT EXISTS laps_comparison ON laps(comparison_key,status,lap_time_s)")
                ensure_discussion_schema(db)
                with db:
                    yield db
            finally:
                db.close()

    def _refresh(self, db):
        rows = db.execute("""SELECT laps.id,analysis_status,summary,laps.error,laps.attempts,laps.provider,laps.model,laps.reasoning_effort,
            COALESCE(lap_discussions.status,'idle') AS discussion_status,
            COALESCE(lap_discussions.revision,0) AS discussion_revision
            FROM laps LEFT JOIN lap_discussions ON lap_discussions.lap_id=laps.id
            ORDER BY created_at DESC,laps.rowid DESC LIMIT 100""").fetchall()
        self.summaries = [{**json.loads(row["summary"]), "id": row["id"],
                           "analysis_status": row["analysis_status"], "error": row["error"],
                           "attempts": row["attempts"], "provider": row["provider"],
                           "model": row["model"], "reasoning_effort": row["reasoning_effort"],
                           "discussion_status": row["discussion_status"],
                           "discussion_revision": row["discussion_revision"]} for row in rows]
        self.pending = db.execute("SELECT COUNT(*) FROM laps WHERE analysis_status IN ('queued','analyzing','waiting_for_connection')").fetchone()[0]

    def set_active(self, active):
        with self.lock:
            self.active = bool(active)
        self.wake.set()
        self.discussion_wake.set()

    def _ready(self):
        return self.settings["enabled"] and self.active and self.connection.state().get("connected", False)

    def state(self):
        connection = self.connection.state()
        with self.lock:
            rows = copy.deepcopy(self.summaries)
            for row in rows:
                if row["analysis_status"] in {"queued", "waiting_for_connection"}:
                    row["analysis_status"] = "queued" if self.settings["enabled"] and connection.get("connected") else "waiting_for_connection"
                if row["discussion_status"] == "queued" and not connection.get("connected"):
                    row["discussion_status"] = "waiting_for_connection"
            return {"connection": {**connection, "source_repo": self.settings["source_repo"],
                                   "source_ref": self.settings["source_ref"],
                                   "source_status": self.source_error or self.source.get("status"),
                                   "source_commit": self.source.get("commit")},
                    "enabled": self.settings["enabled"], "active": self.active,
                    "laps": rows, "pending": self.pending, "retention_laps": self.retention}

    def configure(self, values):
        if not isinstance(values, dict):
            raise ValueError("Analysis settings must be an object")
        with self.config_lock:
            with self.lock:
                self._acquire_store()
            settings = dict(self.settings)
            connection_values = dict(values)
            for name in settings:
                if name in connection_values:
                    value = connection_values.pop(name)
                    if not isinstance(value, type(settings[name])):
                        raise ValueError("Invalid analysis setting: " + name)
                    settings[name] = value
            source = read_source_context(settings["source_repo"], settings["source_ref"])
            if connection_values:
                self.connection.configure(connection_values)
            with self.lock:
                self._acquire_store()
                temporary = self.settings_path.with_suffix(".tmp")
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    output.write(_json(settings))
                os.chmod(temporary, 0o600)
                temporary.replace(self.settings_path)
                self.settings, self.source, self.source_error = settings, source, None
            self.wake.set()
            self.discussion_wake.set()
        return self.state()

    def ingest(self, value):
        lap = validate_lap(value)
        # Lap validation owns numeric/size limits. A source cannot overwrite an acknowledged lap.
        with self.lock, self._database() as db:
            existing = db.execute("SELECT id FROM laps WHERE id=?", (lap["id"],)).fetchone()
            if existing:
                return {"ok": True, "id": lap["id"], "duplicate": True}
            if self.pending >= self.retention and lap["status"] == "valid":
                raise ValueError("Lap analysis backlog is full (200 laps). Restore analysis before delivering more laps; the adapter retains unacknowledged evidence.")
            count = db.execute("SELECT COUNT(*) FROM laps").fetchone()[0]
            if count >= self.retention and not db.execute("""SELECT 1 FROM laps LEFT JOIN lap_discussions
                    ON lap_discussions.lap_id=laps.id WHERE analysis_status IN ('complete','invalid')
                    AND COALESCE(lap_discussions.status,'idle') NOT IN ('queued','analyzing','failed') LIMIT 1""").fetchone():
                raise ValueError("Lap analysis backlog is full. Pending lap reports and discussions are retained until completed.")
            baseline = self._baseline(db, lap)
            metrics = summarize_lap(lap, baseline)
            summary = {key: lap.get(key) for key in ("source_session_id", "lap_number", "status", "reason", "start_time_s", "end_time_s", "lap_time_s", "route_hash")}
            summary["baseline_id"] = baseline.get("id") if baseline else None
            summary["metrics"] = {key: value for key, value in metrics.items() if key not in {"evidence", "sectors", "samples", "trace"} and not isinstance(value, (dict, list))}
            state = "queued" if lap["status"] == "valid" else "invalid"
            db.execute("INSERT INTO laps(id,created_at,status,analysis_status,lap,metrics,summary,source_context,comparison_key,lap_time_s) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (lap["id"], datetime.now(timezone.utc).isoformat(), lap["status"], state,
                        zlib.compress(_json(lap).encode(), 6), _json(metrics), _json(summary), _json(self.source),
                        self._comparison_key(lap), lap["lap_time_s"]))
            # Pending jobs survive retention; evict only old finished evidence.
            count = db.execute("SELECT COUNT(*) FROM laps").fetchone()[0]
            if count > self.retention:
                db.execute("""DELETE FROM laps WHERE id IN (SELECT laps.id FROM laps LEFT JOIN lap_discussions
                    ON lap_discussions.lap_id=laps.id WHERE analysis_status IN ('complete','invalid')
                    AND COALESCE(lap_discussions.status,'idle') NOT IN ('queued','analyzing','failed')
                    ORDER BY created_at LIMIT ?)""", (count - self.retention,))
            self._refresh(db)
        self.wake.set()
        return {"ok": True, "id": lap["id"]}

    @staticmethod
    def _comparison_key(lap):
        metadata = lap.get("metadata", {})
        comparable = {key: metadata.get(key) for key in
                      ("map_frame", "map_hash", "gate", "speed_source", "conditions", "clock_source")}
        comparable.update({"route_hash": lap.get("route_hash"), "direction": lap.get("direction", 1),
                           "pose_offset_m": metadata.get("pose_offset_m", metadata.get("pose_offset", 0))})
        return hashlib.sha256(json.dumps(comparable, sort_keys=True, allow_nan=False).encode()).hexdigest()

    @classmethod
    def _compatible(cls, first, second):
        return cls._comparison_key(first) == cls._comparison_key(second)

    def _baseline(self, db, lap):
        row = db.execute("SELECT lap FROM laps WHERE status='valid' AND comparison_key=? ORDER BY lap_time_s LIMIT 1",
                         (self._comparison_key(lap),)).fetchone()
        return json.loads(zlib.decompress(row[0])) if row else None

    def get(self, identifier):
        with self.lock:
            if not self.path.exists():
                raise KeyError("Lap not found")
            with self._database() as db:
                row = db.execute("SELECT * FROM laps WHERE id=?", (identifier,)).fetchone()
                discussion = self._discussion_get(db, identifier)
            if not row:
                raise KeyError("Lap not found")
            return {"lap": json.loads(zlib.decompress(row["lap"])), "metrics": json.loads(row["metrics"]),
                    "report": json.loads(row["report"]) if row["report"] else None,
                    "analysis_status": row["analysis_status"], "error": row["error"],
                    "source_context": json.loads(row["source_context"]),
                    "attempts": row["attempts"], "provider": row["provider"], "model": row["model"],
                    "reasoning_effort": row["reasoning_effort"], "discussion": discussion}

    def retry(self, identifier):
        with self.lock, self._database() as db:
            row = db.execute("SELECT status,analysis_status FROM laps WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ValueError("Lap not found")
            if row["status"] != "valid":
                raise ValueError("An invalid lap cannot receive performance recommendations")
            if row["analysis_status"] == "analyzing":
                raise ValueError("This lap is already being analyzed")
            db.execute("UPDATE laps SET analysis_status='queued',error=NULL,attempts=0 WHERE id=?", (identifier,))
            self._refresh(db)
        self.wake.set()
        return {"ok": True, "id": identifier}

    def export(self, identifier, recording=False):
        detail = self.get(identifier)
        if not recording:
            return {"format": "robertaris-lap-analysis", "version": 1, **detail}
        lap = detail["lap"]
        frames = []
        samples = lap["samples"]
        if len(samples) > 12000:
            # Replay's established file limit is 12,000 frames. Keep both
            # interpolated gate boundaries and preserve full samples in export.
            indices = {round(i * (len(samples) - 1) / 11999) for i in range(12000)}
            samples = [samples[i] for i in sorted(indices)]
        for sample in samples:
            frame = copy.deepcopy(sample)
            frame.update({"mode": "live", "status": "recorded", "source_time_s": sample["time_s"],
                          "time_s": max(0.0, sample["time_s"] - lap["start_time_s"])})
            frames.append(frame)
        return {"format": "robertaris-racing-dashboard", "version": 1,
                "context": {"route": lap["route"], "metadata": {**lap.get("metadata", {}),
                            "source": "Saved live lap; scalar telemetry only, scans/grids were not archived"}},
                "frames": frames, "events": [], "map": None,
                "dropped_frames": len(lap["samples"]) - len(samples),
                "lap_analysis": {key: value for key, value in detail.items() if key != "lap"}}

    @staticmethod
    def _lap_evidence(detail):
        lap, metrics = detail["lap"], detail["metrics"]
        evidence = {"lap": {key: value for key, value in lap.items() if key not in {"samples", "route"}},
                    "metrics": copy.deepcopy(metrics), "source_context": detail["source_context"],
                    "measurement_limits": [
                        "Steering is a command, not measured wheel angle. Odometry speed depends on the publisher.",
                        "Route deviation may be intentional; no collision, slip, actuator fault or solver failure can be inferred from it.",
                        "Published MPC prediction headers may use solve-completion time; geometric divergence is not a time-aligned prediction residual.",
                        "Local Git context is not verified deployed source. Missing effective settings and solver diagnostics remain unknown.",
                        "reference_speed is interpolated from the source CSV, not a verified active MPC target. The controller can cap and reshape it using MAX_SPEED and acceleration/deceleration limits.",
                        "Baseline sector differences are observed comparisons, not guaranteed achievable savings; obstacle/track conditions may differ.",
                    ]}
        evidence["lap"]["metadata"] = copy.deepcopy(metrics.get("metadata", {}))
        return evidence

    def _work_one(self):
        ready = self._ready()
        with self.lock:
            if not self.path.exists() or not ready:
                return False
            with self._database() as db:
                row = db.execute("SELECT id FROM laps WHERE analysis_status IN ('queued','waiting_for_connection') ORDER BY created_at,rowid LIMIT 1").fetchone()
                if not row:
                    return False
                identifier = row[0]
                db.execute("UPDATE laps SET analysis_status='analyzing',error=NULL,attempts=attempts+1 WHERE id=?", (identifier,))
                self._refresh(db)
        detail = self.get(identifier)
        metrics = detail["metrics"]
        evidence = self._lap_evidence(detail)
        identity = self.connection.state()
        report, error = None, None
        for attempt in range(3):
            try:
                if attempt:
                    with self.lock, self._database() as db:
                        db.execute("UPDATE laps SET attempts=attempts+1 WHERE id=?", (identifier,))
                        self._refresh(db)
                report = validate_report(self.connection.analyze(
                    evidence, on_start=lambda snapshot: identity.update(snapshot)), metrics.get("evidence", []))
                break
            except Exception as exc:
                # Provider exceptions are sanitized by LLMConnection; do not persist arbitrary payloads.
                error = str(exc)[:500] if isinstance(exc, ValueError) else "Analysis request failed; check the connection and retry this lap."
                if ((isinstance(exc, ValueError) and not getattr(exc, "retryable", False))
                        or attempt == 2 or self.stop.wait(min(2 ** attempt, 4))):
                    break
        if self.stop.is_set():
            return False
        with self.lock, self._database() as db:
            db.execute("UPDATE laps SET analysis_status=?,report=?,error=?,provider=?,model=?,reasoning_effort=? WHERE id=?",
                       ("complete" if report else "failed", _json(report) if report else None,
                        None if report else error, identity.get("provider"), identity.get("model"),
                        identity.get("reasoning_effort"), identifier))
            self._refresh(db)
        return True

    def _run(self):
        while not self.stop.is_set():
            try:
                if self._work_one():
                    continue
            except Exception:
                # Disk/provider faults must never kill telemetry ingestion or create an inference loop.
                self.stop.wait(2)
            self.wake.wait(1)
            self.wake.clear()

    def close(self):
        with self.lock:
            if self._store_closed:
                return
            self._store_closed = True
            self.stop.set()
            self.wake.set()
            self.discussion_wake.set()
        workers = (self.thread, self.discussion_thread)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=.5)
        if any(worker.is_alive() for worker in workers):
            # An outstanding paid request may still be returning. Retain the
            # history lease until it exits so a replacement service cannot
            # concurrently requeue that request. The close call stays bounded.
            def release_when_stopped():
                for worker in workers:
                    worker.join()
                self._release_store()
            self._store_finalizer = threading.Thread(target=release_when_stopped,
                                                     name="lap-store-close", daemon=True)
            self._store_finalizer.start()
        else:
            self._release_store()
