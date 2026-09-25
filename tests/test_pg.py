"""Tests for the SQLite -> Postgres query translation (app/pg.py). No live
database needed - these check the rewritten SQL for every statement shape
app/db.py actually sends.
"""
import re

from app import db
from app.pg import translate


def test_placeholders_become_psycopg_style():
    sql, returning = translate("SELECT * FROM notes WHERE id = ?")
    assert sql == "SELECT * FROM notes WHERE id = %s"
    assert returning is False


def test_literal_percent_is_escaped():
    sql, _ = translate("SELECT * FROM notes WHERE raw_text LIKE '50%' AND id = ?")
    assert sql == "SELECT * FROM notes WHERE raw_text LIKE '50%%' AND id = %s"


def test_insert_into_id_table_returns_id():
    sql, returning = translate("INSERT INTO notes (source) VALUES (?)")
    assert sql.endswith("RETURNING id")
    assert returning is True


def test_insert_without_id_column_does_not_return_id():
    sql, returning = translate(
        "INSERT INTO pending_input (chat_id, draft_id, kind, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET draft_id = excluded.draft_id"
    )
    assert "RETURNING" not in sql
    assert returning is False


def test_insert_or_ignore_becomes_on_conflict_do_nothing():
    sql, returning = translate(
        "INSERT OR IGNORE INTO processed_updates (update_id, processed_at) VALUES (?, ?)"
    )
    assert sql == (
        "INSERT INTO processed_updates (update_id, processed_at) VALUES (%s, %s) ON CONFLICT DO NOTHING"
    )
    assert returning is False


def test_multiline_insert_with_leading_whitespace():
    sql, returning = translate(
        """
        INSERT INTO drafts (note_id, text)
        VALUES (?, ?)
        """
    )
    assert returning is True
    assert sql.rstrip().endswith("RETURNING id")


def test_every_autoincrement_table_gets_returning_id():
    """Guards against a new table being added to db.SCHEMA without updating
    app/pg.py's _TABLES_WITH_ID - lastrowid would silently be None.
    """
    tables = re.findall(
        r"CREATE TABLE IF NOT EXISTS (\w+) \(\s*id\s+INTEGER PRIMARY KEY AUTOINCREMENT", db.SCHEMA
    )
    assert tables  # sanity: the regex still matches the schema
    for table in tables:
        _, returning = translate(f"INSERT INTO {table} (x) VALUES (?)")
        assert returning, f"{table} inserts need RETURNING id - add it to _TABLES_WITH_ID in app/pg.py"


def test_postgres_schema_has_every_sqlite_table():
    from app.pg import SCHEMA_PATH

    sqlite_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db.SCHEMA))
    pg_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA_PATH.read_text(encoding="utf-8")))
    assert sqlite_tables == pg_tables


def test_is_postgres_detection():
    assert db.is_postgres("postgresql://u:p@host:5432/postgres")
    assert db.is_postgres("postgres://u:p@host:5432/postgres")
    assert not db.is_postgres("data/skinstinct.db")
