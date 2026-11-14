"""Audit trail for runs, plus the fingerprints used to spot repeated transactions.

Deliberately small: we store *what* was applied (hashes and amounts), never a copy of the
workbook or the screenshot. If DATABASE_URL is unset — local dev without compose, say —
every method degrades to a no-op so the app still works statelessly.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import asyncmy

# Migrations are the normal path. This fallback is idempotent, so applying it to an
# already-migrated database is a no-op; it only matters if someone deploys without them.
SCHEMA_FALLBACK: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS tx_events (
        id         BIGINT       NOT NULL AUTO_INCREMENT,
        created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        actor      VARCHAR(320) NOT NULL DEFAULT 'anonymous',
        action     VARCHAR(32)  NOT NULL,
        sheet      VARCHAR(128) NULL,
        cell       VARCHAR(16)  NULL,
        count      INT          NOT NULL DEFAULT 0,
        detail     VARCHAR(512) NULL,
        PRIMARY KEY (id)
    ) DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS tx_seen (
        fingerprint CHAR(64)      NOT NULL,
        tx_date     VARCHAR(32)   NULL,
        amount      DECIMAL(14,4) NOT NULL,
        seen_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (fingerprint)
    ) DEFAULT CHARSET=utf8mb4""",
)


def _dsn() -> dict:
    # mysql://user%40tenant:password@host:2881/dbname  (OceanBase speaks MySQL wire)
    url = urlparse(os.environ["DATABASE_URL"])
    return {
        "host": url.hostname,
        "port": url.port or 2881,
        "user": unquote(url.username or ""),
        "password": unquote(url.password or ""),
        "db": (url.path or "/").lstrip("/"),
    }


class Database:
    def __init__(self) -> None:
        self._pool: asyncmy.Pool | None = None
        self._schema_checked = False

    def configured(self) -> bool:
        return bool(os.getenv("DATABASE_URL"))

    async def connect(self) -> None:
        if self.configured() and self._pool is None:
            self._pool = await asyncmy.create_pool(**_dsn(), autocommit=True)

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def _run(self, sql: str, args: tuple = ()) -> list[tuple]:
        """Run one statement, recreating the tables once if they are missing."""
        pool = self._pool
        if pool is None:
            return []
        try:
            return await _execute(pool, sql, args)
        except asyncmy.errors.ProgrammingError as exc:
            if exc.args and exc.args[0] == 1146:  # table doesn't exist
                await self._ensure_schema()
                return await _execute(pool, sql, args)
            raise

    async def _ensure_schema(self) -> None:
        if self._schema_checked or self._pool is None:
            return
        self._schema_checked = True
        for statement in SCHEMA_FALLBACK:
            await _execute(self._pool, statement, ())

    # -- public API -------------------------------------------------------------

    async def log(
        self,
        actor: str | None,
        action: str,
        sheet: str | None,
        cell: str | None,
        count: int,
        detail: str | None,
    ) -> None:
        try:
            await self._run(
                "INSERT INTO tx_events (actor, action, sheet, cell, count, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    (actor or "anonymous")[:320],
                    action[:32],
                    _clip(sheet, 128),
                    _clip(cell, 16),
                    count,
                    _clip(detail, 512),
                ),
            )
        except Exception:  # noqa: BLE001 - auditing must never fail the user's request
            pass

    async def remember(self, fingerprint: str, tx_date: str | None, amount) -> None:
        try:
            await self._run(
                "INSERT INTO tx_seen (fingerprint, tx_date, amount) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE seen_at = CURRENT_TIMESTAMP",
                (fingerprint, _clip(tx_date, 32), str(amount)),
            )
        except Exception:  # noqa: BLE001
            pass

    async def fingerprints(self) -> set[str]:
        try:
            rows = await self._run(
                "SELECT fingerprint FROM tx_seen ORDER BY seen_at DESC LIMIT 2000"
            )
        except Exception:  # noqa: BLE001
            return set()
        return {row[0] for row in rows}

    async def recent(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        try:
            rows = await self._run(
                "SELECT created_at, actor, action, sheet, cell, count, detail "
                "FROM tx_events ORDER BY id DESC LIMIT %s",
                (limit,),
            )
        except Exception:  # noqa: BLE001
            return []
        return [
            {
                "created_at": _iso(row[0]),
                "actor": row[1],
                "action": row[2],
                "sheet": row[3],
                "cell": row[4],
                "count": int(row[5] or 0),
                "detail": row[6],
            }
            for row in rows
        ]


async def _execute(pool: asyncmy.Pool, sql: str, args: tuple) -> list[tuple]:
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(sql, args)
        if cur.description is None:
            return []
        return list(await cur.fetchall())


def _clip(value: str | None, length: int) -> str | None:
    return None if value is None else str(value)[:length]


def _iso(value) -> str:
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamp.isoformat()
    return str(value)