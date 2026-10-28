"""Read-only live telemetry and lap import from another dashboard instance.

This connection only sends GET requests. It never forwards provider settings,
credentials, commands, or model requests to the source dashboard.
"""
from __future__ import annotations

from collections import deque
import base64
import copy
import hashlib
import ipaddress
import json
import math
import struct
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .llm import _verified_ssl_context


POLL_SECONDS = .4
REQUEST_TIMEOUT = 3
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_TELEMETRY_AGE = 2.0


def validate_telemetry_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Enter an existing dashboard URL, or leave it blank for direct telemetry")
    value = value.strip().rstrip("/")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        loopback = host == "localhost"
        if host and not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"} or any(c.isspace() or ord(c) < 32 for c in value)
                or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("Use HTTPS, or HTTP on localhost through your SSH tunnel, without a path or credentials") from None
    return parsed.geturl().rstrip("/")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _safe_map(value):
    """Allow a bounded PNG map, never a URL or a local file reference to open."""
    if not isinstance(value, dict) or not value:
        return None
    image = value.get("image")
    width, height = value.get("width"), value.get("height")
    resolution, origin = value.get("resolution"), value.get("origin")
    if (type(width) is not int or type(height) is not int or not 0 < width * height <= 16_000_000
            or min(width, height) <= 0 or type(resolution) not in (int, float)
            or not math.isfinite(resolution) or resolution <= 0
            or not isinstance(origin, list) or len(origin) != 3
            or any(type(n) not in (int, float) or not math.isfinite(n) for n in origin)
            or not isinstance(image, str) or not image.startswith("data:image/png;base64,")
            or len(image) > 16 * 1024 * 1024):
        raise ValueError("Invalid upstream map")
    try:
        raw = base64.b64decode(image.split(",", 1)[1], validate=True)
        if (len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR"
                or struct.unpack(">II", raw[16:24]) != (width, height)):
            raise ValueError()
    except (ValueError, struct.error):
        raise ValueError("Invalid upstream map") from None
    result = {"image": image, "width": width, "height": height,
              "resolution": resolution, "origin": list(origin)}
    if isinstance(value.get("source"), str):
        result["source"] = value["source"][:2048]
    return result


class UpstreamTelemetry:
    def __init__(self, dashboard, *, autostart=True):
        self.dashboard = dashboard
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lap_wake = threading.Event()
        self.lock = threading.RLock()
        self._state = {"status": "disabled", "url": "", "error": None,
                       "lap_error": None, "imported_laps": 0}
        self._url = ""
        self._last_key = None
        self._last_received = None
        self._map_signature = None
        self._map_owned = False
        self._pending = deque()
        self._queued = set()
        self._retry_after = {}
        self._known = {row["id"] for row in dashboard.analysis.state().get("laps", [])}
        self._transport = threading.local()
        self.thread = threading.Thread(target=self._run, name="upstream-telemetry", daemon=True)
        self.lap_thread = threading.Thread(target=self._run_laps, name="upstream-laps", daemon=True)
        if autostart:
            self.thread.start()
            self.lap_thread.start()

    def state(self):
        with self.lock:
            return copy.deepcopy(self._state)

    def _set_state(self, **values):
        with self.lock:
            self._state.update(values)

    def _selection(self):
        with self.dashboard.lock:
            if self.stop.is_set() or self.dashboard.closed or self.dashboard.setup.get("mode") != "live":
                return ""
            return validate_telemetry_url(self.dashboard.setup.get("telemetry_url", ""))

    def _selected(self, url):
        return bool(url) and self._selection() == url

    def _read(self, url):
        if not hasattr(self._transport, "opener"):
            self._transport.opener = build_opener(_NoRedirect(), HTTPSHandler(context=_verified_ssl_context()))
        request = Request(url, headers={"Accept": "application/json"})
        with self._transport.opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_RESPONSE_BYTES:
                raise ValueError("Upstream response too large")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Upstream response too large")
        return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number")))

    def _source_changed(self, url):
        clear_map = False
        with self.lock:
            if url == self._url:
                return
            self._url = url
            self._last_key = self._last_received = self._map_signature = None
            self._pending.clear()
            self._queued.clear()
            self._retry_after.clear()
            self._state.update(url=url, status="connecting" if url else "disabled", error=None, lap_error=None)
            clear_map, self._map_owned = self._map_owned, False
        if clear_map:
            with self.dashboard.lock:
                if self.dashboard.setup.get("mode") == "live" and not self.dashboard.setup.get("map_yaml"):
                    self.dashboard.map_data = None
                    self.dashboard.map_path = None

    def _sync_map(self, value, url):
        mapped = _safe_map(value)
        if mapped is None:
            return
        signature = hashlib.sha256(json.dumps(mapped, separators=(",", ":")).encode()).hexdigest()
        if signature == self._map_signature:
            return
        with self.dashboard.lock:
            if (self.stop.is_set() or self.dashboard.closed or self.dashboard.setup.get("mode") != "live"
                    or self.dashboard.setup.get("telemetry_url", "").rstrip("/") != url
                    or self.dashboard.setup.get("map_yaml")):
                return
            self.dashboard.map_data = mapped
            self.dashboard.map_error = None
            self._map_owned = True
            self._map_signature = signature

    def _queue_laps(self, rows, url):
        if not isinstance(rows, list):
            return
        # Upstream summaries are newest first; local receive timestamps must
        # preserve their chronological order for latest-lap context and baselines.
        with self.lock:
            for item in reversed(rows[:200]):
                identifier = item.get("id") if isinstance(item, dict) else None
                if (not isinstance(identifier, str) or not 0 < len(identifier) <= 200
                        or any(ord(c) < 32 or c in "/\\" for c in identifier)
                        or identifier in self._known or identifier in self._queued
                        or self._retry_after.get(identifier, 0) > time.monotonic()):
                    continue
                self._pending.append((url, identifier))
                self._queued.add(identifier)
        self.lap_wake.set()

    def poll_once(self):
        try:
            url = self._selection()
            self._source_changed(url)
            if not url:
                self._set_state(status="disabled", url="", error=None)
                return False
            parsed = urlsplit(url)
            if (parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                    and parsed.port == getattr(self.dashboard, "listen_port", None)):
                raise ValueError("Telemetry URL points to this dashboard")
            remote = self._read(url + "/api/state")
            if not isinstance(remote, dict) or not isinstance(remote.get("setup"), dict):
                raise ValueError("Not a dashboard state")
            if not self._selected(url):
                return False
            if remote["setup"].get("mode") != "live":
                self._set_state(status="stale", error="The source dashboard is not in Live car mode.")
                return False
            analysis = remote.get("analysis")
            self._queue_laps(analysis.get("laps", []) if isinstance(analysis, dict) else [], url)
            session = remote.get("session") or {}
            if not isinstance(session, dict):
                raise ValueError("Invalid source session")
            diagnostics = session.get("diagnostics") or {}
            if not isinstance(diagnostics, dict):
                raise ValueError("Invalid source diagnostics")
            age = diagnostics.get("transport_age_s")
            if (not isinstance(session, dict) or session.get("status") != "connected" or session.get("pose") is None
                    or type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age <= MAX_TELEMETRY_AGE):
                self._set_state(status="stale", error="The source dashboard is waiting for fresh ROS telemetry.")
                return False
            sequence = session.get("source_sequence")
            key = (session.get("source_session_id"), sequence if sequence is not None else
                   session.get("source_time_s", session.get("time_s")))
            if key == self._last_key:
                stale = self._last_received is not None and time.monotonic() - self._last_received > MAX_TELEMETRY_AGE
                self._set_state(status="stale" if stale else "connected", error="No new pose samples are arriving from the source dashboard." if stale else None)
                return False
            frame = {key: copy.deepcopy(value) for key, value in session.items()
                     if key not in {"history", "trail", "summary", "events", "map_yaml"}}
            frame.setdefault("diagnostics", {})["upstream_url"] = url
            if not self._selected(url):
                return False
            self.dashboard.ingest_live(frame, transport_age_s=age)
            self._last_key = key
            self._last_received = time.monotonic() - age
            try:
                self._sync_map(remote.get("map"), url)
            except ValueError:
                # An unavailable/invalid background must not suppress telemetry.
                pass
            self._set_state(status="connected", error=None)
            return True
        except (HTTPError, URLError, OSError, ValueError, TypeError, KeyError):
            self._set_state(status="error", error="Could not read the source dashboard. Check its URL and keep the SSH tunnel running.")
            return False

    def import_one(self):
        with self.lock:
            if not self._pending:
                return False
            url, identifier = self._pending.popleft()
        try:
            if not self._selected(url):
                return False
            detail = self._read(url + "/api/laps/" + quote(identifier, safe=""))
            if (not isinstance(detail, dict) or not isinstance(detail.get("lap"), dict)
                    or detail["lap"].get("id") != identifier):
                raise ValueError("Invalid upstream lap")
            if not self._selected(url):
                return False
            source = detail.get("source_context")
            if not isinstance(source, dict) or not source:
                source = {"status": "unavailable", "deployment_verified": False,
                          "note": "No source manifest was supplied by the upstream capture."}
            result = self.dashboard.analysis.ingest(detail["lap"], source_context=source)
            with self.lock:
                self._known.add(identifier)
                if not result.get("duplicate"):
                    self._state["imported_laps"] += 1
                self._state["lap_error"] = None
            return True
        except (HTTPError, URLError, OSError, ValueError, TypeError, KeyError):
            with self.lock:
                self._retry_after[identifier] = time.monotonic() + 30
                self._state["lap_error"] = "A saved lap could not be imported yet; it remains on the source dashboard."
            return False
        finally:
            with self.lock:
                self._queued.discard(identifier)

    def _run(self):
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception:
                # A malformed remote document or local storage failure must
                # not permanently disconnect the telemetry worker.
                self._set_state(status="error", error="Telemetry synchronization failed and will retry.")
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()

    def _run_laps(self):
        while not self.stop.is_set():
            try:
                if self.import_one():
                    continue
            except Exception:
                self._set_state(lap_error="Saved lap synchronization failed and will retry.")
            self.lap_wake.wait(.5)
            self.lap_wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        self.lap_wake.set()
        for worker in (self.thread, self.lap_thread):
            if worker.is_alive() and worker is not threading.current_thread():
                worker.join(timeout=.5)
