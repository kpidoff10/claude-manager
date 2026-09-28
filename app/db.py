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
    ("tasks", "remind_at", "TEXT"),
    ("tasks", "remind_note", "TEXT"),
    ("tasks", "reminded_at", "TEXT"),
    ("runs", "lesson_state", "TEXT"),
    ("runs", "lesson_note", "TEXT"),
    # Fiche support : le SEUL contexte projet donné à l'IA qui parle aux
    # signaleurs. Jamais la mémoire, le journal ni le briefing.
    ("projects", "support_context", "TEXT"),
    ("support_tickets", "duplicate_of", "INTEGER"),
    # Session `claude` de la conversation : elle dure autant que la discussion
    # (--session-id au premier message, --resume ensuite).
    ("support_tickets", "ai_session", "TEXT"),
    # Code lu par l'IA support : une copie en lecture seule, tenue par le démon
    # à partir de ce dépôt (clé de déploiement sans droit d'écriture).
    ("projects", "support_git", "TEXT"),
    ("projects", "support_branch", "TEXT"),
    # Session préparée dès l'ouverture de la discussion, pendant que le
    # signaleur lit l'accueil : pending → done | failed.
    ("support_tickets", "warm_state", "TEXT"),
    # Ce que fait l'IA pendant que le signaleur attend (« consulte le code… »).
    ("support_tickets", "ai_progress", "TEXT"),
]


def _apply_migrations(conn) -> None:
    for table, column, definition in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _reprend_rappels(conn) -> None:
    """Les premiers rappels vivaient dans des colonnes de `tasks`. On les
    recopie dans `reminders` puis on vide les colonnes : relancé, ce code ne
    trouve plus rien à reprendre."""
    lignes = conn.execute("SELECT id, project_id, remind_at, remind_note, reminded_at"
                          " FROM tasks WHERE remind_at IS NOT NULL").fetchall()
    for r in lignes:
        conn.execute("INSERT INTO reminders (project_id, task_id, note, remind_at, reminded_at,"
                     " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (r["project_id"], r["id"], r["remind_note"], r["remind_at"],
                      r["reminded_at"], now(), now()))
        conn.execute("UPDATE tasks SET remind_at = NULL, remind_note = NULL,"
                     " reminded_at = NULL WHERE id = ?", (r["id"],))


def _reprend_projets_signaleurs(conn) -> None:
    """Un signaleur n'avait qu'un projet : il le garde dans la table de liaison.
    Idempotent — relancé à chaque démarrage, il ne trouve plus rien à faire."""
    conn.execute("INSERT OR IGNORE INTO reporter_projects (reporter_id, project_id)"
                 " SELECT id, project_id FROM reporters")


def init_db() -> None:
    global _initialised
    schema = (config.BASE_DIR / "schema.sql").read_text(encoding="utf-8")
    with connect() as conn:
        conn.executescript(schema)
        _apply_migrations(conn)
        _reprend_rappels(conn)
        _reprend_projets_signaleurs(conn)
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
