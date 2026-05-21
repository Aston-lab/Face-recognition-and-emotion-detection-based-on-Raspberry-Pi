from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


MIN_VALID_TS_MS = 1_704_067_200_000


def payload_ts_ms(payload: dict[str, Any], received_ms: int) -> int:
    try:
        ts_ms = int(payload.get("ts_ms") or 0)
    except (TypeError, ValueError):
        ts_ms = 0
    return ts_ms if ts_ms >= MIN_VALID_TS_MS else received_ms


class SQLiteStore:
    def __init__(self, db_path: Path, online_ttl_ms: int = 30_000) -> None:
        self.db_path = db_path
        self.online_ttl_ms = online_ttl_ms
        self._lock = Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                  device_id TEXT PRIMARY KEY,
                  role TEXT NOT NULL DEFAULT 'unknown',
                  display_name TEXT NOT NULL DEFAULT '',
                  online INTEGER NOT NULL DEFAULT 1,
                  last_seen_ms INTEGER NOT NULL,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_ms INTEGER NOT NULL,
                  updated_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS device_status (
                  device_id TEXT PRIMARY KEY,
                  status_json TEXT NOT NULL,
                  ts_ms INTEGER NOT NULL,
                  updated_ms INTEGER NOT NULL,
                  FOREIGN KEY(device_id) REFERENCES devices(device_id)
                );

                CREATE TABLE IF NOT EXISTS status_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  device_id TEXT NOT NULL,
                  status_json TEXT NOT NULL,
                  ts_ms INTEGER NOT NULL,
                  received_ms INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_status_events_device_ts
                  ON status_events(device_id, ts_ms DESC);

                CREATE TABLE IF NOT EXISTS recognition_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_device TEXT NOT NULL,
                  track_id INTEGER,
                  frame_id INTEGER,
                  name TEXT,
                  known INTEGER,
                  identity_confidence REAL,
                  emotion TEXT,
                  emotion_confidence REAL,
                  latency_ms REAL,
                  raw_json TEXT NOT NULL,
                  ts_ms INTEGER NOT NULL,
                  received_ms INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recognition_events_ts
                  ON recognition_events(ts_ms DESC);

                CREATE TABLE IF NOT EXISTS enrollment_images (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_device TEXT NOT NULL,
                  person_name TEXT NOT NULL,
                  image_url TEXT NOT NULL,
                  filename TEXT NOT NULL DEFAULT '',
                  original_filename TEXT NOT NULL DEFAULT '',
                  content_type TEXT NOT NULL DEFAULT '',
                  file_size INTEGER NOT NULL DEFAULT 0,
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  ts_ms INTEGER NOT NULL,
                  received_ms INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_enrollment_images_person_ts
                  ON enrollment_images(person_name, ts_ms DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_enrollment_images_source_person
                  ON enrollment_images(source_device, person_name);

                CREATE TABLE IF NOT EXISTS telemetry (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  device_id TEXT NOT NULL,
                  telemetry_json TEXT NOT NULL,
                  temperature REAL,
                  humidity REAL,
                  light REAL,
                  rssi REAL,
                  ts_ms INTEGER NOT NULL,
                  received_ms INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_telemetry_device_ts
                  ON telemetry(device_id, ts_ms DESC);

                CREATE TABLE IF NOT EXISTS conversation_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  device_id TEXT NOT NULL,
                  speaker TEXT NOT NULL,
                  text TEXT NOT NULL,
                  source TEXT NOT NULL DEFAULT '',
                  raw_json TEXT NOT NULL,
                  ts_ms INTEGER NOT NULL,
                  received_ms INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_conversation_events_ts
                  ON conversation_events(ts_ms DESC, id DESC);

                CREATE TABLE IF NOT EXISTS fall_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_device TEXT NOT NULL,
                  frame_id INTEGER,
                  mode TEXT,
                  fall_state TEXT,
                  message TEXT,
                  fps REAL,
                  image_url TEXT,
                  raw_json TEXT NOT NULL,
                  ts_ms INTEGER NOT NULL,
                  received_ms INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_fall_events_ts
                  ON fall_events(ts_ms DESC, id DESC);

                CREATE TABLE IF NOT EXISTS command_logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  command_id TEXT NOT NULL UNIQUE,
                  device_id TEXT NOT NULL,
                  command TEXT NOT NULL,
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  status TEXT NOT NULL DEFAULT 'pending',
                  result_json TEXT NOT NULL DEFAULT '{}',
                  error TEXT NOT NULL DEFAULT '',
                  created_ms INTEGER NOT NULL,
                  updated_ms INTEGER NOT NULL,
                  completed_ms INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_command_logs_device_status
                  ON command_logs(device_id, status, created_ms DESC);

                CREATE INDEX IF NOT EXISTS idx_command_logs_created
                  ON command_logs(created_ms DESC);
                """
            )
            self._conn.commit()

    def upsert_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = str(payload.get("device_id", "")).strip()
        if not device_id:
            raise ValueError("device_id is required")

        received_ms = now_ms()
        ts_ms = payload_ts_ms(payload, received_ms)
        role = str(payload.get("role") or payload.get("device_role") or "unknown").strip() or "unknown"
        display_name = str(payload.get("display_name") or device_id).strip()
        online = bool(payload.get("online", True))
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}

        status = payload.get("status")
        if not isinstance(status, dict):
            ignored = {"device_id", "role", "device_role", "display_name", "online", "ts_ms", "metadata", "merge_status"}
            status = {key: value for key, value in payload.items() if key not in ignored}
        status = dict(status)
        if status.get("probe") == "tcp":
            status.setdefault("network_online", online)
            status["network_seen_ms"] = received_ms
        if status.get("app") == "asdun_access":
            status.setdefault("app_online", online)
            status["app_seen_ms"] = received_ms
        if status.get("service") == "inference":
            status.setdefault("service_online", online)
            status["service_seen_ms"] = received_ms
        status.setdefault("online", online)
        metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))

        with self._lock:
            existing = self._conn.execute(
                "SELECT d.last_seen_ms, s.status_json FROM devices d LEFT JOIN device_status s ON s.device_id = d.device_id WHERE d.device_id = ?",
                (device_id,),
            ).fetchone()
            last_seen_ms = received_ms if online or existing is None else int(existing["last_seen_ms"])
            if bool(payload.get("merge_status", False)) and existing is not None:
                merged_status = _loads_json(existing["status_json"] or "{}")
                merged_status.update(status)
                status = merged_status
            status_json = json.dumps(status, ensure_ascii=False, separators=(",", ":"))

            self._conn.execute(
                """
                INSERT INTO devices(device_id, role, display_name, online, last_seen_ms, metadata_json, created_ms, updated_ms)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                  role=excluded.role,
                  display_name=excluded.display_name,
                  online=excluded.online,
                  last_seen_ms=excluded.last_seen_ms,
                  metadata_json=excluded.metadata_json,
                  updated_ms=excluded.updated_ms
                """,
                (device_id, role, display_name, int(online), last_seen_ms, metadata_json, received_ms, received_ms),
            )
            self._conn.execute(
                """
                INSERT INTO device_status(device_id, status_json, ts_ms, updated_ms)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                  status_json=excluded.status_json,
                  ts_ms=excluded.ts_ms,
                  updated_ms=excluded.updated_ms
                """,
                (device_id, status_json, ts_ms, received_ms),
            )
            self._conn.execute(
                "INSERT INTO status_events(device_id, status_json, ts_ms, received_ms) VALUES(?, ?, ?, ?)",
                (device_id, status_json, ts_ms, received_ms),
            )
            self._conn.commit()

        return self.latest_status_for(device_id)

    def latest_status_for(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT d.device_id, d.role, d.display_name, d.online, d.last_seen_ms,
                       d.metadata_json, d.created_ms, d.updated_ms,
                       s.status_json, s.ts_ms AS status_ts_ms
                FROM devices d
                LEFT JOIN device_status s ON s.device_id = d.device_id
                WHERE d.device_id = ?
                """,
                (device_id,),
            ).fetchone()
        if row is None:
            raise KeyError(device_id)
        return self._device_row_to_dict(row)

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT d.device_id, d.role, d.display_name, d.online, d.last_seen_ms,
                       d.metadata_json, d.created_ms, d.updated_ms,
                       s.status_json, s.ts_ms AS status_ts_ms
                FROM devices d
                LEFT JOIN device_status s ON s.device_id = d.device_id
                ORDER BY d.role, d.device_id
                """
            ).fetchall()
        return [self._device_row_to_dict(row) for row in rows]

    def insert_recognition_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_device = str(payload.get("source_device") or payload.get("device_id") or "unknown").strip()
        received_ms = now_ms()
        ts_ms = payload_ts_ms(payload, received_ms)
        identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
        emotion = payload.get("emotion") if isinstance(payload.get("emotion"), dict) else {}

        name = payload.get("name") or identity.get("name")
        known = payload.get("known")
        if known is None:
            known = identity.get("known")
        identity_confidence = payload.get("identity_confidence")
        if identity_confidence is None:
            identity_confidence = identity.get("confidence")
        emotion_label = payload.get("emotion") if isinstance(payload.get("emotion"), str) else emotion.get("label")
        emotion_confidence = payload.get("emotion_confidence")
        if emotion_confidence is None:
            emotion_confidence = emotion.get("confidence")

        raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO recognition_events(
                  source_device, track_id, frame_id, name, known, identity_confidence,
                  emotion, emotion_confidence, latency_ms, raw_json, ts_ms, received_ms
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_device,
                    payload.get("track_id"),
                    payload.get("frame_id"),
                    name,
                    None if known is None else int(bool(known)),
                    identity_confidence,
                    emotion_label,
                    emotion_confidence,
                    payload.get("latency_ms"),
                    raw_json,
                    ts_ms,
                    received_ms,
                ),
            )
            self._conn.commit()
            event_id = int(cur.lastrowid)
        return self.get_recognition_event(event_id)

    def get_recognition_event(self, event_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM recognition_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._recognition_row_to_dict(row)

    def list_recognition_events(
        self,
        limit: int = 100,
        person: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(5000, int(limit)))
        person_name = str(person or "").strip()
        clauses: list[str] = []
        params: list[Any] = []
        if person_name:
            clauses.append("name IS NOT NULL")
            clauses.append("LOWER(TRIM(name)) = LOWER(?)")
            params.append(person_name)
        if start_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(int(start_ms))
        if end_ms is not None:
            clauses.append("ts_ms < ?")
            params.append(int(end_ms))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(safe_limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM recognition_events {where} ORDER BY ts_ms DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._recognition_row_to_dict(row) for row in rows]

    def list_people_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM recognition_events
                WHERE known = 1
                  AND name IS NOT NULL
                  AND TRIM(name) != ''
                  AND LOWER(TRIM(name)) != 'unknown'
                ORDER BY ts_ms ASC, id ASC
                """
            ).fetchall()

        profiles: dict[str, dict[str, Any]] = {}
        for row in rows:
            event = self._recognition_row_to_dict(row)
            name = str(event.get("name") or "").strip()
            if not name:
                continue

            profile = profiles.setdefault(
                name,
                {
                    "name": name,
                    "event_count": 0,
                    "identity_confidence_sum": 0.0,
                    "first_seen_ms": None,
                    "last_seen_ms": None,
                    "sources": set(),
                    "emotions": {},
                },
            )
            profile["event_count"] += 1
            if event.get("identity_confidence") is not None:
                profile["identity_confidence_sum"] += _clamp_pct(event["identity_confidence"])
            if event.get("source_device"):
                profile["sources"].add(str(event["source_device"]))
            ts_ms = int(event.get("ts_ms") or event.get("received_ms") or 0)
            if ts_ms:
                profile["first_seen_ms"] = ts_ms if profile["first_seen_ms"] is None else min(profile["first_seen_ms"], ts_ms)
                profile["last_seen_ms"] = ts_ms if profile["last_seen_ms"] is None else max(profile["last_seen_ms"], ts_ms)

            emotion = str(event.get("emotion") or "").strip()
            if emotion:
                bucket = profile["emotions"].setdefault(emotion, {"count": 0, "confidence_sum": 0.0})
                bucket["count"] += 1
                bucket["confidence_sum"] += _clamp_pct(event.get("emotion_confidence"))

        enrollment_summaries = self._enrollment_summary_by_person()
        for key, summary in enrollment_summaries.items():
            name = str(summary.get("person_name") or "").strip()
            if not name:
                continue
            profile = profiles.setdefault(
                name,
                {
                    "name": name,
                    "event_count": 0,
                    "identity_confidence_sum": 0.0,
                    "first_seen_ms": None,
                    "last_seen_ms": None,
                    "sources": set(),
                    "emotions": {},
                },
            )
            if summary.get("source_device"):
                profile["sources"].add(str(summary["source_device"]))
            last_uploaded_ms = int(summary.get("last_uploaded_ms") or 0)
            if last_uploaded_ms:
                profile["first_seen_ms"] = (
                    last_uploaded_ms
                    if profile["first_seen_ms"] is None
                    else min(profile["first_seen_ms"], last_uploaded_ms)
                )
                profile["last_seen_ms"] = (
                    last_uploaded_ms
                    if profile["last_seen_ms"] is None
                    else max(profile["last_seen_ms"], last_uploaded_ms)
                )
            profile["enrollment_image_count"] = int(summary.get("image_count") or 0)
            profile["latest_image_url"] = summary.get("latest_image_url") or ""
            profile["last_enrollment_ms"] = last_uploaded_ms

        people: list[dict[str, Any]] = []
        for profile in profiles.values():
            event_count = int(profile["event_count"])
            total = max(1, event_count)
            emotions = []
            for label, values in profile["emotions"].items():
                count = int(values["count"])
                confidence_sum = float(values["confidence_sum"])
                emotions.append(
                    {
                        "label": label,
                        "count": count,
                        "weighted_pct": count * 100.0 / total,
                        "avg_confidence": confidence_sum / max(1, count),
                    }
                )
            emotions.sort(key=lambda item: (-item["weighted_pct"], item["label"]))
            people.append(
                {
                    "name": profile["name"],
                    "event_count": event_count,
                    "avg_identity_confidence": profile["identity_confidence_sum"] / total,
                    "first_seen_ms": profile["first_seen_ms"],
                    "last_seen_ms": profile["last_seen_ms"],
                    "sources": sorted(profile["sources"]),
                    "dominant_emotion": emotions[0] if emotions else None,
                    "emotions": emotions,
                    "enrollment_image_count": int(profile.get("enrollment_image_count") or 0),
                    "latest_image_url": profile.get("latest_image_url") or "",
                    "last_enrollment_ms": profile.get("last_enrollment_ms"),
                }
            )
        people.sort(key=lambda item: (-(item["last_seen_ms"] or 0), item["name"]))
        return people

    def _enrollment_summary_by_person(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT person_name, source_device, COUNT(*) AS image_count, MAX(ts_ms) AS last_uploaded_ms
                FROM enrollment_images
                WHERE TRIM(person_name) != ''
                GROUP BY LOWER(TRIM(person_name))
                """
            ).fetchall()
            latest_rows = self._conn.execute(
                """
                SELECT person_name, image_url
                FROM enrollment_images
                WHERE id IN (
                  SELECT id FROM enrollment_images AS latest
                  WHERE LOWER(TRIM(latest.person_name)) = LOWER(TRIM(enrollment_images.person_name))
                  ORDER BY ts_ms DESC, id DESC
                  LIMIT 1
                )
                """
            ).fetchall()

        latest_by_person = {
            str(row["person_name"] or "").strip().lower(): row["image_url"]
            for row in latest_rows
            if str(row["person_name"] or "").strip()
        }
        summaries: dict[str, dict[str, Any]] = {}
        for row in rows:
            person_name = str(row["person_name"] or "").strip()
            if not person_name:
                continue
            key = person_name.lower()
            summaries[key] = {
                "person_name": person_name,
                "source_device": row["source_device"],
                "image_count": row["image_count"],
                "last_uploaded_ms": row["last_uploaded_ms"],
                "latest_image_url": latest_by_person.get(key, ""),
            }
        return summaries

    def insert_enrollment_images(self, payload: dict[str, Any], images: list[dict[str, Any]]) -> list[dict[str, Any]]:
        source_device = str(payload.get("source_device") or payload.get("device_id") or "unknown").strip()
        person_name = str(payload.get("name") or payload.get("person_name") or "").strip()
        if not source_device:
            raise ValueError("source_device is required")
        if not person_name:
            raise ValueError("name is required")

        received_ms = now_ms()
        ts_ms = payload_ts_ms(payload, received_ms)
        row_ids: list[int] = []
        with self._lock:
            for image in images:
                image_url = str(image.get("image_url") or "").strip()
                if not image_url:
                    continue
                row_payload = {
                    "source_device": source_device,
                    "person_name": person_name,
                    "image": image,
                    "replace": bool(payload.get("replace", False)),
                }
                raw_json = json.dumps(row_payload, ensure_ascii=False, separators=(",", ":"))
                cur = self._conn.execute(
                    """
                    INSERT INTO enrollment_images(
                      source_device, person_name, image_url, filename, original_filename,
                      content_type, file_size, raw_json, ts_ms, received_ms
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_device,
                        person_name,
                        image_url,
                        str(image.get("filename") or ""),
                        str(image.get("original_filename") or ""),
                        str(image.get("content_type") or ""),
                        int(image.get("file_size") or 0),
                        raw_json,
                        ts_ms,
                        received_ms,
                    ),
                )
                row_ids.append(int(cur.lastrowid))
            self._conn.commit()

        return [self.get_enrollment_image(row_id) for row_id in row_ids]

    def get_enrollment_image(self, image_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM enrollment_images WHERE id = ?",
                (image_id,),
            ).fetchone()
        if row is None:
            raise KeyError(image_id)
        return self._enrollment_image_row_to_dict(row)

    def list_enrollment_images(
        self,
        person: str | None = None,
        source_device: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(500, int(limit)))
        clean_person = str(person or "").strip()
        clean_source = str(source_device or "").strip()
        clauses: list[str] = []
        params: list[Any] = []
        if clean_person:
            clauses.append("LOWER(TRIM(person_name)) = LOWER(?)")
            params.append(clean_person)
        if clean_source:
            clauses.append("source_device = ?")
            params.append(clean_source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(safe_limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM enrollment_images {where} ORDER BY ts_ms DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._enrollment_image_row_to_dict(row) for row in rows]

    def clear_enrollment_images(self, source_device: str, person: str) -> list[str]:
        clean_source = str(source_device or "").strip()
        clean_person = str(person or "").strip()
        if not clean_source or not clean_person:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT image_url FROM enrollment_images
                WHERE source_device = ?
                  AND LOWER(TRIM(person_name)) = LOWER(?)
                """,
                (clean_source, clean_person),
            ).fetchall()
            urls = [str(row["image_url"] or "") for row in rows if str(row["image_url"] or "")]
            self._conn.execute(
                """
                DELETE FROM enrollment_images
                WHERE source_device = ?
                  AND LOWER(TRIM(person_name)) = LOWER(?)
                """,
                (clean_source, clean_person),
            )
            self._conn.commit()
        return urls

    def insert_telemetry(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = str(payload.get("device_id", "")).strip()
        if not device_id:
            raise ValueError("device_id is required")

        received_ms = now_ms()
        ts_ms = payload_ts_ms(payload, received_ms)
        telemetry = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else None
        if telemetry is None and isinstance(payload.get("status"), dict):
            telemetry = payload.get("status")
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
        telemetry_json = json.dumps(telemetry, ensure_ascii=False, separators=(",", ":"))

        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO telemetry(
                  device_id, telemetry_json, temperature, humidity, light, rssi, ts_ms, received_ms
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    telemetry_json,
                    _float_or_none(telemetry.get("temperature")),
                    _float_or_none(telemetry.get("humidity")),
                    _float_or_none(telemetry.get("light")),
                    _float_or_none(telemetry.get("rssi")),
                    ts_ms,
                    received_ms,
                ),
            )
            self._conn.commit()
            event_id = int(cur.lastrowid)

        status_payload = {
            "device_id": device_id,
            "role": payload.get("role") or payload.get("device_role") or "esp32",
            "display_name": payload.get("display_name") or device_id,
            "online": payload.get("online", True),
            "merge_status": True,
            "status": {
                **telemetry,
                "telemetry_seen_ms": received_ms,
            },
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            "ts_ms": ts_ms,
        }
        self.upsert_status(status_payload)
        return self.get_telemetry_event(event_id)

    def get_telemetry_event(self, event_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM telemetry WHERE id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._telemetry_row_to_dict(row)

    def list_telemetry(self, device_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(1000, int(limit)))
        clean_device_id = str(device_id or "").strip()
        with self._lock:
            if clean_device_id:
                rows = self._conn.execute(
                    """
                    SELECT * FROM telemetry
                    WHERE device_id = ?
                    ORDER BY ts_ms DESC, id DESC
                    LIMIT ?
                    """,
                    (clean_device_id, safe_limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM telemetry ORDER BY ts_ms DESC, id DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
        return [self._telemetry_row_to_dict(row) for row in rows]

    def delete_telemetry_event(self, event_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM telemetry WHERE id = ?", (int(event_id),))
            self._conn.commit()
        return cur.rowcount > 0

    def clear_telemetry(self, device_id: str | None = None) -> int:
        clean_device_id = str(device_id or "").strip()
        with self._lock:
            if clean_device_id:
                cur = self._conn.execute("DELETE FROM telemetry WHERE device_id = ?", (clean_device_id,))
            else:
                cur = self._conn.execute("DELETE FROM telemetry")
            self._conn.commit()
        return int(cur.rowcount)

    def insert_conversation_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = str(payload.get("device_id") or payload.get("source_device") or "xiaozhi-mcp").strip()
        speaker = str(payload.get("speaker") or "").strip().lower()
        text = str(payload.get("text") or payload.get("message") or "").strip()
        source = str(payload.get("source") or "").strip()
        if not device_id:
            raise ValueError("device_id is required")
        if speaker not in {"user", "assistant", "system"}:
            raise ValueError("speaker must be user, assistant, or system")
        if not text:
            raise ValueError("text is required")

        received_ms = now_ms()
        ts_ms = payload_ts_ms(payload, received_ms)
        raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO conversation_events(device_id, speaker, text, source, raw_json, ts_ms, received_ms)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (device_id, speaker, text, source, raw_json, ts_ms, received_ms),
            )
            self._conn.commit()
            event_id = int(cur.lastrowid)
        return self.get_conversation_event(event_id)

    def get_conversation_event(self, event_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversation_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._conversation_row_to_dict(row)

    def list_conversation_events(self, device_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(500, int(limit)))
        clean_device_id = str(device_id or "").strip()
        with self._lock:
            if clean_device_id:
                rows = self._conn.execute(
                    """
                    SELECT * FROM conversation_events
                    WHERE device_id = ?
                    ORDER BY ts_ms DESC, id DESC
                    LIMIT ?
                    """,
                    (clean_device_id, safe_limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM conversation_events ORDER BY ts_ms DESC, id DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
        return [self._conversation_row_to_dict(row) for row in rows]

    def insert_fall_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_device = str(payload.get("source_device") or payload.get("device_id") or "unknown").strip()
        received_ms = now_ms()
        ts_ms = payload_ts_ms(payload, received_ms)
        raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO fall_events(
                  source_device, frame_id, mode, fall_state, message, fps,
                  image_url, raw_json, ts_ms, received_ms
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_device,
                    payload.get("frame_id"),
                    payload.get("mode"),
                    payload.get("fall_state"),
                    payload.get("message"),
                    _float_or_none(payload.get("fps")),
                    payload.get("image_url"),
                    raw_json,
                    ts_ms,
                    received_ms,
                ),
            )
            self._conn.commit()
            event_id = int(cur.lastrowid)
        return self.get_fall_event(event_id)

    def get_fall_event(self, event_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM fall_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._fall_row_to_dict(row)

    def list_fall_events(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(500, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM fall_events ORDER BY ts_ms DESC, id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [self._fall_row_to_dict(row) for row in rows]

    def create_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = str(payload.get("device_id", "")).strip()
        command = str(payload.get("command") or payload.get("name") or "").strip()
        if not device_id:
            raise ValueError("device_id is required")
        if not command:
            raise ValueError("command is required")

        command_id = str(payload.get("command_id") or payload.get("request_id") or "").strip()
        if not command_id:
            command_id = f"cmd-{now_ms()}-{uuid.uuid4().hex[:8]}"

        command_payload = payload.get("payload")
        if not isinstance(command_payload, dict):
            command_payload = {}
            for key in ("value", "mode", "target", "duration_ms"):
                if key in payload:
                    command_payload[key] = payload[key]

        timestamp_ms = now_ms()
        payload_json = json.dumps(command_payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO command_logs(
                      command_id, device_id, command, payload_json, status,
                      result_json, error, created_ms, updated_ms, completed_ms
                    )
                    VALUES(?, ?, ?, ?, 'pending', '{}', '', ?, ?, NULL)
                    """,
                    (command_id, device_id, command, payload_json, timestamp_ms, timestamp_ms),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("command_id already exists") from exc
        return self.get_command(command_id)

    def get_command(self, command_id: str) -> dict[str, Any]:
        clean_id = str(command_id or "").strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM command_logs WHERE command_id = ?",
                (clean_id,),
            ).fetchone()
        if row is None:
            raise KeyError(clean_id)
        return self._command_row_to_dict(row)

    def list_commands(
        self,
        device_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(500, int(limit)))
        clean_device_id = str(device_id or "").strip()
        clean_status = str(status or "").strip()
        clauses: list[str] = []
        params: list[Any] = []
        if clean_device_id:
            clauses.append("device_id = ?")
            params.append(clean_device_id)
        if clean_status:
            clauses.append("status = ?")
            params.append(clean_status)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM command_logs {where} ORDER BY created_ms DESC, id DESC LIMIT ?"
        params.append(safe_limit)
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._command_row_to_dict(row) for row in rows]

    def list_pending_commands(self, device_id: str, limit: int = 10) -> list[dict[str, Any]]:
        clean_device_id = str(device_id or "").strip()
        if not clean_device_id:
            raise ValueError("device_id is required")
        return self.list_commands(device_id=clean_device_id, status="pending", limit=limit)

    def complete_command(self, command_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        clean_id = str(command_id or "").strip()
        if not clean_id:
            raise ValueError("command_id is required")

        ok = bool(payload.get("ok", True))
        status = "completed" if ok else "failed"
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        if "message" in payload:
            result.setdefault("message", payload.get("message"))
        error = str(payload.get("error") or ("" if ok else payload.get("message") or "command failed")).strip()
        timestamp_ms = now_ms()
        result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))

        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE command_logs
                SET status = ?, result_json = ?, error = ?, updated_ms = ?, completed_ms = ?
                WHERE command_id = ?
                """,
                (status, result_json, error, timestamp_ms, timestamp_ms, clean_id),
            )
            if cur.rowcount == 0:
                raise KeyError(clean_id)
            self._conn.commit()
        return self.get_command(clean_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "devices": self.list_devices(),
            "people": self.list_people_profiles(),
            "recognition_events": self.list_recognition_events(limit=50),
            "enrollment_images": self.list_enrollment_images(limit=50),
            "telemetry": self.list_telemetry(limit=50),
            "conversation_events": self.list_conversation_events(limit=50),
            "fall_events": self.list_fall_events(limit=50),
            "commands": self.list_commands(limit=50),
            "server_time_ms": now_ms(),
        }

    def device_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS count FROM devices").fetchone()
        return int(row["count"])

    def _device_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        now = now_ms()
        last_seen_ms = int(row["last_seen_ms"])
        stale = now - last_seen_ms > self.online_ttl_ms
        stored_online = bool(row["online"])
        status = _loads_json(row["status_json"] or "{}")
        online = stored_online and not stale
        return {
            "device_id": row["device_id"],
            "role": row["role"],
            "display_name": row["display_name"] or row["device_id"],
            "online": online,
            "stale": stale,
            "last_seen_ms": last_seen_ms,
            "metadata": _loads_json(row["metadata_json"] or "{}"),
            "status": status,
            "signals": _derive_signals(
                role=str(row["role"] or ""),
                online=online,
                status=status,
                now=now,
                ttl_ms=self.online_ttl_ms,
            ),
            "status_ts_ms": row["status_ts_ms"],
            "created_ms": row["created_ms"],
            "updated_ms": row["updated_ms"],
        }

    @staticmethod
    def _recognition_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source_device": row["source_device"],
            "track_id": row["track_id"],
            "frame_id": row["frame_id"],
            "name": row["name"],
            "known": None if row["known"] is None else bool(row["known"]),
            "identity_confidence": row["identity_confidence"],
            "emotion": row["emotion"],
            "emotion_confidence": row["emotion_confidence"],
            "latency_ms": row["latency_ms"],
            "raw": _loads_json(row["raw_json"] or "{}"),
            "ts_ms": row["ts_ms"],
            "received_ms": row["received_ms"],
        }

    @staticmethod
    def _enrollment_image_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source_device": row["source_device"],
            "person_name": row["person_name"],
            "image_url": row["image_url"],
            "filename": row["filename"],
            "original_filename": row["original_filename"],
            "content_type": row["content_type"],
            "file_size": row["file_size"],
            "raw": _loads_json(row["raw_json"] or "{}"),
            "ts_ms": row["ts_ms"],
            "received_ms": row["received_ms"],
        }

    @staticmethod
    def _telemetry_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "device_id": row["device_id"],
            "telemetry": _loads_json(row["telemetry_json"] or "{}"),
            "temperature": row["temperature"],
            "humidity": row["humidity"],
            "light": row["light"],
            "rssi": row["rssi"],
            "ts_ms": row["ts_ms"],
            "received_ms": row["received_ms"],
        }

    @staticmethod
    def _conversation_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "device_id": row["device_id"],
            "speaker": row["speaker"],
            "text": row["text"],
            "source": row["source"],
            "raw": _loads_json(row["raw_json"] or "{}"),
            "ts_ms": row["ts_ms"],
            "received_ms": row["received_ms"],
        }

    @staticmethod
    def _fall_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source_device": row["source_device"],
            "frame_id": row["frame_id"],
            "mode": row["mode"],
            "fall_state": row["fall_state"],
            "message": row["message"],
            "fps": row["fps"],
            "image_url": row["image_url"],
            "raw": _loads_json(row["raw_json"] or "{}"),
            "ts_ms": row["ts_ms"],
            "received_ms": row["received_ms"],
        }

    @staticmethod
    def _command_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "command_id": row["command_id"],
            "device_id": row["device_id"],
            "command": row["command"],
            "payload": _loads_json(row["payload_json"] or "{}"),
            "status": row["status"],
            "result": _loads_json(row["result_json"] or "{}"),
            "error": row["error"],
            "created_ms": row["created_ms"],
            "updated_ms": row["updated_ms"],
            "completed_ms": row["completed_ms"],
        }


def _loads_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clamp_pct(value: Any) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, pct))


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fresh_bool(status: dict[str, Any], value_key: str, seen_key: str, now: int, ttl_ms: int) -> bool | None:
    if value_key not in status:
        return None
    seen_ms = status.get(seen_key)
    if isinstance(seen_ms, (int, float)) and now - int(seen_ms) > ttl_ms:
        return False
    return bool(status.get(value_key))


def _signal(state: str, label: str, detail: str = "") -> dict[str, str]:
    return {"state": state, "label": label, "detail": detail}


def _derive_signals(role: str, online: bool, status: dict[str, Any], now: int, ttl_ms: int) -> dict[str, dict[str, str]]:
    network_online = _fresh_bool(status, "network_online", "network_seen_ms", now, ttl_ms)
    app_online = _fresh_bool(status, "app_online", "app_seen_ms", now, ttl_ms)
    service_online = _fresh_bool(status, "service_online", "service_seen_ms", now, ttl_ms)

    if network_online is None:
        network_online = online
    if service_online is None and role == "inference_server":
        service_online = online

    cloud_connected = status.get("cloud_connected")
    inference_ready = bool(status.get("emotion_ready")) and bool(status.get("identity_ready"))
    provider = str(status.get("provider") or status.get("device") or "")

    probe_detail = str(status.get("probe_host") or "")
    network = _signal("ok", "Online", probe_detail) if network_online else _signal("bad", "Offline")

    if role == "raspberry_pi":
        if app_online is True:
            mode = str(status.get("mode") or "running")
            app = _signal("ok", "Running", mode)
            network = _signal("ok", "Online", "reported by app")
        elif app_online is False:
            app = _signal("bad", "Stopped")
        else:
            app = _signal("warn", "Unknown")

        if app_online is True:
            cloud = _signal("ok", "Connected") if bool(cloud_connected) else _signal("bad", "Disconnected")
        else:
            cloud = _signal("warn", "No app")
        inference = _signal("muted", "-", "")
    elif role == "inference_server":
        app = _signal("ok", "Running", str(status.get("service") or "inference")) if service_online else _signal("bad", "Stopped")
        cloud = _signal("muted", "-", "")
        if service_online and inference_ready:
            inference = _signal("ok", "Ready", provider)
        elif service_online:
            inference = _signal("warn", "Partial", provider)
        else:
            inference = _signal("bad", "Offline")
    elif role == "esp32":
        app = _signal("ok", "Reporting") if online else _signal("bad", "Stopped")
        cloud = _signal("muted", "-", "")
        inference = _signal("muted", "-", "")
    else:
        app = _signal("warn", "Unknown")
        cloud = _signal("muted", "-", "")
        inference = _signal("muted", "-", "")

    return {
        "network": network,
        "app": app,
        "cloud_link": cloud,
        "inference": inference,
    }

