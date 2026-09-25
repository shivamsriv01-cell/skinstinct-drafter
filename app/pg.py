"""Postgres (Supabase) backend for app/db.py.

Exposes the small slice of the sqlite3.Connection API the rest of the app
actually uses - execute() returning a cursor with fetchone()/fetchall()/
lastrowid, executemany(), executescript(), commit(), close() - so every
module that takes a `conn` works unchanged on either backend. app/db.py
picks this backend when the database target is a postgres:// URL (see
SUPABASE_DB_URL in .env.example).

Behaviour notes:
  * Queries are written for SQLite ("?" placeholders, INSERT OR IGNORE);
    translate() rewrites them for Postgres, and appends RETURNING id to
    inserts so cursor.lastrowid keeps working.
  * The connection runs in autocommit mode. The bot holds one long-lived
    connection and makes slow Gemini calls between queries; with explicit
    transactions that would leave Postgres "idle in transaction" for the
    whole call, and one failed statement would poison every later query
    on the shared connection. The app already commits after every write,
    so commit() here is a no-op.
  * If Supabase drops the idle connection (pooler timeouts, network
    blips), the next query reconnects - up to 4 attempts over ~10s - and
    retries once.
  * Rows come back as dicts, so row["column"] works exactly like
    sqlite3.Row.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

import psycopg
from psycopg.rows import dict_row

SCHEMA_PATH = Path(__file__).resolve().parent / "schema_postgres.sql"

# Waits between reconnect attempts after a dropped connection (4 attempts).
RECONNECT_DELAYS_SECONDS = (1, 3, 6, 0)

# Tables with an identity `id` column - inserts into these get RETURNING id.
_TABLES_WITH_ID = {
    "notes", "note_groups", "batches", "scores", "picks",
    "news_items", "drafts", "decisions", "logs",
}

_INSERT_RE = re.compile(r"^\s*INSERT\s+(OR\s+IGNORE\s+)?INTO\s+(\w+)", re.IGNORECASE)


def translate(sql: str) -> tuple[str, bool]:
    """Rewrites one SQLite-dialect statement for Postgres. Returns the new
    SQL and whether it ends in RETURNING id (so the caller can read
    lastrowid).
    """
    # "%" is psycopg's placeholder escape, so literal ones must be doubled
    # before "?" becomes "%s".
    out = sql.replace("%", "%%").replace("?", "%s")

    match = _INSERT_RE.match(out)
    if not match:
        return out, False

    or_ignore, table = match.group(1), match.group(2).lower()
    if or_ignore:
        out = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", out, count=1, flags=re.IGNORECASE)
        out = out.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    if table in _TABLES_WITH_ID and "RETURNING" not in out.upper():
        out = out.rstrip().rstrip(";") + " RETURNING id"
        return out, True
    return out, False


class PgCursor:
    def __init__(self, cursor: psycopg.Cursor, returning_id: bool):
        self._cursor = cursor
        self.rowcount: int = cursor.rowcount
        self.lastrowid: Optional[int] = None
        self._rows: Optional[list[dict]] = None
        if returning_id:
            row = cursor.fetchone()
            self.lastrowid = row["id"] if row else None
            self._rows = []

    def fetchone(self) -> Optional[dict]:
        if self._rows is not None or self._cursor.description is None:
            return None
        return self._cursor.fetchone()

    def fetchall(self) -> list[dict]:
        if self._rows is not None or self._cursor.description is None:
            return []
        return self._cursor.fetchall()

    def __iter__(self) -> Iterator[dict]:
        return iter(self.fetchall())


class PgConnection:
    def __init__(self, url: str):
        self._url = url
        self._conn = self._open()

    def _open(self) -> psycopg.Connection:
        return psycopg.connect(
            self._url,
            autocommit=True,
            row_factory=dict_row,
            # Supabase's transaction-mode pooler (port 6543) doesn't support
            # server-side prepared statements; psycopg would otherwise start
            # preparing repeated queries after 5 runs.
            prepare_threshold=None,
            connect_timeout=15,
        )

    def _reopen_with_retries(self) -> None:
        # A dropped connection is often followed by a few seconds of network
        # trouble (e.g. DNS briefly failing), so space the attempts out.
        for attempt, delay in enumerate(RECONNECT_DELAYS_SECONDS, start=1):
            try:
                self._conn = self._open()
                return
            except psycopg.OperationalError:
                if attempt == len(RECONNECT_DELAYS_SECONDS):
                    raise
                time.sleep(delay)

    def _run(self, fn):
        if self._conn.closed or self._conn.broken:
            self._reopen_with_retries()
        try:
            return fn(self._conn.cursor())
        except psycopg.OperationalError:
            # Only retry when the connection itself died - a genuine SQL
            # error on a healthy connection should surface as-is.
            if not (self._conn.closed or self._conn.broken):
                raise
            self._reopen_with_retries()
            return fn(self._conn.cursor())

    def execute(self, sql: str, params: Sequence[Any] = ()) -> PgCursor:
        pg_sql, returning_id = translate(sql)

        def run(cur: psycopg.Cursor) -> PgCursor:
            cur.execute(pg_sql, tuple(params))
            return PgCursor(cur, returning_id)

        return self._run(run)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> None:
        pg_sql, _ = translate(sql)
        rows = [tuple(p) for p in seq_of_params]
        if not rows:
            return
        self._run(lambda cur: cur.executemany(pg_sql, rows))

    def executescript(self, script: str) -> None:
        """Runs a multi-statement script verbatim (no placeholder
        translation) - used only for the schema.
        """
        self._run(lambda cur: cur.execute(script))

    def commit(self) -> None:
        # Autocommit mode - see module docstring.
        pass

    def close(self) -> None:
        self._conn.close()


def init_schema(conn: PgConnection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
