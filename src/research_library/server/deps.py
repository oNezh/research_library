"""Dependency helpers for the FastAPI server.

SQLite connections are cheap to open and this app is single-user local, so we
open one connection per request (thread) instead of sharing across threads.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

from research_library.library import db as library_db


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = library_db.connect()
    library_db.ensure_schema(conn)
    try:
        yield conn
    finally:
        conn.close()
