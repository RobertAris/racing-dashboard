"""Durable acknowledged lap delivery, separate from latest-only live snapshots."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import time
import urllib.parse
import urllib.request
import uuid

from .laps import validate_lap


class LapOutbox:
    """Callbacks enqueue references; disk and HTTP work run on separate workers.

    Accepted bundles are durable once the writer finishes an atomic replace.
    Bounded memory/disk exhaustion is reported explicitly; unacknowledged laps
    are never evicted to make room. Shutdown drains accepted queue items.
    """
    def __init__(self, url, directory, *, report=print, max_files=256,
                 max_bytes=100*1024*1024, queue_size=64, retry_s=1.0, timeout_s=2.0):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Dashboard URL must be an http(s) URL")
        self.url = url.rstrip("/")+"/api/laps"
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.report, self.max_files, self.max_bytes = report, max_files, max_bytes
        self.retry_s, self.timeout_s = retry_s, timeout_s
        self.queue = queue.Queue(maxsize=queue_size)
        self.stop, self.wake = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.closed = False
        self.state = {"queued": 0, "pending": 0, "delivered": 0, "rejected": 0,
                      "delivery_failures": 0, "last_error": None}
        self.writer = threading.Thread(target=self._write, name="lap-spool", daemon=True)
        self.sender = threading.Thread(target=self._deliver, name="lap-delivery", daemon=True)
        self.writer.start()
        self.sender.start()

    def _error(self, text, *, rejected=False):
        with self.lock:
            self.state["last_error"] = text
            if rejected:
                self.state["rejected"] += 1
        self.report(text)

    def submit(self, bundle):
        if self.closed:
            self._error("Lap outbox is closed; lap was not accepted", rejected=True)
            return False
        try:
            self.queue.put_nowait(bundle)
            return True
        except queue.Full:
            self._error("Lap outbox memory queue is full; lap was not accepted", rejected=True)
            return False

    def diagnostics(self):
        with self.lock:
            return {**self.state, "queued": self.queue.qsize(), "directory": str(self.directory)}

    def _write(self):
        while not self.stop.is_set() or not self.queue.empty():
            try:
                bundle = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            temporary = None
            try:
                validate_lap(bundle)
                data = json.dumps(bundle, allow_nan=False, separators=(",", ":")).encode()
                # IDs are not filesystem paths. A stable hash also deduplicates
                # a retry in this process before the server acknowledges it.
                import hashlib
                name = hashlib.sha256(bundle["id"].encode()).hexdigest()+".json"
                target = self.directory/name
                files = list(self.directory.glob("*.json"))
                total = sum(path.stat().st_size for path in files if path.exists())
                if not target.exists() and (len(files) >= self.max_files or total+len(data) > self.max_bytes):
                    raise OSError("Lap outbox disk limit reached; unacknowledged evidence retained")
                temporary = self.directory/("."+uuid.uuid4().hex+".tmp")
                with temporary.open("xb") as output:
                    os.chmod(temporary, 0o600)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
                # Persist the directory entry as well as file contents.
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self.wake.set()
            except (OSError, ValueError, TypeError) as error:
                self._error(f"Lap could not be spooled: {error}", rejected=True)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                self.queue.task_done()

    def _deliver(self):
        while not self.stop.is_set():
            try:
                files = sorted(self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime)
                with self.lock:
                    self.state["pending"] = len(files)
                for path in files:
                    if self.stop.is_set():
                        break
                    try:
                        data = path.read_bytes()
                        bundle = json.loads(data)
                        validate_lap(bundle)
                        request = urllib.request.Request(self.url, data=data,
                                                         headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                            ack = json.loads(response.read(4096))
                        if ack.get("ok") is not True or ack.get("id") != bundle["id"]:
                            raise ValueError("server did not acknowledge this lap ID")
                        path.unlink()
                        with self.lock:
                            self.state["delivered"] += 1
                            self.state["last_error"] = None
                            self.state["pending"] = max(0, self.state["pending"]-1)
                    except (OSError, ValueError, TypeError) as error:
                        with self.lock:
                            self.state["delivery_failures"] += 1
                        self._error(f"Lap delivery pending, evidence retained: {error}")
                        # Continue past a malformed old file so later valid
                        # evidence can still be delivered.
            except OSError as error:
                self._error(f"Lap outbox could not be read: {error}")
            self.wake.wait(self.retry_s)
            self.wake.clear()

    def close(self):
        self.closed = True
        self.stop.set()
        self.wake.set()
        self.writer.join(timeout=5)
        self.sender.join(timeout=self.timeout_s+0.5)
        if self.writer.is_alive():
            self._error("Lap writer has not finished; retain process until queued evidence is durable")
