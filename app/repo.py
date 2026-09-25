"""Couche d'accès aux données : toutes les requêtes passent par ici.

Chaque fonction publique prend une connexion optionnelle en argument nommé
``conn``. Sans elle, la fonction ouvre sa propre transaction ; avec elle, elle
s'inscrit dans une transaction existante — ce qui permet au briefing de tout
lire d'un seul coup sans rouvrir la base dix fois.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from . import config, db
from .db import now


def _with_conn(fn):
    @wraps(fn)
    def wrapper(*args, conn=None, **kwargs):
        if conn is not None:
            return fn(conn, *args, **kwargs)
        with db.cursor() as c:
            return fn(c, *args, **kwargs)

    return wrapper


class NotFound(Exception):
    pass


# --------------------------------------------------------------------------
# Index de recherche
# --------------------------------------------------------------------------

def _index(conn, entity_type: str, entity_id: int, project_id, title: str, body: str) -> None:
    conn.execute(
        "DELETE FROM search_index WHERE entity_type = ? AND entity_id = ?",
        (entity_type, entity_id),
    )
    conn.execute(
        "INSERT INTO search_index (title, body, entity_type, entity_id, project_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (title or "", body or "", entity_type, entity_id, project_id),
    )


def _unindex(conn, entity_type: str, entity_id: int) -> None:
    conn.execute(
        "DELETE FROM search_index WHERE entity_type = ? AND entity_id = ?",
        (entity_type, entity_id),
    )


# --------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------

def _tags_out(value: str) -> list:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def normalise_tag(value: str) -> str:
    """`#UI`, `UI `, `mise en page` deviennent `ui`, `ui`, `mise-en-page`.

    Sans cette normalisation, `ui` et `UI` seraient deux tags distincts et le
    vocabulaire se disperserait en quelques jours. Les lettres accentuées sont
    conservées : « réfacto » reste lisible.
    """
    text = str(value).strip().lstrip("#").lower()
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "-" for c in text)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")


def _tags_in(value) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    seen, tags = set(), []
    for item in value:
        tag = normalise_tag(item)
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return json.dumps(tags, ensure_ascii=False)


def _priority_in(value) -> int:
    """Accepte 3, \"3\" ou \"high\"."""
    if value is None:
        return 2
    if isinstance(value, int):
        return max(0, min(4, value))
    text = str(value).strip().lower()
    if text.isdigit():
        return max(0, min(4, int(text)))
    if text in config.PRIORITY_BY_NAME:
        return config.PRIORITY_BY_NAME[text]
    raise ValueError(f"priorité inconnue : {value!r}")


def task_out(row) -> dict:
    d = dict(row)
    d["tags"] = _tags_out(d.get("tags"))
    d["priority_label"] = config.PRIORITIES.get(d.get("priority", 2), "normal")
    return d


def project_out(row) -> dict:
    d = dict(row)
    d["tags"] = _tags_out(d.get("tags"))
    return d


def memory_out(row) -> dict:
    d = dict(row)
    d["tags"] = _tags_out(d.get("tags"))
    return d


# --------------------------------------------------------------------------
# Projets
# --------------------------------------------------------------------------

@_with_conn
def list_projects(conn, status: str | None = None, tag: str | None = None) -> list[dict]:
    sql = "SELECT * FROM projects"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, name"
    projects = [project_out(r) for r in conn.execute(sql, params)]
    if tag:
        wanted = normalise_tag(tag)
        projects = [p for p in projects if wanted in p["tags"]]
    running = running_run(conn=conn)
    for p in projects:
        p.update(project_stats(p["id"], conn=conn))
        p["agent_running"] = bool(running and running["project_id"] == p["id"])
        p["active_recently"] = _is_recent(p.get("last_activity"))
    return projects


def _is_recent(stamp: str | None, minutes: int = 15) -> bool:
    """Quelqu'un a-t-il écrit sur ce projet il y a peu ?

    C'est le seul signal disponible pour « une session travaille ici » : une
    session interactive ne s'annonce pas, mais elle consigne. Approximatif par
    nature, donc discret à l'écran.
    """
    if not stamp:
        return False
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() < minutes * 60


@_with_conn
def get_project(conn, ref) -> dict | None:
    """Retrouve un projet par slug, par identifiant, ou par chemin exact."""
    if ref is None:
        return None
    row = conn.execute("SELECT * FROM projects WHERE slug = ?", (str(ref),)).fetchone()
    if row is None and str(ref).isdigit():
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (int(ref),)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM projects WHERE path = ?", (str(ref),)).fetchone()
    return project_out(row) if row else None


@_with_conn
def require_project(conn, ref) -> dict:
    project = get_project(ref, conn=conn)
    if project is None:
        known = [r["slug"] for r in conn.execute("SELECT slug FROM projects ORDER BY slug")]
        raise NotFound(f"projet introuvable : {ref!r}. Projets connus : {', '.join(known) or 'aucun'}")
    return project


@_with_conn
def resolve_project_by_path(conn, path: str) -> dict | None:
    """Résout un répertoire de travail en projet.

    On retient le chemin enregistré le plus long qui préfixe ``path``, pour que
    /home/dev/projects/mare/server tombe sur « mare » et non sur un projet
    parent enregistré plus haut.
    """
    if not path:
        return None
    path = path.rstrip("/")
    best = None
    for row in conn.execute("SELECT * FROM projects WHERE path IS NOT NULL"):
        p = row["path"].rstrip("/")
        if path == p or path.startswith(p + "/"):
            if best is None or len(p) > len(best["path"].rstrip("/")):
                best = row
    return project_out(best) if best else None


@_with_conn
def upsert_project(conn, slug: str, name: str | None = None, path: str | None = None,
                   repo_url: str | None = None, description: str | None = None,
                   status: str | None = None, tags=None) -> dict:
    """`tags` remplace la liste entière ; None la laisse telle quelle."""
    existing = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    ts = now()
    if existing is None:
        conn.execute(
            "INSERT INTO projects (slug, name, path, repo_url, description, status, tags,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, name or slug, path, repo_url, description, status or "active",
             _tags_in(tags), ts, ts),
        )
    else:
        fields, params = [], []
        for column, value in (
            ("name", name), ("path", path), ("repo_url", repo_url),
            ("description", description), ("status", status),
            ("tags", _tags_in(tags) if tags is not None else None),
        ):
            if value is not None:
                fields.append(f"{column} = ?")
                params.append(value)
        if fields:
            fields.append("updated_at = ?")
            params.extend([ts, slug])
            conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE slug = ?", params)
    project = project_out(conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone())
    _index(conn, "project", project["id"], project["id"], project["name"],
           " ".join([project.get("description") or ""] + [f"#{t}" for t in project["tags"]]))
    return project


@_with_conn
def delete_project(conn, ref) -> None:
    project = require_project(ref, conn=conn)
    conn.execute("DELETE FROM projects WHERE id = ?", (project["id"],))
    conn.execute("DELETE FROM search_index WHERE project_id = ?", (project["id"],))


@_with_conn
def project_stats(conn, project_id: int) -> dict:
    row = conn.execute(
        """SELECT
             COUNT(*)                                                    AS tasks_total,
             SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END)            AS tasks_done,
             SUM(CASE WHEN status IN ('todo','in_progress','blocked') THEN 1 ELSE 0 END) AS tasks_open,
             SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END)     AS tasks_in_progress,
             SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END)         AS tasks_blocked,
             SUM(CASE WHEN status IN ('todo','in_progress','blocked') AND priority >= 3 THEN 1 ELSE 0 END) AS tasks_hot,
             SUM(CASE WHEN status IN ('review','needs_input','blocked') THEN 1 ELSE 0 END) AS tasks_awaiting
           FROM tasks WHERE project_id = ?""",
        (project_id,),
    ).fetchone()
    stats = {k: (row[k] or 0) for k in row.keys()}
    countable = stats["tasks_total"] - _count(
        conn, "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status = 'cancelled'", (project_id,))
    stats["progress"] = round(100 * stats["tasks_done"] / countable) if countable else 0
    stats["last_activity"] = _scalar(
        conn, "SELECT MAX(created_at) FROM journal WHERE project_id = ?", (project_id,))
    return stats


# Ce qui, en changeant, doit réafficher la page d'un projet : chaque table
# rattachée au projet, avec sa colonne d'horodatage la plus parlante.
_VERSION_SOURCES = [
    ("tasks", "updated_at"), ("journal", "created_at"), ("memories", "updated_at"),
    ("milestones", "updated_at"), ("project_technologies", "updated_at"),
    ("commands", "updated_at"), ("services", "updated_at"), ("env_vars", "updated_at"),
    ("resources", "updated_at"), ("docs", "updated_at"), ("practices", "updated_at"),
    ("runs", "COALESCE(finished_at, started_at)"),
]


@_with_conn
def project_version(conn, project_id: int) -> str:
    """Empreinte de l'état d'un projet, pour le rafraîchissement en direct.

    Le navigateur l'interroge toutes les quelques secondes et ne recharge le
    panneau que si elle change. Nombre de lignes compris : une suppression ne
    change aucun horodatage.
    """
    parts = [_scalar(conn, "SELECT updated_at FROM projects WHERE id = ?", (project_id,)) or ""]
    for table, column in _VERSION_SOURCES:
        try:
            row = conn.execute(f"SELECT COUNT(*), MAX({column}) FROM {table} WHERE project_id = ?",
                               (project_id,)).fetchone()
        except sqlite3.OperationalError:
            continue
        parts.append(f"{row[0]}:{row[1] or ''}")
    parts.append(str(_count(conn, "SELECT COUNT(*) FROM runs WHERE project_id = ? AND status = 'running'",
                            (project_id,))))
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


@_with_conn
def project_activity(conn, project_id: int) -> dict:
    """Qui travaille sur ce projet, maintenant : agents de la file, tâches en
    cours (une session interactive passe sa tâche en in_progress), et dernière
    trace consignée."""
    runs = [r for r in running_runs(conn=conn) if r["project_id"] == project_id]
    in_run = {r["task_id"] for r in runs}
    working = [task_out(r) for r in conn.execute(
        "SELECT * FROM tasks WHERE project_id = ? AND status = 'in_progress'"
        " ORDER BY updated_at DESC LIMIT 5", (project_id,)) if r["id"] not in in_run]
    last = conn.execute("SELECT * FROM journal WHERE project_id = ? ORDER BY created_at DESC, id DESC"
                        " LIMIT 1", (project_id,)).fetchone()
    last_task = conn.execute("SELECT id, title, status, updated_at FROM tasks WHERE project_id = ?"
                             " ORDER BY updated_at DESC LIMIT 1", (project_id,)).fetchone()
    return {"runs": runs, "working": working,
            "last_journal": dict(last) if last else None,
            "last_task": dict(last_task) if last_task else None}


def _count(conn, sql: str, params=()) -> int:
    return conn.execute(sql, params).fetchone()[0] or 0


def _scalar(conn, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------
# Jalons
# --------------------------------------------------------------------------

@_with_conn
def list_milestones(conn, project_id: int, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM milestones WHERE project_id = ?"
    params: list = [project_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY order_index, id"
    milestones = [dict(r) for r in conn.execute(sql, params)]
    for m in milestones:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done
               FROM tasks WHERE milestone_id = ? AND status != 'cancelled'""",
            (m["id"],),
        ).fetchone()
        total, done = row["total"] or 0, row["done"] or 0
        m["tasks_total"], m["tasks_done"] = total, done
        m["progress"] = round(100 * done / total) if total else 0
    return milestones


@_with_conn
def create_milestone(conn, project_id: int, name: str, description: str | None = None,
                     target_date: str | None = None) -> dict:
    ts = now()
    order_index = (_scalar(conn, "SELECT MAX(order_index) FROM milestones WHERE project_id = ?",
                           (project_id,)) or 0) + 1
    cur = conn.execute(
        "INSERT INTO milestones (project_id, name, description, target_date, order_index, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, name, description, target_date, order_index, ts, ts),
    )
    _index(conn, "milestone", cur.lastrowid, project_id, name, description or "")
    return dict(conn.execute("SELECT * FROM milestones WHERE id = ?", (cur.lastrowid,)).fetchone())


@_with_conn
def update_milestone(conn, milestone_id: int, **fields) -> dict:
    allowed = {"name", "description", "status", "target_date", "order_index"}
    sets, params = [], []
    for key, value in fields.items():
        if key in allowed and value is not None:
            sets.append(f"{key} = ?")
            params.append(value)
    if sets:
        sets.append("updated_at = ?")
        params.extend([now(), milestone_id])
        conn.execute(f"UPDATE milestones SET {', '.join(sets)} WHERE id = ?", params)
    row = conn.execute("SELECT * FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
    if row is None:
        raise NotFound(f"jalon introuvable : {milestone_id}")
    _index(conn, "milestone", milestone_id, row["project_id"], row["name"], row["description"] or "")
    return dict(row)


@_with_conn
def delete_milestone(conn, milestone_id: int) -> None:
    conn.execute("DELETE FROM milestones WHERE id = ?", (milestone_id,))
    _unindex(conn, "milestone", milestone_id)


# --------------------------------------------------------------------------
# Tâches
# --------------------------------------------------------------------------

@_with_conn
def list_tasks(conn, project_id: int | None = None, status: str | list | None = None,
               min_priority: int | None = None, tag: str | None = None,
               milestone_id: int | None = None, owner: str | None = None,
               parent_id: int | None = None, include_done: bool = False,
               limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM tasks WHERE 1 = 1"
    params: list = []
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    if status:
        statuses = [status] if isinstance(status, str) else list(status)
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    elif not include_done:
        sql += f" AND status IN ({','.join('?' * len(config.OPEN_TASK_STATUSES))})"
        params.extend(config.OPEN_TASK_STATUSES)
    if min_priority is not None:
        sql += " AND priority >= ?"
        params.append(min_priority)
    if milestone_id is not None:
        sql += " AND milestone_id = ?"
        params.append(milestone_id)
    if owner:
        sql += " AND owner = ?"
        params.append(owner)
    if parent_id is not None:
        sql += " AND parent_id = ?"
        params.append(parent_id)
    sql += (" ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'blocked' THEN 1"
            " WHEN 'todo' THEN 2 ELSE 3 END, priority DESC, order_index, id LIMIT ?")
    params.append(limit)
    tasks = [task_out(r) for r in conn.execute(sql, params)]
    if tag:
        tasks = [t for t in tasks if tag in t["tags"]]
    return tasks


@_with_conn
def list_tags(conn, project_id: int | None = None) -> list[dict]:
    """Le vocabulaire de tags réellement en usage, avec ses compteurs.

    C'est ce qui permet de réutiliser `ui` plutôt que d'inventer `interface`.
    """
    sql = "SELECT tags, status FROM tasks WHERE status != 'cancelled'"
    params: list = []
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    tally: dict[str, dict] = {}
    for row in conn.execute(sql, params):
        for tag in _tags_out(row["tags"]):
            entry = tally.setdefault(tag, {"tag": tag, "total": 0, "open": 0})
            entry["total"] += 1
            if row["status"] in config.OPEN_TASK_STATUSES:
                entry["open"] += 1
    return sorted(tally.values(), key=lambda e: (-e["open"], -e["total"], e["tag"]))


@_with_conn
def tasks_by_tag(conn, tag: str, project_id: int | None = None,
                 include_done: bool = False) -> list[dict]:
    wanted = normalise_tag(tag)
    tasks = list_tasks(project_id, include_done=include_done, limit=500, conn=conn)
    return [t for t in tasks if wanted in t["tags"]]


@_with_conn
def retag(conn, tag: str, project_id: int | None = None, status: str | None = None,
          priority=None, add_tag: str | None = None, remove_tag: str | None = None,
          apply: bool = False) -> dict:
    """Agit sur toutes les tâches portant un tag.

    Par défaut on ne fait que montrer : une modification de masse mérite d'être
    vue avant d'être subie.
    """
    targets = tasks_by_tag(tag, project_id, include_done=True, conn=conn)
    plan = {"tag": normalise_tag(tag), "matched": len(targets), "applied": apply,
            "tasks": [{"id": t["id"], "title": t["title"], "status": t["status"],
                       "priority": t["priority_label"]} for t in targets]}
    if not apply or not targets:
        return plan

    for task in targets:
        fields: dict = {}
        if status is not None:
            fields["status"] = status
        if priority is not None:
            fields["priority"] = priority
        if add_tag or remove_tag:
            tags = list(task["tags"])
            if add_tag:
                new = normalise_tag(add_tag)
                if new and new not in tags:
                    tags.append(new)
            if remove_tag:
                gone = normalise_tag(remove_tag)
                tags = [t for t in tags if t != gone]
            fields["tags"] = tags
        if fields:
            update_task(task["id"], conn=conn, **fields)
    return plan


@_with_conn
def get_task(conn, task_id: int) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(f"tâche introuvable : {task_id}")
    task = task_out(row)
    task["subtasks"] = [task_out(r) for r in conn.execute(
        "SELECT * FROM tasks WHERE parent_id = ? ORDER BY order_index, id", (task_id,))]
    return task


@_with_conn
def create_task(conn, project_id: int, title: str, body: str | None = None,
                priority=2, status: str = "todo", owner: str = "claude",
                tags=None, parent_id: int | None = None,
                milestone_id: int | None = None, allow_dirty: bool = False) -> dict:
    ts = now()
    order_index = (_scalar(conn, "SELECT MAX(order_index) FROM tasks WHERE project_id = ?",
                           (project_id,)) or 0) + 1
    cur = conn.execute(
        """INSERT INTO tasks (project_id, milestone_id, parent_id, title, body, status,
                              priority, owner, tags, order_index, allow_dirty,
                              created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, milestone_id, parent_id, title, body, status, _priority_in(priority),
         owner, _tags_in(tags), order_index, int(allow_dirty), ts, ts),
    )
    _index(conn, "task", cur.lastrowid, project_id, title, body or "")
    return task_out(conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone())


@_with_conn
def update_task(conn, task_id: int, **fields) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(f"tâche introuvable : {task_id}")

    sets, params = [], []

    def put(column, value):
        sets.append(f"{column} = ?")
        params.append(value)

    for key in ("title", "body", "owner", "blocked_reason", "milestone_id",
                "parent_id", "order_index"):
        if fields.get(key) is not None:
            put(key, fields[key])
    if fields.get("priority") is not None:
        put("priority", _priority_in(fields["priority"]))
    if fields.get("tags") is not None:
        put("tags", _tags_in(fields["tags"]))
    reason = (fields.get("cancel_reason") or "").strip() or None
    cancelling = False
    if fields.get("status") is not None:
        status = fields["status"]
        if status not in config.TASK_STATUSES:
            raise ValueError(f"statut inconnu : {status}")
        put("status", status)
        # Le passage à « done » horodate ; en repartir efface l'horodatage.
        put("completed_at", now() if status == "done" else None)
        cancelling = status == "cancelled" and row["status"] != "cancelled"
        # Rouverte, une tâche n'a plus de raison d'abandon.
        if status != "cancelled":
            put("cancel_reason", None)
    if reason and (fields.get("status") or row["status"]) == "cancelled":
        put("cancel_reason", reason)

    if sets:
        sets.append("updated_at = ?")
        params.extend([now(), task_id])
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)

    # L'abandon laisse une trace au journal : c'est là qu'on relit l'histoire
    # d'un projet, et une tâche annulée sans explication n'y apparaîtrait pas.
    if cancelling:
        titre = row["title"] if len(row["title"]) <= 80 else row["title"][:79] + "…"
        log_work(row["project_id"],
                 summary=f"Annulée #{task_id} {titre}" + (f" — {reason}" if reason else ""),
                 kind="note", task_id=task_id,
                 actor=fields.get("actor") or "claude", conn=conn)

    updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    _index(conn, "task", task_id, updated["project_id"], updated["title"], updated["body"] or "")
    return task_out(updated)


@_with_conn
def delete_task(conn, task_id: int) -> None:
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    _unindex(conn, "task", task_id)


@_with_conn
def reorder_task(conn, task_id: int, after_id: int | None) -> dict:
    """Place une tâche juste après une autre (ou en tête si after_id est nul)."""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(f"tâche introuvable : {task_id}")
    if after_id is None:
        lowest = _scalar(conn, "SELECT MIN(order_index) FROM tasks WHERE project_id = ?",
                         (row["project_id"],)) or 0
        new_index = lowest - 1
    else:
        anchor = conn.execute("SELECT order_index FROM tasks WHERE id = ?", (after_id,)).fetchone()
        if anchor is None:
            raise NotFound(f"tâche introuvable : {after_id}")
        following = _scalar(
            conn,
            "SELECT MIN(order_index) FROM tasks WHERE project_id = ? AND order_index > ?",
            (row["project_id"], anchor["order_index"]),
        )
        new_index = (anchor["order_index"] + following) / 2 if following is not None \
            else anchor["order_index"] + 1
    conn.execute("UPDATE tasks SET order_index = ?, updated_at = ? WHERE id = ?",
                 (new_index, now(), task_id))
    return task_out(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


# --------------------------------------------------------------------------
# Technologies
# --------------------------------------------------------------------------

@_with_conn
def list_technologies(conn, project_id: int | None = None,
                      status: str | None = None) -> list[dict]:
    if project_id is None:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM technologies ORDER BY category, name")]
    sql = """SELECT pt.*, t.name, t.category, t.docs_url, t.preference, t.preference_level
             FROM project_technologies pt JOIN technologies t ON t.id = pt.technology_id
             WHERE pt.project_id = ?"""
    params: list = [project_id]
    if status:
        sql += " AND pt.status = ?"
        params.append(status)
    sql += (" ORDER BY CASE pt.status WHEN 'active' THEN 0 WHEN 'considered' THEN 1 ELSE 2 END,"
            " t.category, t.name")
    return [dict(r) for r in conn.execute(sql, params)]


@_with_conn
def get_or_create_technology(conn, name: str, category: str | None = None,
                             docs_url: str | None = None) -> dict:
    row = conn.execute("SELECT * FROM technologies WHERE name = ? COLLATE NOCASE",
                       (name,)).fetchone()
    ts = now()
    if row is None:
        cur = conn.execute(
            "INSERT INTO technologies (name, category, docs_url, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, category or "lib", docs_url, ts, ts),
        )
        row = conn.execute("SELECT * FROM technologies WHERE id = ?", (cur.lastrowid,)).fetchone()
    else:
        sets, params = [], []
        # Une catégorie explicite prime sur le « lib » par défaut posé au scan.
        if category and row["category"] == "lib":
            sets.append("category = ?")
            params.append(category)
        if docs_url and not row["docs_url"]:
            sets.append("docs_url = ?")
            params.append(docs_url)
        if sets:
            sets.append("updated_at = ?")
            params.extend([ts, row["id"]])
            conn.execute(f"UPDATE technologies SET {', '.join(sets)} WHERE id = ?", params)
            row = conn.execute("SELECT * FROM technologies WHERE id = ?", (row["id"],)).fetchone()
    return dict(row)


@_with_conn
def set_technology(conn, project_id: int, name: str, category: str | None = None,
                   version: str | None = None, role: str | None = None,
                   status: str | None = None, notes: str | None = None,
                   docs_url: str | None = None, source: str = "manual") -> dict:
    tech = get_or_create_technology(name, category, docs_url, conn=conn)
    ts = now()
    existing = conn.execute(
        "SELECT * FROM project_technologies WHERE project_id = ? AND technology_id = ?",
        (project_id, tech["id"]),
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO project_technologies
               (project_id, technology_id, version, role, status, notes, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, tech["id"], version, role, status or "active", notes, source, ts, ts),
        )
    else:
        sets, params = [], []
        for column, value in (("version", version), ("role", role),
                              ("status", status), ("notes", notes)):
            # Un scan ne doit pas écraser ce que l'humain a écrit à la main.
            if value is None:
                continue
            if source == "scan" and existing["source"] == "manual" and column != "version":
                continue
            sets.append(f"{column} = ?")
            params.append(value)
        if sets:
            sets.append("updated_at = ?")
            params.extend([ts, existing["id"]])
            conn.execute(f"UPDATE project_technologies SET {', '.join(sets)} WHERE id = ?", params)
    _index(conn, "technology", tech["id"], project_id, tech["name"],
           " ".join(filter(None, [role, notes, tech.get("preference")])))
    return dict(conn.execute(
        """SELECT pt.*, t.name, t.category, t.docs_url, t.preference, t.preference_level
           FROM project_technologies pt JOIN technologies t ON t.id = pt.technology_id
           WHERE pt.project_id = ? AND pt.technology_id = ?""",
        (project_id, tech["id"]),
    ).fetchone())


@_with_conn
def remove_technology(conn, project_id: int, name: str) -> None:
    row = conn.execute("SELECT id FROM technologies WHERE name = ? COLLATE NOCASE",
                       (name,)).fetchone()
    if row is None:
        raise NotFound(f"technologie inconnue : {name}")
    conn.execute("DELETE FROM project_technologies WHERE project_id = ? AND technology_id = ?",
                 (project_id, row["id"]))


@_with_conn
def set_preference(conn, name: str, preference: str, level: str = "preferred",
                   category: str | None = None, docs_url: str | None = None) -> dict:
    tech = get_or_create_technology(name, category, docs_url, conn=conn)
    conn.execute(
        "UPDATE technologies SET preference = ?, preference_level = ?, updated_at = ? WHERE id = ?",
        (preference, level, now(), tech["id"]),
    )
    _index(conn, "preference", tech["id"], None, tech["name"], preference)
    return dict(conn.execute("SELECT * FROM technologies WHERE id = ?", (tech["id"],)).fetchone())


@_with_conn
def list_preferences(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM technologies WHERE preference IS NOT NULL AND preference != ''"
        " ORDER BY CASE preference_level WHEN 'preferred' THEN 0 WHEN 'neutral' THEN 1 ELSE 2 END,"
        " category, name")]


@_with_conn
def projects_using(conn, name: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT p.slug, p.name, pt.version, pt.status, pt.role
           FROM project_technologies pt
           JOIN technologies t ON t.id = pt.technology_id
           JOIN projects p ON p.id = pt.project_id
           WHERE t.name = ? COLLATE NOCASE ORDER BY p.name""",
        (name,))]


# --------------------------------------------------------------------------
# Fiche projet : commandes, services, variables d'env, ressources
# --------------------------------------------------------------------------

def _generic_upsert(conn, table: str, project_id: int, key_columns: dict,
                    values: dict, index_type: str, title: str, body: str) -> dict:
    ts = now()
    where = " AND ".join(f"{c} = ?" for c in key_columns)
    existing = conn.execute(
        f"SELECT * FROM {table} WHERE project_id = ? AND {where}",
        [project_id, *key_columns.values()],
    ).fetchone()
    payload = {k: v for k, v in values.items() if v is not None}
    if existing is None:
        columns = ["project_id", *key_columns.keys(), *payload.keys(), "created_at", "updated_at"]
        params = [project_id, *key_columns.values(), *payload.values(), ts, ts]
        cur = conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)})"
            f" VALUES ({', '.join('?' * len(columns))})",
            params,
        )
        row_id = cur.lastrowid
    else:
        row_id = existing["id"]
        if payload:
            sets = ", ".join(f"{c} = ?" for c in payload)
            conn.execute(f"UPDATE {table} SET {sets}, updated_at = ? WHERE id = ?",
                         [*payload.values(), ts, row_id])
    _index(conn, index_type, row_id, project_id, title, body)
    return dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())


@_with_conn
def set_command(conn, project_id: int, name: str, command: str | None = None,
                workdir: str | None = None, description: str | None = None) -> dict:
    return _generic_upsert(
        conn, "commands", project_id, {"name": name},
        {"command": command, "workdir": workdir, "description": description},
        "command", f"{name} : {command or ''}", description or "",
    )


@_with_conn
def request_doc_edit(conn, project_id: int, path: str, content: str,
                     branch: str | None = None) -> dict:
    """Dépose une modification de documentation, que le démon appliquera.

    Le conteneur ne peut pas écrire dans les dépôts — le montage est en lecture
    seule, et c'est une propriété qu'on garde. La demande transite donc par ici.
    Une demande déjà en attente sur le même fichier est remplacée : c'est la
    dernière saisie qui compte, pas la file des brouillons.
    """
    conn.execute("DELETE FROM doc_edits WHERE project_id = ? AND path = ?"
                 " AND state = 'pending'", (project_id, path))
    ts = now()
    cur = conn.execute(
        "INSERT INTO doc_edits (project_id, path, content, branch, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)", (project_id, path, content, branch, ts, ts))
    return dict(conn.execute("SELECT * FROM doc_edits WHERE id = ?",
                             (cur.lastrowid,)).fetchone())


@_with_conn
def pending_doc_edits(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT e.*, p.path AS project_path, p.slug AS project_slug
           FROM doc_edits e JOIN projects p ON p.id = e.project_id
           WHERE e.state = 'pending' ORDER BY e.id""")]


@_with_conn
def set_doc_edit_result(conn, edit_id: int, state: str, detail: str | None = None,
                        commit_hash: str | None = None) -> dict:
    conn.execute("UPDATE doc_edits SET state = ?, detail = ?, commit_hash = ?,"
                 " updated_at = ? WHERE id = ?",
                 (state, detail, commit_hash, now(), edit_id))
    row = conn.execute("SELECT * FROM doc_edits WHERE id = ?", (edit_id,)).fetchone()
    return dict(row) if row else {}


@_with_conn
def doc_edit_state(conn, project_id: int, path: str) -> dict | None:
    """Dernière demande connue sur ce fichier, pour l'afficher à l'écran."""
    row = conn.execute(
        "SELECT * FROM doc_edits WHERE project_id = ? AND path = ?"
        " ORDER BY id DESC LIMIT 1", (project_id, path)).fetchone()
    return dict(row) if row else None


@_with_conn
def set_doc(conn, project_id: int, path: str, title: str | None = None,
            covers: str | None = None) -> dict:
    """Un fichier de documentation du projet, et ce qu'on y trouve.

    On garde un **pointeur**, jamais le contenu : la documentation doit changer
    dans le même commit que le code qu'elle décrit. Ce que le manager apporte,
    c'est de savoir quel fichier ouvrir pour quelle question — pas une seconde
    copie qui divergera.
    """
    return _generic_upsert(
        conn, "docs", project_id, {"path": path},
        {"title": title, "covers": covers},
        "doc", title or path, covers or "",
    )


@_with_conn
def list_docs(conn, project_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM docs WHERE project_id = ? ORDER BY order_index, path",
        (project_id,))]


@_with_conn
def delete_doc(conn, doc_id: int) -> None:
    conn.execute("DELETE FROM search_index WHERE entity_type = 'doc' AND entity_id = ?",
                 (doc_id,))
    conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))


# Répertoires qu'on ne parcourt jamais : ils contiennent des milliers de
# markdown qui ne sont pas la documentation du projet.
IGNORES_DOCS = {"node_modules", ".git", "dist", "build", "vendor", "target",
                ".venv", "venv", "__pycache__", ".next", "coverage"}


@_with_conn
def scan_docs(conn, project_id: int, root: str, profondeur: int = 3) -> list[dict]:
    """Repère les fichiers markdown du dépôt et inscrit ceux qui manquent.

    N'écrase jamais un `covers` déjà écrit : la détection trouve les fichiers,
    c'est l'humain ou l'agent qui dit ce qu'ils couvrent. Un fichier disparu
    n'est pas retiré non plus — c'est peut-être une branche en cours.
    """
    base = Path(root)
    if not base.is_dir():
        return []
    connus = {d["path"] for d in list_docs(project_id, conn=conn)}
    trouves = []
    for chemin in sorted(base.rglob("*.md")):
        relatif = chemin.relative_to(base)
        if set(relatif.parts) & IGNORES_DOCS or len(relatif.parts) > profondeur:
            continue
        texte = str(relatif)
        trouves.append(texte)
        if texte not in connus:
            set_doc(project_id, path=texte, title=chemin.stem.replace("-", " ").title(),
                    conn=conn)
    return list_docs(project_id, conn=conn)


@_with_conn
def set_practice(conn, project_id: int, title: str, body: str | None = None,
                 category: str | None = None) -> dict:
    """Une pratique de travail du projet. Le titre l'identifie et la remplace.

    Ce qui décrit le **code** n'a rien à faire ici : une convention doit changer
    dans le même commit que ce qu'elle décrit, donc elle vit dans le CLAUDE.md du
    dépôt. Ici vit ce que le dépôt ne dit pas — la définition de « terminé », le
    périmètre qu'on ne franchit pas sans décision, le rituel de revue.
    """
    return _generic_upsert(
        conn, "practices", project_id, {"title": title},
        {"body": body, "category": (category or "general").strip().lower()},
        "practice", title, body or "",
    )


@_with_conn
def list_practices(conn, project_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM practices WHERE project_id = ?"
        " ORDER BY order_index, category, id", (project_id,))]


@_with_conn
def delete_practice(conn, practice_id: int) -> None:
    conn.execute("DELETE FROM search_index WHERE entity_type = 'practice'"
                 " AND entity_id = ?", (practice_id,))
    conn.execute("DELETE FROM practices WHERE id = ?", (practice_id,))


@_with_conn
def list_commands(conn, project_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM commands WHERE project_id = ? ORDER BY order_index, name", (project_id,))]


@_with_conn
def set_service(conn, project_id: int, name: str, kind: str | None = None,
                url: str | None = None, port: int | None = None,
                container: str | None = None, environment: str = "prod",
                notes: str | None = None) -> dict:
    return _generic_upsert(
        conn, "services", project_id, {"name": name, "environment": environment},
        {"kind": kind, "url": url, "port": port, "container": container, "notes": notes},
        "service", f"{name} {url or ''}", notes or "",
    )


@_with_conn
def list_services(conn, project_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM services WHERE project_id = ? ORDER BY environment, name", (project_id,))]


@_with_conn
def set_env_var(conn, project_id: int, name: str, required: bool | None = None,
                secret: bool | None = None, location: str | None = None,
                description: str | None = None, example: str | None = None) -> dict:
    return _generic_upsert(
        conn, "env_vars", project_id, {"name": name},
        {"required": None if required is None else int(required),
         "secret": None if secret is None else int(secret),
         "location": location, "description": description, "example": example},
        "env_var", name, description or "",
    )


@_with_conn
def list_env_vars(conn, project_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM env_vars WHERE project_id = ? ORDER BY required DESC, name", (project_id,))]


@_with_conn
def add_resource(conn, project_id: int | None, title: str, url: str,
                 kind: str = "other", notes: str | None = None) -> dict:
    ts = now()
    existing = conn.execute(
        "SELECT * FROM resources WHERE url = ? AND project_id IS ?", (url, project_id)).fetchone()
    if existing is not None:
        conn.execute("UPDATE resources SET title = ?, kind = ?, notes = ?, updated_at = ? WHERE id = ?",
                     (title, kind, notes, ts, existing["id"]))
        row_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO resources (project_id, title, url, kind, notes, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, title, url, kind, notes, ts, ts),
        )
        row_id = cur.lastrowid
    _index(conn, "resource", row_id, project_id, title, f"{url} {notes or ''}")
    return dict(conn.execute("SELECT * FROM resources WHERE id = ?", (row_id,)).fetchone())


@_with_conn
def list_resources(conn, project_id: int | None = None,
                   include_global: bool = True) -> list[dict]:
    if project_id is None:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM resources WHERE project_id IS NULL ORDER BY kind, title")]
    sql = "SELECT * FROM resources WHERE project_id = ?"
    if include_global:
        sql += " OR project_id IS NULL"
    sql += " ORDER BY project_id IS NULL, kind, title"
    return [dict(r) for r in conn.execute(sql, (project_id,))]


PROFILE_TABLES = {"command": "commands", "service": "services",
                  "env_var": "env_vars", "resource": "resources"}


@_with_conn
def delete_profile_item(conn, kind: str, item_id: int) -> None:
    table = PROFILE_TABLES.get(kind)
    if table is None:
        raise ValueError(f"type inconnu : {kind}. Attendu : {', '.join(PROFILE_TABLES)}")
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
    _unindex(conn, kind, item_id)


# --------------------------------------------------------------------------
# Mémoire
# --------------------------------------------------------------------------

@_with_conn
def add_memory(conn, project_id: int | None, title: str, body: str,
               kind: str = "note", tags=None, pinned: bool = False) -> dict:
    ts = now()
    cur = conn.execute(
        "INSERT INTO memories (project_id, kind, title, body, tags, pinned, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, kind, title, body, _tags_in(tags), int(pinned), ts, ts),
    )
    _index(conn, "memory", cur.lastrowid, project_id, title, body)
    return memory_out(conn.execute("SELECT * FROM memories WHERE id = ?",
                                   (cur.lastrowid,)).fetchone())


@_with_conn
def update_memory(conn, memory_id: int, **fields) -> dict:
    sets, params = [], []
    for key in ("title", "body", "kind"):
        if fields.get(key) is not None:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    if fields.get("tags") is not None:
        sets.append("tags = ?")
        params.append(_tags_in(fields["tags"]))
    if fields.get("pinned") is not None:
        sets.append("pinned = ?")
        params.append(int(fields["pinned"]))
    if sets:
        sets.append("updated_at = ?")
        params.extend([now(), memory_id])
        conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", params)
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise NotFound(f"mémoire introuvable : {memory_id}")
    _index(conn, "memory", memory_id, row["project_id"], row["title"], row["body"])
    return memory_out(row)


@_with_conn
def list_memories(conn, project_id: int | None = None, kind: str | None = None,
                  include_global: bool = True, limit: int = 100) -> list[dict]:
    if project_id is None:
        sql = "SELECT * FROM memories WHERE project_id IS NULL"
        params: list = []
    elif include_global:
        sql = "SELECT * FROM memories WHERE (project_id = ? OR project_id IS NULL)"
        params = [project_id]
    else:
        sql = "SELECT * FROM memories WHERE project_id = ?"
        params = [project_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY pinned DESC, updated_at DESC LIMIT ?"
    params.append(limit)
    return [memory_out(r) for r in conn.execute(sql, params)]


@_with_conn
def delete_memory(conn, memory_id: int) -> None:
    conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    _unindex(conn, "memory", memory_id)


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------

@_with_conn
def log_work(conn, project_id: int, summary: str, detail: str | None = None,
             kind: str = "work", task_id: int | None = None,
             session_id: str | None = None, actor: str = "claude") -> dict:
    cur = conn.execute(
        "INSERT INTO journal (project_id, task_id, kind, summary, detail, session_id, actor, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, task_id, kind, summary, detail, session_id, actor, now()),
    )
    _index(conn, "journal", cur.lastrowid, project_id, summary, detail or "")
    return dict(conn.execute("SELECT * FROM journal WHERE id = ?", (cur.lastrowid,)).fetchone())


@_with_conn
def list_journal(conn, project_id: int | None = None, limit: int = 30,
                 kind: str | None = None) -> list[dict]:
    sql = ("SELECT j.*, p.slug AS project_slug, t.title AS task_title"
           " FROM journal j JOIN projects p ON p.id = j.project_id"
           " LEFT JOIN tasks t ON t.id = j.task_id WHERE 1 = 1")
    params: list = []
    if project_id is not None:
        sql += " AND j.project_id = ?"
        params.append(project_id)
    if kind:
        sql += " AND j.kind = ?"
        params.append(kind)
    sql += " ORDER BY j.created_at DESC, j.id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------------------
# Recherche
# --------------------------------------------------------------------------

def _fts_query(raw: str) -> str:
    """Neutralise la syntaxe FTS5 pour qu'une saisie libre ne casse pas la requête."""
    terms = [t for t in "".join(c if c.isalnum() or c in "-_" else " " for c in raw).split() if t]
    if not terms:
        return ""
    return " AND ".join(f'"{t}"*' for t in terms)


@_with_conn
def search(conn, query: str, project_id: int | None = None,
           entity_types: list | None = None, limit: int = 30) -> list[dict]:
    match = _fts_query(query)
    if not match:
        return []
    sql = ("SELECT title, body, entity_type, entity_id, project_id, rank"
           " FROM search_index WHERE search_index MATCH ?")
    params: list = [match]
    if project_id is not None:
        sql += " AND (project_id = ? OR project_id IS NULL)"
        params.append(project_id)
    if entity_types:
        sql += f" AND entity_type IN ({','.join('?' * len(entity_types))})"
        params.extend(entity_types)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    slugs = {r["id"]: r["slug"] for r in conn.execute("SELECT id, slug FROM projects")}
    results = []
    for r in rows:
        d = dict(r)
        d.pop("rank", None)
        d["project_slug"] = slugs.get(d["project_id"])
        d["excerpt"] = (d.pop("body") or "")[:300]
        results.append(d)
    return results


# --------------------------------------------------------------------------
# File d'agents : réglages, candidats, exécutions
# --------------------------------------------------------------------------

@_with_conn
def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


@_with_conn
def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), now()),
    )


@_with_conn
def queue_pending(conn, limit: int = 20) -> list[dict]:
    """Tâches en file, dans l'ordre où elles doivent être traitées.

    Les tâches confiées à l'humain et celles sans énoncé n'y figurent pas : une
    tâche sans corps ne se délègue pas, un agent qui démarre à froid n'aurait
    que le titre pour deviner.
    """
    rows = conn.execute(
        """SELECT t.*, p.slug AS project_slug, p.path AS project_path, p.name AS project_name
           FROM tasks t JOIN projects p ON p.id = t.project_id
           WHERE t.status = 'queued' AND t.owner = 'claude' AND p.status = 'active'
             AND p.path IS NOT NULL AND t.body IS NOT NULL AND TRIM(t.body) != ''
           ORDER BY t.priority DESC, t.order_index, t.id
           LIMIT ?""",
        (limit,),
    )
    pending = []
    for row in rows:
        task = task_out(row)
        task["attempt"] = _count(
            conn, "SELECT COUNT(*) FROM runs WHERE task_id = ?", (task["id"],)) + 1
        pending.append(task)
    return pending


@_with_conn
def running_runs(conn) -> list[dict]:
    """Toutes les exécutions en cours."""
    return [dict(r) for r in conn.execute(
        """SELECT r.*, t.title, p.slug AS project_slug, p.path AS project_path
           FROM runs r JOIN tasks t ON t.id = r.task_id JOIN projects p ON p.id = r.project_id
           WHERE r.status = 'running' ORDER BY r.id""")]


@_with_conn
def running_run(conn) -> dict | None:
    """La plus récente des exécutions en cours, pour les affichages qui n'en
    montrent qu'une."""
    runs = running_runs(conn=conn)
    return runs[-1] if runs else None


@_with_conn
def tasks_awaiting_answer(conn) -> list[dict]:
    """Tâches arrêtées sur une question, la plus récente d'abord.

    Sert à rattacher une réponse venue de Telegram quand elle ne dit pas à quoi
    elle répond : une seule question ouverte, aucune ambiguïté ; plusieurs, on
    demande plutôt que de deviner.
    """
    return [dict(r) for r in conn.execute(
        """SELECT t.id, t.title, t.blocked_reason, p.slug AS project_slug,
                  p.name AS project_name
           FROM tasks t JOIN projects p ON p.id = t.project_id
           WHERE t.status = 'needs_input' ORDER BY t.updated_at DESC""")]


@_with_conn
def orphan_tasks(conn, minutes: int = 45) -> list[dict]:
    """Tâches « en cours » que plus aucune exécution ne porte.

    Vu sur mare #142 : marquée `in_progress` par une session interactive, jamais
    refermée. La file ne la réclame pas — elle ne prend que `queued` — et le
    tableau la montre en cours. Elle avait disparu des radars pendant des heures.

    **Une tâche `in_progress` sans exécution n'est pas une anomalie en soi** :
    c'est l'état normal d'un travail fait à la main, en ce moment même. D'où le
    délai : on ne s'inquiète qu'au-delà d'une inactivité franche. Et l'on se
    contente de signaler — remettre en file automatiquement lancerait un agent
    là où quelqu'un travaille peut-être encore, ce que toute la file s'échine
    justement à éviter.
    """
    return [dict(r) for r in conn.execute(
        """SELECT t.*, p.slug AS project_slug, p.name AS project_name,
                  CAST((julianday('now') - julianday(t.updated_at)) * 1440 AS INTEGER)
                    AS idle_minutes
           FROM tasks t JOIN projects p ON p.id = t.project_id
           WHERE t.status = 'in_progress'
             AND NOT EXISTS (SELECT 1 FROM runs r
                             WHERE r.task_id = t.id AND r.status = 'running')
             AND julianday('now') - julianday(t.updated_at) > ? / 1440.0
           ORDER BY t.updated_at""", (minutes,))]


@_with_conn
def claim_task(conn, task_id: int, commit_before: str | None = None,
               log_path: str | None = None, group_id: str | None = None,
               allow_parallel: bool = False, branch: str | None = None,
               worktree: str | None = None) -> dict | None:
    """Prend une tâche de la file et ouvre son exécution.

    Deux verrous. Le premier est le nombre total d'agents. Le second protège un
    même projet de deux agents qui partageraient une copie de travail — c'était
    la règle du temps où un lot entier tenait dans un seul worktree.

    **Une exécution qui a son propre worktree y échappe** : elle n'a aucun
    fichier en commun avec les autres, et le désaccord éventuel se réglera à la
    fusion. Sans cette exception, le passage à un worktree par tâche n'aurait
    servi à rien — `allow_parallel` valant toujours faux pour une tâche seule,
    la deuxième réclamation était refusée en boucle.
    """
    active = running_runs(conn=conn)
    if len(active) >= config.MAX_PARALLEL:
        return None
    row = conn.execute("SELECT * FROM tasks WHERE id = ? AND status = 'queued'",
                       (task_id,)).fetchone()
    if row is None:
        return None
    same_project = [r for r in active if r["project_id"] == row["project_id"]]
    if same_project and not allow_parallel and not worktree:
        return None
    attempt = _count(conn, "SELECT COUNT(*) FROM runs WHERE task_id = ?", (task_id,)) + 1
    cur = conn.execute(
        """INSERT INTO runs (task_id, project_id, status, commit_before, log_path,
                             attempt, group_id, started_at)
           VALUES (?, ?, 'running', ?, ?, ?, ?, ?)""",
        (task_id, row["project_id"], commit_before, log_path, attempt, group_id, now()),
    )
    conn.execute("UPDATE tasks SET status = 'in_progress', updated_at = ? WHERE id = ?",
                 (now(), task_id))
    if not log_path:
        conn.execute("UPDATE runs SET log_path = ? WHERE id = ?",
                     (f"{config.RUN_LOG_DIR}/run-{cur.lastrowid}.log", cur.lastrowid))
    if branch or worktree:
        conn.execute("UPDATE runs SET branch = ?, worktree = ? WHERE id = ?",
                     (branch, worktree, cur.lastrowid))
    repo_run = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (cur.lastrowid,)).fetchone())
    log_work(row["project_id"], summary=f"Agent lancé sur la tâche #{task_id} : {row['title']}",
             kind="work", task_id=task_id, actor="agent", conn=conn)
    return repo_run


@_with_conn
def finish_run(conn, run_id: int, status: str, task_status: str,
               **fields) -> dict:
    """Clôt une exécution et pose l'état de la tâche."""
    allowed = {"commit_after", "diff_stat", "tests_command", "tests_ok",
               "tests_output", "summary", "exit_code", "log_path", "foreign_files",
               "collision", "branch", "base_branch", "worktree", "preview_url",
               "preview_port"}
    sets = ["status = ?", "finished_at = ?"]
    params: list = [status, now()]
    for key, value in fields.items():
        if key in allowed and value is not None:
            sets.append(f"{key} = ?")
            params.append(int(value) if key == "tests_ok" else value)
    params.append(run_id)
    conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", params)
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise NotFound(f"exécution introuvable : {run_id}")
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
    # L'agent a pu poser lui-même « needs_input » : sa demande prime sur le
    # verdict mécanique, c'est la seule chose qu'il est seul à savoir.
    if task["status"] != "needs_input":
        conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                     (task_status, now(), row["task_id"]))

    # Une tâche qui atterrit dans « Pour moi » doit dire pourquoi. Sans ce
    # report, la raison reste sur l'exécution et la carte est muette — c'est
    # arrivé pour de bon après une interruption du démon.
    if task_status == "needs_input" and not (task["blocked_reason"] or "").strip():
        raison = (row["summary"] or "").strip() or \
            "L'agent s'est arrêté sans explication. Voir son journal."
        conn.execute("UPDATE tasks SET blocked_reason = ?, updated_at = ? WHERE id = ?",
                     (raison, now(), row["task_id"]))
    return dict(row)


@_with_conn
def request_merge(conn, run_id: int) -> None:
    """Demande la fusion d'une branche d'agent.

    Le conteneur monte les dépôts en lecture seule : il ne peut pas fusionner
    lui-même. On enregistre l'intention, le démon l'exécute côté hôte.
    """
    conn.execute("UPDATE runs SET merge_state = 'requested' WHERE id = ?", (run_id,))


@_with_conn
def live_worktrees(conn) -> list[dict]:
    """Worktrees encore sur le disque, avec l'état de leur tâche.

    Le démon s'en sert pour faire le ménage : un worktree dont la tâche est
    close, et dont la fusion n'est plus en attente, n'a plus de raison d'être.
    """
    return [dict(r) for r in conn.execute(
        """SELECT r.id, r.task_id, r.worktree, r.branch, r.base_branch, r.merge_state,
                  r.preview_port, t.status AS task_status, p.path AS project_path,
                  p.slug AS project_slug
           FROM runs r JOIN tasks t ON t.id = r.task_id JOIN projects p ON p.id = r.project_id
           WHERE r.worktree IS NOT NULL AND r.worktree != ''""")]


@_with_conn
def forget_worktree(conn, run_id: int) -> None:
    conn.execute("UPDATE runs SET worktree = NULL, preview_url = NULL,"
                 " preview_port = NULL WHERE id = ?", (run_id,))


@_with_conn
def used_ports(conn) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT preview_port FROM runs WHERE preview_port IS NOT NULL")]


@_with_conn
def pending_merges(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT r.*, t.title, p.slug AS project_slug, p.path AS project_path
           FROM runs r JOIN tasks t ON t.id = r.task_id JOIN projects p ON p.id = r.project_id
           WHERE r.merge_state = 'requested' ORDER BY r.id""")]


@_with_conn
def set_merge_result(conn, run_id: int, state: str, detail: str | None = None) -> dict:
    conn.execute("UPDATE runs SET merge_state = ?, merge_detail = ? WHERE id = ?",
                 (state, detail, run_id))
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    # `reverted` et `broken` : la fusion a réussi mais la base ne tenait plus.
    # Comme un conflit, ça réclame un humain — et surtout pas une tâche close.
    RAISONS = {
        "conflict": "Fusion impossible sans arbitrage : ",
        "reverted": "Fusion défaite : les tests étaient rouges sur la base. ",
        "broken": "Tests rouges sur la base après fusion, non défaite : ",
    }
    if state in RAISONS and row is not None:
        conn.execute(
            "UPDATE tasks SET status = 'needs_input', blocked_reason = ?, updated_at = ?"
            " WHERE id = ?",
            (RAISONS[state] + (detail or "")[:400], now(), row["task_id"]))
    return dict(row) if row else {}


@_with_conn
def latest_runs(conn, task_ids: list) -> dict:
    if not task_ids:
        return {}
    marks = ",".join("?" * len(task_ids))
    rows = conn.execute(
        f"""SELECT r.* FROM runs r
            JOIN (SELECT task_id, MAX(id) AS last FROM runs
                  WHERE task_id IN ({marks}) GROUP BY task_id) m
              ON m.last = r.id""",
        task_ids,
    )
    return {r["task_id"]: dict(r) for r in rows}


@_with_conn
def list_runs(conn, task_id: int, limit: int = 10) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit))]


@_with_conn
def awaiting_user(conn, project_id: int | None = None) -> list[dict]:
    sql = ("SELECT t.*, p.slug AS project_slug, p.name AS project_name"
           " FROM tasks t JOIN projects p ON p.id = t.project_id"
           f" WHERE t.status IN ({','.join('?' * len(config.AWAITING_USER_STATUSES))})")
    params: list = list(config.AWAITING_USER_STATUSES)
    if project_id is not None:
        sql += " AND t.project_id = ?"
        params.append(project_id)
    sql += " ORDER BY CASE t.status WHEN 'review' THEN 0 WHEN 'needs_input' THEN 1 ELSE 2 END," \
           " t.priority DESC, t.id"
    return [task_out(r) for r in conn.execute(sql, params)]


@_with_conn
def ask_user(conn, task_id: int, question: str, options=None,
             recommendation: str | None = None) -> dict:
    """L'agent rend la main sur une question, en proposant des réponses.

    Une question nue oblige l'humain à tout formuler ; des options le laissent
    trancher d'un clic. Le champ libre reste ouvert à côté — les options sont
    une commodité, jamais une contrainte.
    """
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(f"tâche introuvable : {task_id}")
    if isinstance(options, str):
        options = [o.strip() for o in options.split("\n") if o.strip()]
    payload = json.dumps({"options": [str(o) for o in (options or [])],
                          "recommendation": recommendation or ""}, ensure_ascii=False)
    conn.execute(
        "UPDATE tasks SET status = 'needs_input', blocked_reason = ?, question = ?,"
        " updated_at = ? WHERE id = ?",
        (question, payload, now(), task_id))
    log_work(row["project_id"], kind="blocker", actor="agent", task_id=task_id,
             summary=f"Question sur la tâche #{task_id} : {_first_line(question)}",
             detail=question, conn=conn)
    return task_out(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def question_out(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"options": [], "recommendation": ""}
    return {"options": parsed.get("options") or [],
            "recommendation": parsed.get("recommendation") or ""}


@_with_conn
def answer_question(conn, task_id: int, answer: str) -> dict:
    """La réponse devient une consigne et la tâche repart en file.

    Contrairement à un refus, une réponse ne compte pas comme un essai raté :
    l'agent n'avait pas échoué, il attendait. Elle repart donc toujours.
    """
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(f"tâche introuvable : {task_id}")
    body = (row["body"] or "").rstrip()
    body += (f"\n\n---\n**Réponse de Kevin ({now()[:16].replace('T', ' ')})** "
             f"à la question « {_first_line(row['blocked_reason'] or '')} » :\n{answer}")
    conn.execute(
        "UPDATE tasks SET body = ?, status = 'queued', blocked_reason = NULL,"
        " question = NULL, updated_at = ? WHERE id = ?",
        (body, now(), task_id))
    _index(conn, "task", task_id, row["project_id"], row["title"], body)
    log_work(row["project_id"], kind="decision", actor="user", task_id=task_id,
             summary=f"Réponse sur la tâche #{task_id} : {_first_line(answer)}",
             detail=answer, conn=conn)
    return task_out(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


@_with_conn
def append_feedback(conn, task_id: int, comment: str) -> dict:
    """Un refus n'est pas une note : le commentaire s'ajoute à l'énoncé et la
    tâche repart en file, pour que l'agent suivant le lise comme une consigne."""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(f"tâche introuvable : {task_id}")
    attempts = _count(conn, "SELECT COUNT(*) FROM runs WHERE task_id = ?", (task_id,))
    body = (row["body"] or "").rstrip()
    body += f"\n\n---\n**Retour de Kevin ({now()[:16].replace('T', ' ')})** — à traiter en priorité :\n{comment}"
    # Au-delà du nombre d'essais permis, la tâche revient à l'humain : un agent
    # qui a déjà buté plusieurs fois butera encore. On dit alors pourquoi, sinon
    # la carte atterrit dans « Pour moi » sans que rien ne l'explique.
    if attempts < config.MAX_RETRIES:
        status, reason = "queued", None
    else:
        status = "needs_input"
        reason = (f"Plafond atteint : {attempts} passages d'agent sans satisfaire la "
                  f"demande. À reprendre à la main, ou à réécrire plus précisément "
                  f"avant de la remettre en file.")
    conn.execute(
        "UPDATE tasks SET body = ?, status = ?, blocked_reason = ?, updated_at = ? WHERE id = ?",
        (body, status, reason, now(), task_id))
    _index(conn, "task", task_id, row["project_id"], row["title"], body)
    log_work(row["project_id"], kind="note", actor="user", task_id=task_id,
             summary=f"Retour sur la tâche #{task_id} : {_first_line(comment)}",
             detail=comment, conn=conn)
    return task_out(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def _first_line(text: str, length: int = 90) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= length else line[: length - 1] + "…"


@_with_conn
def activity_since(conn, project_id: int, since: str) -> dict:
    """Ce qui a été écrit sur un projet depuis un instant donné.

    Sert au hook Stop : du travail sans aucune trace ici signale un oubli de
    consignation. On compte les écritures, pas les lectures.
    """
    counts = {
        "journal": _count(conn,
                          "SELECT COUNT(*) FROM journal WHERE project_id = ? AND created_at >= ?",
                          (project_id, since)),
        "tasks": _count(conn,
                        "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND updated_at >= ?",
                        (project_id, since)),
        "memories": _count(conn,
                           "SELECT COUNT(*) FROM memories WHERE project_id = ? AND updated_at >= ?",
                           (project_id, since)),
        "technologies": _count(
            conn,
            "SELECT COUNT(*) FROM project_technologies WHERE project_id = ? AND updated_at >= ?",
            (project_id, since)),
    }
    counts["total"] = sum(counts.values())
    return counts


@_with_conn
def reindex_all(conn) -> int:
    """Reconstruit l'index de recherche. Utile après un import ou une migration."""
    conn.execute("DELETE FROM search_index")
    count = 0
    for r in conn.execute("SELECT id, name, description FROM projects"):
        _index(conn, "project", r["id"], r["id"], r["name"], r["description"] or "")
        count += 1
    for r in conn.execute("SELECT id, project_id, title, body FROM tasks"):
        _index(conn, "task", r["id"], r["project_id"], r["title"], r["body"] or "")
        count += 1
    for r in conn.execute("SELECT id, project_id, title, body FROM memories"):
        _index(conn, "memory", r["id"], r["project_id"], r["title"], r["body"])
        count += 1
    for r in conn.execute("SELECT id, project_id, summary, detail FROM journal"):
        _index(conn, "journal", r["id"], r["project_id"], r["summary"], r["detail"] or "")
        count += 1
    for r in conn.execute("SELECT id, project_id, name, description FROM milestones"):
        _index(conn, "milestone", r["id"], r["project_id"], r["name"], r["description"] or "")
        count += 1
    for r in conn.execute("SELECT id, project_id, title, url, notes FROM resources"):
        _index(conn, "resource", r["id"], r["project_id"], r["title"],
               f"{r['url']} {r['notes'] or ''}")
        count += 1
    return count
