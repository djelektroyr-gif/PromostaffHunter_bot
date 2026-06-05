"""SQLite (локально) или PostgreSQL (прод через DATABASE_URL)."""
import os
import sqlite3
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial

from config import get_database_path

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_POSTGRES = bool(DATABASE_URL)
SQLITE_PATH = get_database_path() if not IS_POSTGRES else None

_db_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="db")


async def run_db(func, *args, **kwargs):
    """Синхронные запросы к PG/SQLite в пуле потоков — не блокируют aiogram."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_db_executor, partial(func, *args, **kwargs))

if IS_POSTGRES:
    import psycopg2
    from psycopg2 import IntegrityError
else:
    IntegrityError = sqlite3.IntegrityError


def db_info_label() -> str:
    if IS_POSTGRES:
        host = DATABASE_URL.split("@")[-1].split("/")[0] if "@" in DATABASE_URL else "postgres"
        return f"PostgreSQL ({host})"
    return f"SQLite ({SQLITE_PATH})"


def q(sql: str) -> str:
    return sql.replace("?", "%s") if IS_POSTGRES else sql


def connect():
    if IS_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(SQLITE_PATH, timeout=10.0)


@contextmanager
def db_conn(commit: bool = True):
    conn = connect()
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_exists(table_name: str) -> bool:
    with db_conn(commit=False) as conn:
        cur = conn.cursor()
        if IS_POSTGRES:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
                (table_name,),
            )
        else:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            )
        return cur.fetchone() is not None


def column_exists(table_name: str, column_name: str) -> bool:
    with db_conn(commit=False) as conn:
        return column_exists_cur(conn.cursor(), table_name, column_name)


def column_exists_cur(cur, table_name: str, column_name: str) -> bool:
    if IS_POSTGRES:
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s AND column_name=%s
            """,
            (table_name, column_name),
        )
        return cur.fetchone() is not None
    cur.execute(f"PRAGMA table_info({table_name})")
    cols = [c[1] for c in cur.fetchall()]
    return column_name in cols


def pg_column_data_type(cur, table_name: str, column_name: str) -> str | None:
    if not IS_POSTGRES:
        return None
    cur.execute(
        """
        SELECT data_type FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s AND column_name=%s
        """,
        (table_name, column_name),
    )
    row = cur.fetchone()
    return row[0] if row else None


def add_column_if_missing(table: str, column: str, ddl_sqlite: str, ddl_pg: str = None, cur=None):
    ddl = ddl_pg if IS_POSTGRES and ddl_pg else ddl_sqlite
    if cur is not None:
        if column_exists_cur(cur, table, column):
            return
        cur.execute(ddl)
        return
    if not table_exists(table) or column_exists(table, column):
        return
    with db_conn() as conn:
        conn.cursor().execute(ddl)


def paid_until_active() -> str:
    if IS_POSTGRES:
        return "(paid_until IS NULL OR paid_until > NOW())"
    return "(paid_until IS NULL OR datetime(paid_until) > datetime('now'))"


def paid_until_expired() -> str:
    if IS_POSTGRES:
        return "paid_until <= NOW()"
    return "datetime(paid_until) <= datetime('now')"


def now_plus_days(days: int) -> str:
    days = int(days)
    if IS_POSTGRES:
        return f"NOW() + INTERVAL '{days} days'"
    return f"datetime('now', '+{days} days')"


def now_minus_days(days: int) -> str:
    days = int(days)
    if IS_POSTGRES:
        return f"NOW() - INTERVAL '{days} days'"
    return f"datetime('now', '-{days} days')"


def vacancy_sort_published_sql() -> str:
    """published_at хранится как TEXT, found_at — TIMESTAMP; COALESCE только в одном типе."""
    if IS_POSTGRES:
        return "COALESCE(NULLIF(published_at, '')::timestamp, found_at)"
    return "COALESCE(published_at, datetime(found_at))"


def serial_pk() -> str:
    return "SERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


def bool_default_true() -> str:
    return "DEFAULT TRUE" if IS_POSTGRES else "DEFAULT 1"


def bool_default_false() -> str:
    return "DEFAULT FALSE" if IS_POSTGRES else "DEFAULT 0"


def bool_true() -> str:
    return "TRUE" if IS_POSTGRES else "1"


def bool_false() -> str:
    return "FALSE" if IS_POSTGRES else "0"


def execute(sql: str, params=()):
    with db_conn() as conn:
        conn.cursor().execute(q(sql), params)


def fetchone(sql: str, params=()):
    with db_conn(commit=False) as conn:
        cur = conn.cursor()
        cur.execute(q(sql), params)
        return cur.fetchone()


def fetchall(sql: str, params=()):
    with db_conn(commit=False) as conn:
        cur = conn.cursor()
        cur.execute(q(sql), params)
        return cur.fetchall()


def fetchval(sql: str, params=(), default=None):
    row = fetchone(sql, params)
    return row[0] if row else default
