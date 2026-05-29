#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


RETENTION_CHOICES: Dict[str, int] = {
    "1w": 7,
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "1y": 365,
}


class GrowcontrolStorage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sensor_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    sensor_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    temperature REAL,
                    moisture REAL,
                    light REAL,
                    conductivity REAL,
                    battery REAL,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_sensor_readings_sensor_ts
                    ON sensor_readings(sensor_id, ts);

                CREATE TABLE IF NOT EXISTS weather_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    status TEXT NOT NULL,
                    temperature REAL,
                    humidity REAL,
                    pressure REAL,
                    wind_speed REAL,
                    description TEXT,
                    raw_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_weather_readings_ts
                    ON weather_readings(ts);

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES('retention_key', '1m')"
            )
            conn.commit()

    def insert_sensor_reading(
        self,
        *,
        ts: str,
        sensor_id: str,
        status: str,
        metrics: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> None:
        values = metrics or {}
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sensor_readings(
                    ts, sensor_id, status, temperature, moisture, light, conductivity, battery, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    sensor_id,
                    status,
                    values.get("temperature"),
                    values.get("moisture"),
                    values.get("light"),
                    values.get("conductivity"),
                    values.get("battery"),
                    error,
                ),
            )
            conn.commit()

    def insert_weather_reading(self, *, ts: str, status: str, data: Optional[Dict[str, Any]], error: Optional[str]) -> None:
        if status != "ok" or not data:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO weather_readings(
                        ts, status, temperature, humidity, pressure, wind_speed, description, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ts, status, None, None, None, None, error or "", None),
                )
                conn.commit()
            return

        main = data.get("main", {})
        wind = data.get("wind", {})
        description = ""
        weather = data.get("weather", [])
        if weather and isinstance(weather, list):
            description = str(weather[0].get("description", ""))

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO weather_readings(
                    ts, status, temperature, humidity, pressure, wind_speed, description, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    status,
                    main.get("temp"),
                    main.get("humidity"),
                    main.get("pressure"),
                    wind.get("speed"),
                    description,
                    json.dumps(data, separators=(",", ":")),
                ),
            )
            conn.commit()

    def get_retention_key(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key='retention_key'"
            ).fetchone()
            if not row:
                return "1m"
            value = str(row["value"])
            return value if value in RETENTION_CHOICES else "1m"

    def set_retention_key(self, value: str) -> None:
        if value not in RETENTION_CHOICES:
            raise ValueError(f"Invalid retention value: {value}")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings(key, value) VALUES('retention_key', ?)",
                (value,),
            )
            conn.commit()

    def retention_days(self) -> int:
        return RETENTION_CHOICES[self.get_retention_key()]

    def prune_old_data(self, days: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM sensor_readings WHERE ts < datetime('now', ?)",
                (f"-{days} days",),
            )
            conn.execute(
                "DELETE FROM weather_readings WHERE ts < datetime('now', ?)",
                (f"-{days} days",),
            )
            conn.commit()

    def clear_all_data(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sensor_readings")
            conn.execute("DELETE FROM weather_readings")
            conn.commit()

    def rename_sensor_history(self, old_id: str, new_id: str) -> int:
        if not old_id or old_id == new_id:
            return 0
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sensor_readings SET sensor_id = ? WHERE sensor_id = ?",
                (new_id, old_id),
            )
            conn.commit()
            return int(cur.rowcount)

    def get_sensor_history(
        self,
        sensor_id: str,
        *,
        limit: int = 1000,
        since_days: Optional[int] = None,
        since_hours: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        where = "sensor_id = ?"
        params: List[Any] = [sensor_id]

        if since_hours is not None:
            where += " AND ts >= datetime('now', ?)"
            params.append(f"-{int(since_hours)} hours")
        elif since_days is not None:
            where += " AND ts >= datetime('now', ?)"
            params.append(f"-{int(since_days)} days")

        params.append(max(1, min(limit, 5000)))

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT ts, status, temperature, moisture, light, conductivity, battery, error
                FROM sensor_readings
                WHERE {where}
                ORDER BY ts DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_weather_history(self, limit: int = 1000) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, status, temperature, humidity, pressure, wind_speed, description
                FROM weather_readings
                ORDER BY ts DESC
                LIMIT ?
                """,
                (max(1, min(limit, 5000)),),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]
