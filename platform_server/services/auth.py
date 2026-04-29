from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException


@dataclass(frozen=True)
class DeviceAuthConfig:
    enabled: bool
    tokens: dict[str, str]
    admin_token: str


def load_device_auth_config() -> DeviceAuthConfig:
    enabled = os.getenv("ASDUN_DEVICE_AUTH_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return DeviceAuthConfig(
        enabled=enabled,
        tokens=_parse_tokens(os.getenv("ASDUN_DEVICE_TOKENS", "")),
        admin_token=os.getenv("ASDUN_ADMIN_TOKEN", "").strip(),
    )


def require_device_auth(
    config: DeviceAuthConfig,
    payload: dict[str, Any],
    header_device_id: str | None,
    header_token: str | None,
) -> None:
    if not config.enabled:
        return

    device_id = _device_id_from_payload(payload, header_device_id)
    if not device_id:
        raise HTTPException(status_code=401, detail={"ok": False, "error": "device_id is required"})

    expected = config.tokens.get(device_id)
    if not expected:
        raise HTTPException(status_code=403, detail={"ok": False, "error": "device is not registered"})

    supplied = str(header_token or payload.get("device_token") or "").strip()
    if not supplied or supplied != expected:
        raise HTTPException(status_code=403, detail={"ok": False, "error": "invalid device token"})


def public_auth_status(config: DeviceAuthConfig) -> dict[str, Any]:
    return {
        "device_auth_enabled": config.enabled,
        "admin_auth_enabled": bool(config.admin_token),
        "registered_device_count": len(config.tokens),
    }


def require_admin_auth(config: DeviceAuthConfig, header_token: str | None) -> None:
    if not config.admin_token:
        return
    supplied = str(header_token or "").strip()
    if not supplied or supplied != config.admin_token:
        raise HTTPException(status_code=403, detail={"ok": False, "error": "invalid admin token"})


def _device_id_from_payload(payload: dict[str, Any], header_device_id: str | None) -> str:
    for value in (
        header_device_id,
        payload.get("device_id"),
        payload.get("source_device"),
        payload.get("producer_device"),
    ):
        device_id = str(value or "").strip()
        if device_id:
            return device_id
    return ""


def _parse_tokens(value: str) -> dict[str, str]:
    raw = value.strip()
    if not raw:
        return {}

    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return {
                str(device_id).strip(): str(token).strip()
                for device_id, token in parsed.items()
                if str(device_id).strip() and str(token).strip()
            }
        return {}

    tokens: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        device_id, token = part.split("=", 1)
        device_id = device_id.strip()
        token = token.strip()
        if device_id and token:
            tokens[device_id] = token
    return tokens
