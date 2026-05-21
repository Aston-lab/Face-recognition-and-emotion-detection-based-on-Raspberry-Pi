from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

try:
    from .services.auth import load_device_auth_config, public_auth_status, require_admin_auth, require_device_auth
    from .services.store import SQLiteStore
    from .services.prober import PlatformProber, default_probes
except ImportError:  # Allows `uvicorn app:app` from inside platform_server/.
    from services.auth import load_device_auth_config, public_auth_status, require_admin_auth, require_device_auth
    from services.store import SQLiteStore
    from services.prober import PlatformProber, default_probes


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("ASDUN_PLATFORM_DB", ROOT / "data" / "asdun_platform.sqlite"))
ONLINE_TTL_MS = int(os.getenv("ASDUN_PLATFORM_ONLINE_TTL_MS", "30000"))
UPLOAD_ROOT = Path(os.getenv("ASDUN_PLATFORM_UPLOAD_DIR", ROOT / "data" / "uploads"))
FALL_IMAGE_DIR = UPLOAD_ROOT / "fall"
FALL_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="ASDUN Platform Server",
    version="0.1.0",
    description="Device status, recognition events, and WebSocket dashboard for ASDUN.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/uploads", StaticFiles(directory=UPLOAD_ROOT), name="uploads")

store = SQLiteStore(DB_PATH, online_ttl_ms=ONLINE_TTL_MS)
device_auth = load_device_auth_config()

IP_LOCATION_CACHE_TTL_SECONDS = 6 * 60 * 60
ip_location_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class WebSocketHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        async with self._lock:
            connections = list(self._connections)
        if not connections:
            return

        stale: list[WebSocket] = []
        for websocket in connections:
            try:
                await websocket.send_json(message)
            except Exception:
                stale.append(websocket)
        if stale:
            async with self._lock:
                for websocket in stale:
                    self._connections.discard(websocket)


hub = WebSocketHub()


def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    if request.client:
        return request.client.host

    return ""


def is_public_ip(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    )


def locate_public_ip(ip: str) -> dict[str, Any]:
    base_location: dict[str, Any] = {
        "ip": ip,
        "country": "",
        "province": "",
        "city": "",
        "district": "",
        "address": "unknown",
        "latitude": None,
        "longitude": None,
        "provider": "ipwho.is",
    }
    if not is_public_ip(ip):
        return base_location

    now = time.time()
    cached = ip_location_cache.get(ip)
    if cached and now - cached[0] < IP_LOCATION_CACHE_TTL_SECONDS:
        return cached[1]

    location = dict(base_location)
    url = f"https://ipwho.is/{urllib.parse.quote(ip)}?lang=en"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
        if data.get("success"):
            province = data.get("region") or ""
            city = data.get("city") or ""
            district = ""
            address = " ".join(part for part in [province, city, district] if part)
            location.update(
                {
                    "country": data.get("country") or "",
                    "province": province,
                    "city": city,
                    "district": district,
                    "address": address or data.get("country") or "unknown",
                    "latitude": data.get("latitude"),
                    "longitude": data.get("longitude"),
                }
            )
        else:
            location["address"] = "lookup_failed"
    except Exception:
        location["address"] = "lookup_failed"

    ip_location_cache[ip] = (now, location)
    return location
prober = PlatformProber(store, default_probes(), hub.broadcast)


@app.on_event("startup")
async def startup() -> None:
    await prober.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    await prober.stop()


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    html_path = ROOT / "static" / "home_unlock_preview.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    html_path = ROOT / "static" / "index.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "asdun-platform-server",
        "db_path": str(DB_PATH),
        "device_count": store.device_count(),
        "online_ttl_ms": ONLINE_TTL_MS,
        "probes": [
            {
                "device_id": probe.device_id,
                "display_name": probe.display_name,
                "host": probe.host,
                "port": probe.port,
            }
            for probe in prober.probes
        ],
    }


@app.get("/api/config/public")
def public_config() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "asdun-platform-server",
        "api_version": app.version,
        **public_auth_status(device_auth),
        "endpoints": {
            "snapshot": "/api/snapshot",
            "status": "/api/status",
            "recognition_events": "/api/events/recognition",
            "conversation_events": "/api/events/conversation",
            "fall_events": "/api/events/fall",
            "telemetry": "/api/telemetry",
            "commands": "/api/commands",
            "pending_commands": "/api/commands/pending",
            "websocket": "/ws",
        },
    }


@app.post("/api/admin/verify")
def verify_admin_token(
    x_asdun_admin_token: str | None = Header(default=None, alias="X-ASDUN-Admin-Token"),
) -> dict[str, Any]:
    require_admin_auth(device_auth, x_asdun_admin_token)
    return {"ok": True}


@app.get("/api/snapshot")
def snapshot() -> dict[str, Any]:
    return {"ok": True, **store.snapshot()}


@app.get("/api/devices")
def devices() -> dict[str, Any]:
    return {"ok": True, "devices": store.list_devices()}


@app.get("/api/status/latest")
def latest_status() -> dict[str, Any]:
    return {"ok": True, "devices": store.list_devices()}


@app.get("/api/people")
def people() -> dict[str, Any]:
    return {"ok": True, "people": store.list_people_profiles()}


@app.post("/api/status")
async def post_status(
    payload: dict[str, Any] = Body(...),
    x_asdun_device_id: str | None = Header(default=None, alias="X-ASDUN-Device-Id"),
    x_asdun_device_token: str | None = Header(default=None, alias="X-ASDUN-Device-Token"),
) -> dict[str, Any]:
    require_device_auth(device_auth, payload, x_asdun_device_id, x_asdun_device_token)
    try:
        device = store.upsert_status(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"ok": False, "error": str(exc)}) from exc

    message = {"type": "device_status", "device": device}
    await hub.broadcast(message)
    return {"ok": True, "device": device}


@app.get("/api/events/recognition")
def recognition_events(
    limit: int = Query(100, ge=1, le=500),
    person: str | None = Query(None),
) -> dict[str, Any]:
    return {"ok": True, "events": store.list_recognition_events(limit=limit, person=person)}


@app.post("/api/events/recognition")
async def post_recognition_event(
    payload: dict[str, Any] = Body(...),
    x_asdun_device_id: str | None = Header(default=None, alias="X-ASDUN-Device-Id"),
    x_asdun_device_token: str | None = Header(default=None, alias="X-ASDUN-Device-Token"),
) -> dict[str, Any]:
    require_device_auth(device_auth, payload, x_asdun_device_id, x_asdun_device_token)
    event = store.insert_recognition_event(payload)
    await hub.broadcast({"type": "recognition_event", "event": event})
    await hub.broadcast({"type": "people", "people": store.list_people_profiles()})
    return {"ok": True, "event": event}


@app.get("/api/events/conversation")
def conversation_events(
    device_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    return {"ok": True, "events": store.list_conversation_events(device_id=device_id, limit=limit)}


@app.post("/api/events/conversation")
async def post_conversation_event(
    payload: dict[str, Any] = Body(...),
    x_asdun_device_id: str | None = Header(default=None, alias="X-ASDUN-Device-Id"),
    x_asdun_device_token: str | None = Header(default=None, alias="X-ASDUN-Device-Token"),
) -> dict[str, Any]:
    require_device_auth(device_auth, payload, x_asdun_device_id, x_asdun_device_token)
    try:
        event = store.insert_conversation_event(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"ok": False, "error": str(exc)}) from exc

    await hub.broadcast({"type": "conversation_event", "event": event})
    return {"ok": True, "event": event}


@app.get("/api/events/fall")
def fall_events(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return {"ok": True, "events": store.list_fall_events(limit=limit)}


@app.post("/api/events/fall")
async def post_fall_event(
    source_device: str = Form(...),
    frame_id: int | None = Form(default=None),
    ts_ms: int | None = Form(default=None),
    mode: str = Form(default=""),
    alert_action: str = Form(default="raise"),
    fall_state: str = Form(default="FallDetected"),
    message: str = Form(default=""),
    fps: float | None = Form(default=None),
    image: UploadFile | None = File(default=None),
    x_asdun_device_id: str | None = Header(default=None, alias="X-ASDUN-Device-Id"),
    x_asdun_device_token: str | None = Header(default=None, alias="X-ASDUN-Device-Token"),
) -> dict[str, Any]:
    auth_payload = {"device_id": source_device}
    require_device_auth(device_auth, auth_payload, x_asdun_device_id, x_asdun_device_token)

    image_url = ""
    if image is not None:
        suffix = Path(image.filename or "").suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".jpg"
        image_name = f"fall-{uuid.uuid4().hex}{suffix}"
        image_path = FALL_IMAGE_DIR / image_name
        content = await image.read()
        if content:
            image_path.write_bytes(content)
            image_url = f"/uploads/fall/{image_name}"

    payload: dict[str, Any] = {
        "source_device": source_device,
        "frame_id": frame_id,
        "ts_ms": ts_ms,
        "mode": mode,
        "alert_action": alert_action,
        "fall_state": fall_state,
        "message": message,
        "fps": fps,
        "image_url": image_url,
    }
    event = store.insert_fall_event(payload)
    action_text = str(alert_action or "").lower()
    state_text = str(fall_state or "").lower()
    detected = action_text != "clear" and "fall" in state_text and "normal" not in state_text and "recover" not in state_text
    device = store.upsert_status(
        {
            "device_id": source_device,
            "role": "visual_fall_detector",
            "display_name": "Visual Fall Detector",
            "online": True,
            "merge_status": True,
            "ts_ms": event["ts_ms"],
            "status": {
                "visual_fall_detected": detected,
                "alert_action": alert_action,
                "fall_state": fall_state,
                "mode": mode,
                "frame_id": frame_id,
                "fps": fps,
                "message": message,
                "image_url": image_url,
            },
        }
    )
    await hub.broadcast({"type": "fall_event", "event": event})
    await hub.broadcast({"type": "device_status", "device": device})
    return {"ok": True, "event": event, "device": device}


@app.get("/api/telemetry")
def telemetry(
    device_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    return {"ok": True, "telemetry": store.list_telemetry(device_id=device_id, limit=limit)}


@app.delete("/api/telemetry/{event_id}")
async def delete_telemetry_event(
    event_id: int,
    x_asdun_admin_token: str | None = Header(default=None, alias="X-ASDUN-Admin-Token"),
) -> dict[str, Any]:
    require_admin_auth(device_auth, x_asdun_admin_token)
    deleted = store.delete_telemetry_event(event_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "telemetry not found"})
    await hub.broadcast({"type": "telemetry_deleted", "id": event_id})
    return {"ok": True, "deleted": 1, "id": event_id}


@app.delete("/api/telemetry")
async def clear_telemetry(
    device_id: str | None = Query(None),
    x_asdun_admin_token: str | None = Header(default=None, alias="X-ASDUN-Admin-Token"),
) -> dict[str, Any]:
    require_admin_auth(device_auth, x_asdun_admin_token)
    deleted = store.clear_telemetry(device_id=device_id)
    await hub.broadcast({"type": "telemetry_cleared", "device_id": device_id or "", "deleted": deleted})
    return {"ok": True, "deleted": deleted}


@app.post("/api/telemetry")
async def post_telemetry(
    request: Request,
    payload: dict[str, Any] = Body(...),
    x_asdun_device_id: str | None = Header(default=None, alias="X-ASDUN-Device-Id"),
    x_asdun_device_token: str | None = Header(default=None, alias="X-ASDUN-Device-Token"),
) -> dict[str, Any]:
    require_device_auth(device_auth, payload, x_asdun_device_id, x_asdun_device_token)
    client_ip = get_client_ip(request)
    location = await asyncio.to_thread(locate_public_ip, client_ip)
    telemetry = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else None
    if telemetry is None:
        telemetry = payload.get("status") if isinstance(payload.get("status"), dict) else None
    if telemetry is None:
        ignored = {
            "device_id",
            "role",
            "device_role",
            "display_name",
            "online",
            "ts_ms",
            "metadata",
            "status",
            "device_token",
        }
        telemetry = {key: value for key, value in payload.items() if key not in ignored}
    telemetry = dict(telemetry)
    telemetry["client_ip"] = client_ip
    telemetry["location"] = location
    payload["telemetry"] = telemetry
    try:
        event = store.insert_telemetry(payload)
        device = store.latest_status_for(event["device_id"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"ok": False, "error": str(exc)}) from exc

    await hub.broadcast({"type": "telemetry", "event": event})
    await hub.broadcast({"type": "device_status", "device": device})
    return {"ok": True, "event": event}


@app.get("/api/commands")
def commands(
    device_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    return {"ok": True, "commands": store.list_commands(device_id=device_id, status=status, limit=limit)}


@app.post("/api/commands")
async def post_command(
    payload: dict[str, Any] = Body(...),
    x_asdun_admin_token: str | None = Header(default=None, alias="X-ASDUN-Admin-Token"),
) -> dict[str, Any]:
    require_admin_auth(device_auth, x_asdun_admin_token)
    try:
        command = store.create_command(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"ok": False, "error": str(exc)}) from exc

    await hub.broadcast({"type": "command", "command": command})
    return {"ok": True, "command": command}


@app.get("/api/commands/pending")
def pending_commands(
    device_id: str = Query(...),
    limit: int = Query(10, ge=1, le=100),
    x_asdun_device_id: str | None = Header(default=None, alias="X-ASDUN-Device-Id"),
    x_asdun_device_token: str | None = Header(default=None, alias="X-ASDUN-Device-Token"),
) -> dict[str, Any]:
    require_device_auth(device_auth, {"device_id": device_id}, x_asdun_device_id, x_asdun_device_token)
    try:
        pending = store.list_pending_commands(device_id=device_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"ok": False, "error": str(exc)}) from exc
    return {"ok": True, "commands": pending}


@app.post("/api/commands/{command_id}/result")
async def post_command_result(
    command_id: str,
    payload: dict[str, Any] = Body(...),
    x_asdun_device_id: str | None = Header(default=None, alias="X-ASDUN-Device-Id"),
    x_asdun_device_token: str | None = Header(default=None, alias="X-ASDUN-Device-Token"),
) -> dict[str, Any]:
    require_device_auth(device_auth, payload, x_asdun_device_id, x_asdun_device_token)
    try:
        command = store.get_command(command_id)
        result_device_id = str(x_asdun_device_id or payload.get("device_id") or "").strip()
        if result_device_id and result_device_id != command["device_id"]:
            raise HTTPException(status_code=403, detail={"ok": False, "error": "device_id does not match command"})
        command = store.complete_command(command_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "command not found"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"ok": False, "error": str(exc)}) from exc

    await hub.broadcast({"type": "command_result", "command": command})
    return {"ok": True, "command": command}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await hub.connect(websocket)
    try:
        await websocket.send_json({"type": "snapshot", **store.snapshot()})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(websocket)
    except Exception:
        await hub.disconnect(websocket)
        raise

