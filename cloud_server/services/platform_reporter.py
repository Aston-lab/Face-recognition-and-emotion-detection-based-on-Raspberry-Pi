from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import PlatformConfig


StatusProvider = Callable[[], dict[str, Any]]


class PlatformReporter:
    def __init__(self, config: PlatformConfig, status_provider: StatusProvider) -> None:
        self._config = config
        self._status_provider = status_provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._event_queue: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=max(1, config.recognition_queue_size)
        )
        self._event_thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._config.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            status_running = True
        else:
            status_running = False
        self._stop.clear()
        if not status_running:
            self._thread = threading.Thread(target=self._loop, name="platform-reporter", daemon=True)
            self._thread.start()
        if self._config.recognition_events_enabled and (
            self._event_thread is None or not self._event_thread.is_alive()
        ):
            self._event_thread = threading.Thread(
                target=self._event_loop,
                name="platform-recognition-reporter",
                daemon=True,
            )
            self._event_thread.start()

    def stop(self) -> None:
        if not self._config.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._event_thread is not None:
            self._event_thread.join(timeout=2.0)
        self._post_status(online=False)

    def enqueue_recognition_event(self, result: dict[str, Any]) -> None:
        if not self._config.enabled or not self._config.recognition_events_enabled:
            return
        event = self._build_recognition_event(result)
        if event is None:
            return
        try:
            self._event_queue.put_nowait(event)
        except queue.Full:
            try:
                self._event_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._event_queue.put_nowait(event)
            except queue.Full:
                print("[PlatformReporter] recognition event queue is full; event dropped")

    def _loop(self) -> None:
        self._post_status(online=True)
        interval_s = self._config.status_interval_ms / 1000.0
        while not self._stop.wait(interval_s):
            self._post_status(online=True)

    def _event_loop(self) -> None:
        while not self._stop.is_set() or not self._event_queue.empty():
            try:
                event = self._event_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._post_recognition_event(event)

    def _post_status(self, online: bool) -> None:
        status = self._status_provider()
        payload = {
            "device_id": self._config.device_id,
            "role": self._config.role,
            "display_name": self._config.display_name,
            "online": online,
            "status": status,
            "metadata": {
                "service": "cloud_server",
            },
            "ts_ms": int(time.time() * 1000),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self._config.base_url}/api/status",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_ms / 1000.0):
                return
        except urllib.error.HTTPError as exc:
            print(f"[PlatformReporter] status post failed: {_http_error_detail(exc)}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[PlatformReporter] status post failed: {exc}")

    def _post_recognition_event(self, event: dict[str, Any]) -> None:
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self._config.base_url}/api/events/recognition",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_ms / 1000.0):
                return
        except urllib.error.HTTPError as exc:
            print(f"[PlatformReporter] recognition event post failed: {_http_error_detail(exc)}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[PlatformReporter] recognition event post failed: {exc}")

    def _build_recognition_event(self, result: dict[str, Any]) -> dict[str, Any] | None:
        if not bool(result.get("ok", False)):
            return None

        identity = result.get("identity") if isinstance(result.get("identity"), dict) else {}
        local_identity = result.get("local_identity") if isinstance(result.get("local_identity"), dict) else {}
        emotion = result.get("emotion") if isinstance(result.get("emotion"), dict) else {}
        if not identity and not emotion:
            return None
        report_identity = identity
        identity_source = "cloud"
        if not _is_known_identity(report_identity) and _is_known_identity(local_identity):
            report_identity = local_identity
            identity_source = "local_fallback"

        if not self._config.recognition_report_unknown and not _is_known_identity(report_identity):
            return None

        source_device = str(
            result.get("source_device")
            or self._config.recognition_source_device
            or result.get("source")
            or "unknown"
        ).strip()
        if not source_device:
            source_device = "unknown"

        event: dict[str, Any] = {
            "source_device": source_device,
            "producer_device": self._config.device_id,
            "source": result.get("source", "cloud_server"),
            "track_id": result.get("track_id"),
            "frame_id": result.get("frame_id"),
            "latency_ms": result.get("latency_ms"),
            "ts_ms": int(result.get("ts_ms") or time.time() * 1000),
        }
        if report_identity:
            event["identity"] = report_identity
        if identity_source == "local_fallback":
            event["debug"] = {
                "identity_source": identity_source,
                "cloud_identity": identity,
            }
        if emotion:
            event["emotion"] = emotion
        return event

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-ASDUN-Device-Id": self._config.device_id,
        }
        if self._config.device_token:
            headers["X-ASDUN-Device-Token"] = self._config.device_token
        return headers


def _is_known_identity(identity: dict[str, Any]) -> bool:
    name = str(identity.get("name") or "").strip()
    return bool(identity.get("known") is True and name and name.lower() != "unknown")


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    body = body.strip()
    if len(body) > 500:
        body = body[:500] + "..."
    if body:
        return f"HTTP {exc.code} {exc.reason}: {body}"
    return f"HTTP {exc.code} {exc.reason}"
