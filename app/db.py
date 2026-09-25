"""Accès SQLite : connexion, schéma, utilitaires."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

_initialised = False


def now() -> str:
    """Horodatage ISO 8601 en UTC, tronqué à la seconde."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


# Colonnes ajoutées après coup. `CREATE TABLE IF NOT EXISTS` ne touche pas une
# table déjà créée : sans ce rattrapage, une base existante n'aurait jamais la
# colonne et l'application tomberait à la première requête.
MIGRATIONS = [
    ("tasks", "allow_dirty", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "foreign_files", "TEXT"),
    ("runs", "group_id", "TEXT"),
    ("runs", "collision", "TEXT"),
    ("tasks", "question", "TEXT"),
    ("runs", "branch", "TEXT"),
    ("runs", "base_branch", "TEXT"),
    ("runs", "merge_state", "TEXT"),
    ("runs", "merge_detail", "TEXT"),
    ("runs", "worktree", "TEXT"),
    ("runs", "preview_url", "TEXT"),
    ("runs", "preview_port", "INTEGER"),
    ("projects", "tags", "TEXT NOT NULL DEFAULT '[]'"),
    ("tasks", "cancel_reason", "TEXT"),
]


def _apply_migrations(conn) -> None:
    for table, column, definition in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    global _initialised
    schema = (config.BASE_DIR / "schema.sql").read_text(encoding="utf-8")
    with connect() as conn:
        conn.executescript(schema)
        _apply_migrations(conn)
        conn.commit()
    _initialised = True


@contextmanager
def cursor():
    """Transaction : commit si tout va bien, rollback sinon."""
    if not _initialised:
        init_db()
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]
