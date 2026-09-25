"""Composition du briefing de démarrage de session.

C'est la pièce centrale : un seul appel doit suffire à savoir où en est un
projet. Le contenu est donc trié par utilité — ce qui est en cours d'abord,
le décor ensuite — et volontairement plafonné pour rester lisible.
"""
from . import config, db, repo

MAX_PLAIN_TECHS = 10
# Rôles posés d'office par le scanner : ils n'apprennent rien et ne justifient
# pas qu'une dépendance occupe sa propre place dans le briefing.
GENERIC_ROLES = {"développement", "runtime", "image de base"}
MAX_PINNED = 6
MAX_TAGS = 18
MAX_DECISIONS = 8
MEMORY_CHARS = 260
PRIORITY_MARK = {4: "🔴", 3: "🟠", 2: "·", 1: "·", 0: "·"}
STATUS_MARK = {"in_progress": "▶", "blocked": "⛔", "todo": "○",
               "done": "✓", "cancelled": "✗"}


def build(project_ref, max_tasks: int = 15, max_journal: int = 10,
          include_stack: bool = True) -> dict:
    with db.cursor() as conn:
        project = repo.require_project(project_ref, conn=conn)
        pid = project["id"]
        stats = repo.project_stats(pid, conn=conn)
        data = {
            "project": project,
            "stats": stats,
            "practices": repo.list_practices(pid, conn=conn),
            "docs": repo.list_docs(pid, conn=conn),
            "milestones": repo.list_milestones(pid, conn=conn),
            "in_progress": repo.list_tasks(pid, status="in_progress", conn=conn),
            "blocked": repo.list_tasks(pid, status="blocked", conn=conn),
            "next_tasks": repo.list_tasks(pid, status="todo", limit=max_tasks, conn=conn),
            "recent_done": repo.list_tasks(pid, status="done", limit=5, conn=conn),
            "memories": repo.list_memories(pid, limit=25, conn=conn),
            "journal": repo.list_journal(pid, limit=max_journal, conn=conn),
            "technologies": repo.list_technologies(pid, conn=conn) if include_stack else [],
            "commands": repo.list_commands(pid, conn=conn),
            "services": repo.list_services(pid, conn=conn),
            "env_vars": repo.list_env_vars(pid, conn=conn),
            "resources": repo.list_resources(pid, conn=conn),
            "preferences": repo.list_preferences(conn=conn),
            "tags": repo.list_tags(pid, conn=conn),
        }
    return data


def _task_line(task: dict) -> str:
    mark = STATUS_MARK.get(task["status"], "·")
    prio = PRIORITY_MARK.get(task["priority"], "·")
    bits = [f"- {mark} {prio} **#{task['id']}** {task['title']}"]
    extras = []
    if task["priority"] >= 3:
        extras.append(task["priority_label"])
    if task.get("parent_id"):
        extras.append(f"sous-tâche de #{task['parent_id']}")
    if task["owner"] != "claude":
        extras.append(f"pour {task['owner']}")
    if task["tags"]:
        extras.append(" ".join(f"#{t}" for t in task["tags"]))
    if task.get("status") == "cancelled" and task.get("cancel_reason"):
        extras.append(f"annulée : {task['cancel_reason']}")
    if task.get("blocked_reason"):
        extras.append(f"bloquée : {task['blocked_reason']}")
    if extras:
        bits.append(f" _({' · '.join(extras)})_")
    return "".join(bits)


def to_markdown(data: dict) -> str:
    p = data["project"]
    s = data["stats"]
    out: list[str] = []
    add = out.append

    add(f"# Projet : {p['name']}  `{p['slug']}`")
    if p.get("description"):
        add(p["description"])
    meta = [f"chemin `{p['path']}`" if p.get("path") else None,
            f"dépôt {p['repo_url']}" if p.get("repo_url") else None,
            f"statut **{p['status']}**",
            " ".join(f"#{t}" for t in p["tags"]) if p.get("tags") else None]
    add(" · ".join(m for m in meta if m))
    add("")
    add(f"**Avancement : {s['progress']}%** — {s['tasks_done']}/{s['tasks_total']} tâches · "
        f"{s['tasks_open']} ouvertes · {s['tasks_in_progress']} en cours · "
        f"{s['tasks_blocked']} bloquées")

    # **Avant tout le reste.** La méthode se lit avant d'agir, pas après : une
    # tâche traitée sans elle est une tâche à refaire. D'où sa place en tête,
    # au-dessus même des jalons.
    if data.get("practices"):
        add("")
        add("## Méthodologie — comment on travaille ici")
        par_categorie: dict[str, list[dict]] = {}
        for p in data["practices"]:
            par_categorie.setdefault(p.get("category") or "general", []).append(p)
        for categorie, pratiques in par_categorie.items():
            add(f"**{categorie}**")
            for p in pratiques:
                corps = " ".join((p.get("body") or "").split())
                add(f"- **{p['title']}** — {corps}" if corps else f"- **{p['title']}**")

    # Juste après la méthode : savoir quoi ouvrir évite de chercher, et de
    # réécrire ce qui existe déjà quelque part dans le dépôt.
    documentes = [d for d in data.get("docs") or [] if (d.get("covers") or "").strip()]
    if documentes:
        add("")
        add("## Documentation du dépôt — quoi ouvrir, pour quoi")
        for d in documentes:
            add(f"- `{d['path']}` — {' '.join(d['covers'].split())}")

    if data["milestones"]:
        add("")
        add("## Jalons")
        for m in data["milestones"]:
            if m["status"] == "cancelled":
                continue
            date = f" — cible {m['target_date']}" if m.get("target_date") else ""
            if m["status"] == "done":
                # Un pourcentage sur un jalon franchi n'apprend rien, et affiche
                # « 0 % » quand aucune tâche ne lui était rattachée.
                add(f"- ✓ **{m['name']}** — terminé{date}")
            elif m["tasks_total"]:
                # On nomme ce que le chiffre mesure : des tâches enregistrées, pas
                # le périmètre réel du jalon. Un jalon dont on n'a écrit que les
                # verrous afficherait sinon 100 % à peine commencé.
                add(f"- ◔ **{m['name']}** — {m['tasks_done']}/{m['tasks_total']} "
                    f"tâche(s) enregistrée(s), {m['progress']}%{date}")
            else:
                add(f"- ◔ **{m['name']}** — aucune tâche enregistrée{date}")

    if data["in_progress"]:
        add("")
        add("## En cours")
        out.extend(_task_line(t) for t in data["in_progress"])

    if data["blocked"]:
        add("")
        add("## Bloqué")
        out.extend(_task_line(t) for t in data["blocked"])

    if data["next_tasks"]:
        add("")
        add("## À faire, par priorité")
        out.extend(_task_line(t) for t in data["next_tasks"])

    # Le briefing est un index, pas une archive : sans plafond ici, il grossirait
    # indéfiniment avec l'âge du projet. Les corps sont tronqués et l'identifiant
    # affiché — `list_memories` donne le texte complet quand il est vraiment utile.
    if data.get("tags"):
        add("")
        add("**Tags en usage** — " + " · ".join(
            f"#{t['tag']} ({t['open']})" if t["open"] else f"#{t['tag']}"
            for t in data["tags"][:MAX_TAGS])
            + ". Réutiliser ces tags plutôt que d'en inventer de proches.")

    pinned = [m for m in data["memories"] if m["pinned"]][:MAX_PINNED]
    decisions = [m for m in data["memories"]
                 if not m["pinned"] and m["kind"] in ("decision", "convention", "gotcha")]
    shown, hidden = decisions[:MAX_DECISIONS], max(0, len(decisions) - MAX_DECISIONS)
    if pinned or shown:
        add("")
        add("## Mémoire")
        for m in pinned:
            add(f"- 📌 **{m['title']}** — {_truncate(m['body'], MEMORY_CHARS)} `#{m['id']}`")
        for m in shown:
            add(f"- _{m['kind']}_ **{m['title']}** — "
                f"{_truncate(m['body'], MEMORY_CHARS)} `#{m['id']}`")
        if hidden:
            add(f"- _…et {hidden} autre(s) mémoire(s) — `list_memories` pour les voir._")

    if data["technologies"]:
        add("")
        add("## Stack")
        by_status: dict[str, list] = {}
        for t in data["technologies"]:
            by_status.setdefault(t["status"], []).append(t)
        for status, label in (("active", "En usage"), ("considered", "Envisagé"),
                              ("deprecated", "Abandonné")):
            items = by_status.get(status) or []
            if not items:
                continue
            # Les entrées porteuses d'un rôle ou d'une note d'abandon sont celles
            # qui apprennent quelque chose ; le reste n'est qu'un inventaire de
            # dépendances, que les manifestes disent déjà. On le résume.
            notable = [t for t in items
                       if (t.get("role") and t["role"] not in GENERIC_ROLES) or t.get("notes")]
            plain = [t for t in items if t not in notable]
            line = " · ".join(_tech_label(t) for t in notable)
            if plain:
                shown = ", ".join(t["name"] for t in plain[:MAX_PLAIN_TECHS])
                rest = len(plain) - MAX_PLAIN_TECHS
                line += (" · " if line else "") + f"_aussi : {shown}"
                if rest > 0:
                    line += f" et {rest} autre{'s' if rest > 1 else ''}_"
                else:
                    line += "_"
            add(f"**{label}** — {line}")
        for t in by_status.get("deprecated", []):
            if t.get("notes"):
                add(f"  - ⚠️ {t['name']} abandonné : {t['notes']}")

    if data["commands"]:
        add("")
        add("## Commandes")
        for c in data["commands"]:
            where = f" (dans `{c['workdir']}`)" if c.get("workdir") else ""
            desc = f" — {c['description']}" if c.get("description") else ""
            add(f"- **{c['name']}** : `{c['command']}`{where}{desc}")

    if data["services"]:
        add("")
        add("## Services")
        for sv in data["services"]:
            bits = [b for b in (sv.get("url"),
                                f"port {sv['port']}" if sv.get("port") else None,
                                f"conteneur `{sv['container']}`" if sv.get("container") else None)
                    if b]
            add(f"- **{sv['name']}** ({sv['environment']}) — {' · '.join(bits) or sv.get('kind')}")

    if data["env_vars"]:
        add("")
        add("## Variables d'environnement")
        # Une liste de noms suffit ; seules celles qui portent une explication
        # méritent leur propre ligne.
        names = ", ".join(f"`{v['name']}`" + ("*" if v["secret"] else "")
                          for v in data["env_vars"])
        add(f"{names} — les valeurs vivent hors dépôt (* = secrète).")
        for v in data["env_vars"]:
            if v.get("description"):
                add(f"- `{v['name']}` : {v['description']}")

    if data["resources"]:
        add("")
        add("## Ressources")
        for r in data["resources"]:
            scope = "" if r.get("project_id") else " _(global)_"
            add(f"- [{r['title']}]({r['url']}) — {r['kind']}{scope}")

    if data["preferences"]:
        add("")
        add("## Préférences techniques générales")
        for pref in data["preferences"]:
            level = {"preferred": "👍", "avoid": "👎"}.get(pref["preference_level"], "•")
            add(f"- {level} **{pref['name']}** — {pref['preference']}")

    if data["journal"]:
        add("")
        add("## Journal récent")
        for entry in data["journal"]:
            link = f" (tâche #{entry['task_id']})" if entry.get("task_id") else ""
            add(f"- `{entry['created_at'][:16].replace('T', ' ')}` "
                f"[{entry['kind']}] {entry['summary']}{link}")

    add("")
    add("---")
    add("_Tenez cet état à jour au fil du travail : `update_task` quand une tâche avance, "
        "`log_work` après chaque étape notable, `add_memory` pour toute décision qui "
        "engage la suite._")
    return "\n".join(out)


def _tech_label(t: dict) -> str:
    label = t["name"]
    if t.get("version"):
        label += f" {t['version']}"
    if t.get("role"):
        label += f" ({t['role']})"
    return label


def _truncate(text: str, length: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[: length - 1] + "…"


def empty_briefing(path: str | None, known: list[dict]) -> str:
    """Message affiché quand aucun projet ne correspond au répertoire courant."""
    lines = [f"Aucun projet enregistré pour `{path}`." if path else "Aucun projet enregistré."]
    if known:
        lines.append("")
        lines.append("Projets connus : " + ", ".join(f"`{p['slug']}`" for p in known))
    lines.append("")
    lines.append("Créez-le avec l'outil `upsert_project` "
                 f"(slug, nom, chemin), puis `scan_stack` pour détecter la stack.")
    return "\n".join(lines)
