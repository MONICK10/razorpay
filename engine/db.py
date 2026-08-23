"""SQLite connection helpers for the finance controller engine."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path, fresh: bool = True) -> sqlite3.Connection:
    """Create (or recreate) the database at db_path from schema.sql."""
    path = Path(db_path)
    if fresh and path.exists():
        path.unlink()
    conn = connect(path)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn
