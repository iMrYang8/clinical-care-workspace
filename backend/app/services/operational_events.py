"""Minimal PHI-safe operational-event repository with bounded retention.

This sink deliberately owns its schema and retention enforcement rather than
delegating deletion to an unspecified logging deployment.  Only the fields
needed to operate the HTTP service are accepted; URLs, query strings, headers,
bodies, identities, and exception messages have no representation here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import settings

_SAFE_ROUTE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_SAFE_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_logger = logging.getLogger("nightingale.operational_events")


@dataclass(frozen=True)
class OperationalEvent:
    id: int
    occurred_at: datetime
    expires_at: datetime
    request_id: str
    route: str
    method: str
    status: int
    duration_ms: int


def _database_path() -> Path:
    return Path(settings.OPERATIONAL_EVENT_DB_PATH).expanduser()


def _connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _retention_days(retention_days: int | None = None) -> int:
    configured = (
        settings.OBSERVABILITY_RETENTION_DAYS
        if retention_days is None
        else retention_days
    )
    return min(30, max(1, int(configured)))


def _expiry(occurred_at: datetime, retention_days: int | None = None) -> datetime:
    return occurred_at + timedelta(days=_retention_days(retention_days))


def _create_operational_events_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE operational_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          occurred_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          request_id TEXT NOT NULL,
          route TEXT NOT NULL,
          method TEXT NOT NULL,
          status INTEGER NOT NULL,
          duration_ms INTEGER NOT NULL
        )
        """
    )


def _migrate_legacy_store(connection: sqlite3.Connection) -> None:
    """Upgrade the repository-owned v1 schema without extending retention.

    The original sink stored only ``occurred_at``.  Rebuilding the small local
    table makes ``expires_at`` addressable and non-null for both legacy and new
    rows.  Existing rows receive exactly the configured retention window from
    their original timestamp; migration time never becomes a new retention
    origin.
    """

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(operational_events)")
    }
    if "expires_at" in columns:
        return

    legacy_rows = connection.execute(
        """
        SELECT id, occurred_at, request_id, route, method, status, duration_ms
        FROM operational_events
        ORDER BY id
        """
    ).fetchall()
    connection.execute("ALTER TABLE operational_events RENAME TO operational_events_v1")
    _create_operational_events_table(connection)
    migrated_rows: list[tuple[object, ...]] = []
    for row in legacy_rows:
        occurred_at = _normalize_occurred_at(
            datetime.fromisoformat(str(row["occurred_at"]))
        )
        migrated_rows.append(
            (
                int(row["id"]),
                occurred_at.isoformat(),
                _expiry(occurred_at).isoformat(),
                str(row["request_id"]),
                str(row["route"]),
                str(row["method"]),
                int(row["status"]),
                int(row["duration_ms"]),
            )
        )
    connection.executemany(
        """
        INSERT INTO operational_events (
          id, occurred_at, expires_at, request_id, route, method, status, duration_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        migrated_rows,
    )
    connection.execute("DROP TABLE operational_events_v1")


def initialize_operational_event_store() -> None:
    """Create the allowlisted local repository and enforce file permissions."""

    path = _database_path()
    with _connect() as connection:
        # Four Uvicorn workers share the mounted SQLite file. Acquire the write
        # lock before inspecting the schema so only one worker can rebuild a
        # legacy table while the others wait for the committed v2 shape.
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'operational_events'
            """
        ).fetchone()
        if exists is None:
            _create_operational_events_table(connection)
        else:
            _migrate_legacy_store(connection)
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_operational_events_occurred_at
            ON operational_events (occurred_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_operational_events_expires_at
            ON operational_events (expires_at)
            """
        )
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Some container volume drivers do not expose chmod. The repository
        # still contains only allowlisted non-PHI fields.
        pass


def _normalize_occurred_at(value: datetime | None) -> datetime:
    occurred_at = value or datetime.now(UTC)
    if occurred_at.utcoffset() is None:
        raise ValueError("OPERATIONAL_EVENT_TIMESTAMP_NAIVE")
    return occurred_at.astimezone(UTC)


def _normalize_request_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return str(uuid.uuid4())


def _normalize_route(value: str | None) -> str:
    return (
        value
        if isinstance(value, str) and _SAFE_ROUTE.fullmatch(value)
        else "unmatched"
    )


def _normalize_method(value: str) -> str:
    candidate = value.upper()
    return candidate if candidate in _SAFE_METHODS else "OTHER"


def purge_operational_events(
    *, now: datetime | None = None, retention_days: int | None = None
) -> int:
    """Delete events outside the repository-owned retention window."""

    initialize_operational_event_store()
    normalized_now = _normalize_occurred_at(now)
    with _connect() as connection:
        # ``retention_days`` remains available to deterministic callers that
        # need to tighten the current policy.  Persisted expiry is authoritative
        # for the ordinary scheduled and write-path purge.
        if retention_days is not None:
            cutoff = normalized_now - timedelta(days=_retention_days(retention_days))
            cursor = connection.execute(
                """
                DELETE FROM operational_events
                WHERE expires_at <= ? OR occurred_at < ?
                """,
                (normalized_now.isoformat(), cutoff.isoformat()),
            )
            return max(0, int(cursor.rowcount))
        cursor = connection.execute(
            "DELETE FROM operational_events WHERE expires_at <= ?",
            (normalized_now.isoformat(),),
        )
        return max(0, int(cursor.rowcount))


async def run_operational_event_purge_loop(
    *,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    interval_seconds: float | None = None,
) -> None:
    """Purge on a timer even when the deployment receives no requests.

    The loop accepts a clock and sleeper so retention can be proven without
    waiting for wall time.  Error logs contain only a static machine code; no
    SQLite exception, file path, request, query, or event value is emitted.
    Cancellation is deliberately propagated for clean lifespan shutdown.
    """

    current_time = clock or (lambda: datetime.now(UTC))
    interval = max(
        1.0,
        float(
            settings.OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS
            if interval_seconds is None
            else interval_seconds
        ),
    )
    while True:
        await sleep(interval)
        try:
            purge_operational_events(now=current_time())
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.error("operational_event_purge_failed code=STORE_ERROR")


def record_operational_event(
    *,
    request_id: str,
    route: str | None,
    method: str,
    status: int,
    duration_ms: int,
    occurred_at: datetime | None = None,
) -> None:
    """Insert one sanitized event and enforce retention in the same commit."""

    initialize_operational_event_store()
    normalized_now = _normalize_occurred_at(occurred_at)
    expires_at = _expiry(normalized_now)
    normalized_status = status if 100 <= int(status) <= 599 else 500
    normalized_duration = max(0, min(int(duration_ms), 86_400_000))
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO operational_events (
              occurred_at, expires_at, request_id, route, method, status, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_now.isoformat(),
                expires_at.isoformat(),
                _normalize_request_id(request_id),
                _normalize_route(route),
                _normalize_method(method),
                normalized_status,
                normalized_duration,
            ),
        )
        connection.execute(
            "DELETE FROM operational_events WHERE expires_at <= ?",
            (normalized_now.isoformat(),),
        )


def list_operational_events() -> list[OperationalEvent]:
    """Return events for deterministic repository tests, oldest first."""

    initialize_operational_event_store()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, occurred_at, expires_at, request_id, route, method,
                   status, duration_ms
            FROM operational_events
            ORDER BY occurred_at, id
            """
        ).fetchall()
    return [
        OperationalEvent(
            id=int(row["id"]),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            request_id=str(row["request_id"]),
            route=str(row["route"]),
            method=str(row["method"]),
            status=int(row["status"]),
            duration_ms=int(row["duration_ms"]),
        )
        for row in rows
    ]
