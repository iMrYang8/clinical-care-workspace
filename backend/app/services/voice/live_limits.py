from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import suppress

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from app.api.deps import RequestContext
from app.core.config import settings
from app.core.db import engine


class LiveConnectionLimitError(Exception):
    """A stable, non-reflective reason for refusing a live provider lease."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _advisory_key(scope: str, identifier: uuid.UUID, slot: int = 0) -> int:
    digest = hashlib.sha256(
        f"nightingale-live:{scope}:{identifier}:{slot}".encode()
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _try_lock(connection: Connection, key: int) -> bool:
    return bool(
        connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": key}
        ).scalar_one()
    )


def _first_slot(
    connection: Connection, scope: str, identifier: uuid.UUID, maximum: int
) -> int | None:
    for slot in range(maximum):
        key = _advisory_key(scope, identifier, slot)
        if _try_lock(connection, key):
            return key
    return None


def _close_locks(connection: Connection, keys: list[int]) -> None:
    try:
        for key in reversed(keys):
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": key}
            )
        connection.commit()
    except Exception:
        # Connection.close() normally returns a DBAPI session to the pool, so
        # it is not a sufficient fallback if explicit unlock failed. Invalidating
        # forces physical teardown and PostgreSQL releases all session locks.
        with suppress(Exception):
            connection.invalidate()
        raise
    finally:
        connection.close()


class LiveConnectionLease:
    def __init__(
        self,
        *,
        connection: Connection,
        keys: list[int],
        semaphore: asyncio.BoundedSemaphore,
    ) -> None:
        self._connection = connection
        self._keys = keys
        self._semaphore = semaphore
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            with suppress(Exception):
                _close_locks(self._connection, self._keys)
        finally:
            self._semaphore.release()


class LiveConnectionLimiter:
    """Cross-worker PostgreSQL leases plus a per-process global semaphore."""

    def __init__(
        self,
        *,
        db_engine: Engine,
        max_global: int,
        max_clinic: int,
        max_user: int,
        timeout_seconds: float,
    ) -> None:
        if min(max_global, max_clinic, max_user) < 1:
            raise ValueError("Live connection limits must be positive")
        self.db_engine = db_engine
        self.max_clinic = max_clinic
        self.max_user = max_user
        self.timeout_seconds = max(0.01, timeout_seconds)
        self._semaphore = asyncio.BoundedSemaphore(max_global)

    async def acquire(
        self, context: RequestContext, session_id: uuid.UUID
    ) -> LiveConnectionLease:
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            raise LiveConnectionLimitError("LIVE_TRANSCRIPT_GLOBAL_LIMIT") from exc

        connection: Connection | None = None
        keys: list[int] = []
        try:
            connection = self.db_engine.connect()
            clinic_key = _first_slot(
                connection,
                "clinic",
                context.clinic_id,
                self.max_clinic,
            )
            if clinic_key is None:
                raise LiveConnectionLimitError("LIVE_TRANSCRIPT_CLINIC_LIMIT")
            keys.append(clinic_key)

            user_key = _first_slot(
                connection,
                "user",
                context.user_id,
                self.max_user,
            )
            if user_key is None:
                raise LiveConnectionLimitError("LIVE_TRANSCRIPT_USER_LIMIT")
            keys.append(user_key)

            session_key = _advisory_key("session", session_id)
            if not _try_lock(connection, session_key):
                raise LiveConnectionLimitError("LIVE_TRANSCRIPT_SESSION_IN_USE")
            keys.append(session_key)
            connection.commit()
            return LiveConnectionLease(
                connection=connection,
                keys=keys,
                semaphore=self._semaphore,
            )
        except LiveConnectionLimitError:
            try:
                with suppress(Exception):
                    if connection is not None:
                        _close_locks(connection, keys)
            finally:
                self._semaphore.release()
            raise
        except Exception as exc:
            try:
                with suppress(Exception):
                    if connection is not None:
                        _close_locks(connection, keys)
            finally:
                self._semaphore.release()
            raise LiveConnectionLimitError("LIVE_TRANSCRIPT_LEASE_UNAVAILABLE") from exc


live_connection_limiter = LiveConnectionLimiter(
    db_engine=engine,
    max_global=settings.LIVE_TRANSCRIPT_MAX_GLOBAL_CONNECTIONS,
    max_clinic=settings.LIVE_TRANSCRIPT_MAX_CLINIC_CONNECTIONS,
    max_user=settings.LIVE_TRANSCRIPT_MAX_USER_CONNECTIONS,
    timeout_seconds=settings.LIVE_TRANSCRIPT_LEASE_TIMEOUT_SECONDS,
)
