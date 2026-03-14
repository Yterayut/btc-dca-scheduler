"""Database connection and transaction helpers."""

from __future__ import annotations

import logging
from contextlib import contextmanager

import MySQLdb
from tenacity import retry, stop_after_attempt, wait_exponential

from services.bootstrap import load_required_env_vars


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_db_connection():
    """Connect to MySQL database with retry mechanism."""
    required_env_vars = load_required_env_vars()
    try:
        db = MySQLdb.connect(
            host=required_env_vars["DB_HOST"],
            user=required_env_vars["DB_USER"],
            passwd=required_env_vars["DB_PASSWORD"],
            db=required_env_vars["DB_NAME"],
            charset="utf8",
        )
        cursor = db.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        return db
    except MySQLdb.OperationalError as exc:
        logging.error(f"Database connection error: {exc}")
        raise


@contextmanager
def db_transaction():
    """Context manager for DB cursor with automatic commit/rollback."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        yield cursor, conn
        conn.commit()
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if conn:
                conn.close()
        except Exception:
            pass
