"""Thin SQLite helpers. Plain SQL on purpose - no ORM hiding the queries."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import ROOT, settings

SCHEMA_FILE = ROOT / "sql" / "schema.sql"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))


def rows(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with tx() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    found = rows(sql, params)
    return found[0] if found else None


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))
