"""Serveur MCP : la face « Claude » de la base.

Les descriptions d'outils ci-dessous sont ce que Claude lit pour choisir quoi
appeler — elles disent donc quand utiliser chaque outil, pas seulement ce
qu'il fait.
"""
from mcp.server.fastmcp import FastMCP

from . import briefing as briefing_mod
from . import config, db, notify, repo, scanner

mcp = FastMCP("claude-manager", stateless_http=True)
# Servi à la racine du point de montage : l'application monte déjà sur /mcp.
mcp.settings.streamable_http_path = "/"


def _project(ref: str) -> dict:
    return repo.require_project(ref)


def _pid(ref: str) -> int:
    return _project(ref)["id"]


# --------------------------------------------------------------------------
# Démarrage de session
# --------------------------------------------------------------------------

@mcp.tool()
def get_briefing(project: str, max_tasks: int = 15) -> str:
    """L'ÉTAT COMPLET d'un projet, en Markdown : avancement, jalons, tâches en
    cours et à faire par priorité, mémoire, stack, commandes, services,
    variables d'environnement, ressources et journal récent.

    À appeler en tout début de session sur un projet, avant de lire du code.
    C'est le seul appel nécessaire pour savoir où on en est.
    """
    return briefing_mod.to_markdown(briefing_mod.build(project, max_tasks=max_tasks))


@mcp.tool()
def resolve_project(path: str) -> dict:
    """Retrouve le projet correspondant à un chemin sur le disque (le répertoire
    de travail courant, par exemple). Renvoie le projet ou la liste des projets
    connus si aucun ne correspond."""
    project = repo.resolve_project_by_path(path)
    if project:
        return {"found": True, "project": project}
    return {"found": False, "known_projects": [
        {"slug": p["slug"], "path": p["path"]} for p in repo.list_projects()]}


# --------------------------------------------------------------------------
# Projets
# --------------------------------------------------------------------------

@mcp.tool()
def list_projects(status: str | None = None, tag: str | None = None) -> list:
    """Liste les projets avec leur avancement et leur dernière activité.
    Filtres optionnels : statut (active, paused, archived) et tag de projet
    (ex. 'pro' pour ne voir que les projets professionnels)."""
    return repo.list_projects(status, tag=tag)


@mcp.tool()
def get_project(project: str) -> dict:
    """Fiche complète d'un projet sous forme structurée : stack, commandes,
    services, variables d'environnement, ressources, jalons et statistiques.
    Préférer get_briefing pour un démarrage de session."""
    data = briefing_mod.build(project)
    data.pop("journal", None)
    return data


@mcp.tool()
def upsert_project(slug: str, name: str | None = None, path: str | None = None,
                   description: str | None = None, repo_url: str | None = None,
                   status: str | None = None, tags: str | None = None) -> dict:
    """Crée un projet ou met à jour ses métadonnées. Le champ `path` (chemin
    absolu du dépôt) est ce qui permet de résoudre automatiquement le répertoire
    courant en projet — le renseigner à la création.

    `tags` : liste séparée par des virgules (ex. 'pro, client-x'), qui REMPLACE
    les tags du projet ; omis, ils restent inchangés ; '' les efface. Distincts
    des tags de tâches : ils classent le projet entier (perso / pro, client…)."""
    return repo.upsert_project(slug=slug, name=name, path=path, description=description,
                               repo_url=repo_url, status=status, tags=tags)


# --------------------------------------------------------------------------
# Tâches
# --------------------------------------------------------------------------

@mcp.tool()
def list_tasks(project: str, status: str | None = None, min_priority: int | None = None,
               tag: str | None = None, owner: str | None = None,
               milestone_id: int | None = None, include_done: bool = False,
               limit: int = 100) -> list:
    """Liste les tâches d'un projet, triées par statut puis priorité décroissante.
    Par défaut, seules les tâches ouvertes (todo, in_progress, blocked) sont
    renvoyées. Statuts possibles : todo, in_progress, blocked, done, cancelled."""
    return repo.list_tasks(_pid(project), status=status, min_priority=min_priority,
                           tag=tag, owner=owner, milestone_id=milestone_id,
                           include_done=include_done, limit=limit)


@mcp.tool()
def list_tags(project: str | None = None) -> list:
    """Le vocabulaire de tags en usage, avec le nombre de tâches concernées.

    À consulter AVANT d'étiqueter une tâche : réutiliser `ui` vaut mieux que
    d'inventer `interface`, sans quoi le vocabulaire se disperse et le filtrage
    par tag ne veut plus rien dire. Les tags sont normalisés à l'écriture —
    minuscules, sans dièse, espaces en tirets."""
    return repo.list_tags(_pid(project) if project else None)


@mcp.tool()
def retag(tag: str, project: str | None = None, status: str | None = None,
          priority: str | None = None, add_tag: str | None = None,
          remove_tag: str | None = None, apply: bool = False) -> dict:
    """Agit d'un coup sur toutes les tâches portant un tag : changer leur statut
    (par exemple les mettre en file), leur priorité, ou ajouter et retirer un tag.

    Avec apply=False (par défaut), montre seulement ce qui serait touché — une
    modification de masse doit être vue avant d'être subie. Repasser ensuite avec
    apply=True.

    Exemple : retag('ui', project='mare', status='queued', apply=True) met en file
    toutes les tâches marquées #ui."""
    return repo.retag(tag, project_id=_pid(project) if project else None,
                      status=status, priority=priority, add_tag=add_tag,
                      remove_tag=remove_tag, apply=apply)


@mcp.tool()
def get_task(task_id: int) -> dict:
    """Détail d'une tâche et de ses sous-tâches."""
    return repo.get_task(task_id)


@mcp.tool()
def create_task(project: str, title: str, body: str | None = None,
                priority: str = "normal", status: str = "todo", owner: str = "claude",
                tags: str | None = None, parent_id: int | None = None,
                milestone_id: int | None = None) -> dict:
    """Crée une tâche. Priorité : someday, low, normal, high, urgent (ou 0 à 4).
    `owner` vaut « claude » pour ce que je dois faire, « user » pour ce qui
    demande une action humaine. `tags` est une liste séparée par des virgules.

    Deux façons de détailler une tâche, affichées toutes deux comme checklist
    dans l'interface (compteur ☑ n/total) :
    - cases markdown `- [ ]` / `- [x]` dans `body` : checklist de suivi interne,
      groupable sous des titres `## …`, cochable d'un clic par l'humain ;
    - sous-tâches via `parent_id` : pour une étape qui a besoin de son propre
      statut, d'être déléguée à un agent ou suivie séparément.
    Le body est rendu en markdown (titres, gras, code, listes, liens)."""
    return repo.create_task(_pid(project), title=title, body=body, priority=priority,
                            status=status, owner=owner, tags=tags,
                            parent_id=parent_id, milestone_id=milestone_id)


@mcp.tool()
def update_task(task_id: int, status: str | None = None, priority: str | None = None,
                title: str | None = None, body: str | None = None,
                tags: str | None = None, owner: str | None = None,
                blocked_reason: str | None = None,
                milestone_id: int | None = None,
                cancel_reason: str | None = None) -> dict:
    """Met à jour une tâche : statut, priorité, contenu, tags, jalon.
    Passer status='in_progress' au moment où l'on commence, 'done' à la fin,
    'blocked' avec `blocked_reason` quand quelque chose empêche d'avancer.
    status='cancelled' EXIGE `cancel_reason` : pourquoi on abandonne (devenue
    inutile, remplacée par #N, approche rejetée…). La raison s'affiche sur la
    tâche et l'annulation est consignée au journal.

    `body` REMPLACE toute la description. Pour cocher une case `- [ ]`, relire
    la tâche (get_task), puis renvoyer le body complet en ne changeant que
    `[ ]` → `[x]`. L'humain peut aussi cocher depuis l'interface : toujours
    repartir du body actuel, jamais d'une copie ancienne."""
    if status == "cancelled" and not (cancel_reason or "").strip():
        current = repo.get_task(task_id)
        if current["status"] != "cancelled":
            raise ValueError("annuler une tâche demande une raison : repasser "
                             "update_task(status='cancelled', cancel_reason='…')")
    return repo.update_task(task_id, status=status, priority=priority, title=title,
                            body=body, tags=tags, owner=owner,
                            blocked_reason=blocked_reason, milestone_id=milestone_id,
                            cancel_reason=cancel_reason)


# --------------------------------------------------------------------------
# Jalons
# --------------------------------------------------------------------------

@mcp.tool()
def ask_user(task_id: int, question: str, options: list[str] | None = None,
             recommendation: str | None = None) -> dict:
    """Rend la main à Kevin sur une question, en lui proposant des réponses.

    À utiliser dès qu'une décision lui revient — arbitrage produit, ambiguïté de
    l'énoncé, choix qui engage la suite — plutôt que de trancher à sa place ou
    de deviner. La tâche passe en « Pour moi » et s'arrête là.

    Écrire des options **actionnables**, chacune sur le modèle
    « Choix — sa conséquence en une ligne ». Trois au maximum : au-delà, ce n'est
    plus une question, c'est un catalogue. Toujours dire dans `recommendation`
    celle que tu retiendrais et pourquoi — un avis motivé aide à trancher.

    Kevin répond d'un clic ou en texte libre ; sa réponse est ajoutée à l'énoncé
    et la tâche repart automatiquement en file."""
    task = repo.ask_user(task_id, question=question, options=options,
                         recommendation=recommendation)
    # C'est la notification qui compte le plus : la file est séquentielle, et une
    # question sans réponse arrête tout ce qui suit. On la prévient ici plutôt
    # que dans repo, pour que la couche données reste sans effet de bord.
    projet = repo.get_project(task.get("project_id")) or {}
    notify.demande(
        task_id,
        f"❓ Un agent attend ta réponse — {projet.get('name', '?')}",
        [f"#{task_id} {task.get('title', '')}", "", question]
        + ([f"Avis de l'agent : {recommendation}"] if recommendation else [])
        + ["", "Touche un bouton, ou réponds à ce message."],
        options,
        notify.lien_taches(projet.get("slug", "")),
    )
    return task


@mcp.tool()
def list_milestones(project: str, status: str | None = None) -> list:
    """Liste les jalons d'un projet avec leur pourcentage d'avancement."""
    return repo.list_milestones(_pid(project), status=status)


@mcp.tool()
def create_milestone(project: str, name: str, description: str | None = None,
                     target_date: str | None = None) -> dict:
    """Crée un jalon (phase, version, lot de travail) auquel rattacher des tâches.
    `target_date` au format AAAA-MM-JJ."""
    return repo.create_milestone(_pid(project), name=name, description=description,
                                 target_date=target_date)


@mcp.tool()
def update_milestone(milestone_id: int, name: str | None = None,
                     description: str | None = None, status: str | None = None,
                     target_date: str | None = None) -> dict:
    """Met à jour un jalon. Statuts : open, done, cancelled."""
    return repo.update_milestone(milestone_id, name=name, description=description,
                                 status=status, target_date=target_date)


# --------------------------------------------------------------------------
# Stack technique
# --------------------------------------------------------------------------

@mcp.tool()
def list_technologies(project: str | None = None, status: str | None = None) -> list:
    """Technologies d'un projet (avec version, rôle et statut), ou le catalogue
    global si `project` est omis. Statuts : active, considered, deprecated."""
    return repo.list_technologies(_pid(project) if project else None, status=status)


@mcp.tool()
def set_technology(project: str, name: str, category: str | None = None,
                   version: str | None = None, role: str | None = None,
                   status: str | None = None, notes: str | None = None,
                   docs_url: str | None = None) -> dict:
    """Ajoute ou met à jour une technologie sur un projet.
    Catégories : language, framework, database, infra, lib, tool, service.
    Statuts : active (en usage), considered (envisagée), deprecated (abandonnée).
    Mettre dans `notes` la RAISON du choix ou de l'abandon — c'est ce qui évite
    de reproposer plus tard une piste déjà écartée."""
    return repo.set_technology(_pid(project), name=name, category=category, version=version,
                               role=role, status=status, notes=notes, docs_url=docs_url)


@mcp.tool()
def remove_technology(project: str, name: str) -> dict:
    """Détache une technologie d'un projet. Pour garder la trace d'un abandon,
    préférer set_technology(status='deprecated', notes='pourquoi')."""
    repo.remove_technology(_pid(project), name)
    return {"removed": name, "project": project}


@mcp.tool()
def scan_stack(project: str, apply: bool = True) -> dict:
    """Analyse les fichiers du projet sur le disque (package.json, pyproject.toml,
    requirements.txt, go.mod, Cargo.toml, composer.json, Dockerfile,
    docker-compose.yml, .env.example) et en déduit technologies, commandes,
    services et variables d'environnement.

    Avec apply=True, enregistre ce qui a été détecté sans écraser les
    informations saisies à la main. Avec apply=False, se contente de montrer."""
    project_row = _project(project)
    if not project_row.get("path"):
        raise ValueError(f"le projet « {project} » n'a pas de chemin enregistré ; "
                         "renseignez-le avec upsert_project(path=...)")
    found = scanner.scan(project_row["path"])
    summary = {
        "scanned_files": found["scanned"],
        "errors": found["errors"],
        "technologies": len(found["technologies"]),
        "commands": len(found["commands"]),
        "services": len(found["services"]),
        "env_vars": len(found["env_vars"]),
        "notes": found["notes"],
        "applied": apply,
    }
    if not apply:
        summary["detail"] = found
        return summary

    pid = project_row["id"]
    with db.cursor() as conn:
        for name, info in found["technologies"].items():
            repo.set_technology(pid, name=name, category=info.get("category"),
                                version=info.get("version"), role=info.get("role"),
                                docs_url=info.get("docs_url"), source="scan", conn=conn)
        for name, info in found["commands"].items():
            repo.set_command(pid, name=name, command=info["command"],
                             description=info.get("description"), conn=conn)
        for name, info in found["services"].items():
            repo.set_service(pid, name=name, kind=info.get("kind"), url=info.get("url"),
                             port=info.get("port"), container=info.get("container"),
                             notes=info.get("notes"), conn=conn)
        for name, info in found["env_vars"].items():
            repo.set_env_var(pid, name=name, required=info.get("required"),
                             secret=info.get("secret"), location=info.get("location"),
                             example=info.get("example"), conn=conn)
        if found.get("repo_url") and not project_row.get("repo_url"):
            repo.upsert_project(project_row["slug"], repo_url=found["repo_url"], conn=conn)
        repo.log_work(pid, summary=f"Scan de la stack : {summary['technologies']} technologies, "
                                   f"{summary['commands']} commandes détectées",
                      kind="note", conn=conn)
    return summary


@mcp.tool()
def set_preference(name: str, preference: str, level: str = "preferred",
                   category: str | None = None) -> dict:
    """Enregistre une préférence technique GÉNÉRALE, indépendante des projets —
    par exemple « pour une API Python, préférer FastAPI » ou « éviter les ORM
    lourds ». Niveaux : preferred, neutral, avoid. Ces préférences apparaissent
    dans tous les briefings."""
    return repo.set_preference(name=name, preference=preference, level=level,
                               category=category)


@mcp.tool()
def list_preferences() -> list:
    """Liste les préférences techniques générales."""
    return repo.list_preferences()


@mcp.tool()
def projects_using(name: str) -> list:
    """Quels projets utilisent une technologie donnée. Utile avant d'introduire
    un outil : on a peut-être déjà résolu le problème ailleurs."""
    return repo.projects_using(name)


# --------------------------------------------------------------------------
# Documentation
# --------------------------------------------------------------------------

@mcp.tool()
def scan_docs(project: str) -> list:
    """Repère les fichiers markdown du dépôt et inscrit ceux qui manquent.

    À lancer une fois sur un projet, puis quand la documentation bouge. La
    détection trouve les fichiers ; c'est ensuite à toi de dire ce que chacun
    couvre, avec `set_doc` — un index sans cette phrase ne sert à rien."""
    p = repo.require_project(project)
    return repo.scan_docs(p["id"], root=p.get("path") or "")


@mcp.tool()
def set_doc(project: str, path: str, covers: str, title: str | None = None) -> dict:
    """Dit ce qu'on trouve dans un fichier de documentation du dépôt.

    `path` est relatif à la racine du dépôt, `covers` répond à une seule
    question : **quand faut-il ouvrir ce fichier ?** Écrire « décrit
    l'architecture » n'aide personne ; « le contrat entre core et client, et
    pourquoi le client ne recalcule jamais » fait gagner une demi-heure.

    On n'enregistre qu'un pointeur : le contenu reste dans le dépôt, où il
    change dans le même commit que le code qu'il décrit."""
    return repo.set_doc(_pid(project), path=path, title=title, covers=covers)


@mcp.tool()
def list_docs(project: str) -> list:
    """L'index de la documentation d'un projet."""
    return repo.list_docs(_pid(project))


@mcp.tool()
def delete_doc(doc_id: int) -> dict:
    """Retire une entrée de l'index — fichier supprimé ou renommé."""
    repo.delete_doc(doc_id)
    return {"deleted": doc_id}


# --------------------------------------------------------------------------
# Méthodologie
# --------------------------------------------------------------------------

@mcp.tool()
def set_practice(project: str, title: str, body: str, category: str = "general") -> dict:
    """Enregistre une pratique de travail du projet — sa méthodologie.

    Le titre identifie la pratique : réappeler avec le même titre la remplace.

    **Ce qui va ici** : ce que le dépôt ne dit pas. Ce que « terminé » veut dire
    sur ce projet, le périmètre qu'on ne franchit pas sans décision préalable,
    comment une tâche doit être écrite pour être délégable, le rituel de revue,
    ce qu'on attend avant de fusionner.

    **Ce qui n'y va PAS** : les conventions de code. Elles doivent changer dans
    le même commit que le code qu'elles décrivent, donc elles vivent dans le
    CLAUDE.md du dépôt. Recopiées ici, elles se décorrèlent de la branche et
    finissent par mentir.

    Catégories qui se lisent bien : `demarrage`, `termine`, `perimetre`,
    `revue`, `tests`, `livraison`, `documentation`. Appeler `list_practices`
    avant d'écrire, pour reprendre une catégorie existante."""
    return repo.set_practice(_pid(project), title=title, body=body, category=category)


@mcp.tool()
def list_practices(project: str) -> list:
    """La méthodologie d'un projet : comment on y travaille."""
    return repo.list_practices(_pid(project))


@mcp.tool()
def delete_practice(practice_id: int) -> dict:
    """Retire une pratique devenue fausse. Vérifier avant qu'elle ne l'est plus."""
    repo.delete_practice(practice_id)
    return {"deleted": practice_id}


# --------------------------------------------------------------------------
# Fiche projet
# --------------------------------------------------------------------------

@mcp.tool()
def set_command(project: str, name: str, command: str, workdir: str | None = None,
                description: str | None = None) -> dict:
    """Enregistre une commande du projet (dev, test, build, deploy, lint…),
    pour ne plus avoir à la deviner à la session suivante."""
    return repo.set_command(_pid(project), name=name, command=command,
                            workdir=workdir, description=description)


@mcp.tool()
def set_service(project: str, name: str, kind: str = "url", url: str | None = None,
                port: int | None = None, container: str | None = None,
                environment: str = "prod", notes: str | None = None) -> dict:
    """Enregistre un service du projet : conteneur, URL publique, port, endpoint.
    Types : container, url, port, endpoint. Environnements : prod, staging, dev."""
    return repo.set_service(_pid(project), name=name, kind=kind, url=url, port=port,
                            container=container, environment=environment, notes=notes)


@mcp.tool()
def set_env_var(project: str, name: str, required: bool = True, secret: bool = False,
                location: str | None = None, description: str | None = None,
                example: str | None = None) -> dict:
    """Déclare une variable d'environnement attendue par le projet.
    NE JAMAIS y mettre la valeur d'un secret : uniquement le nom, l'endroit où
    la variable est définie, et éventuellement un exemple de format non sensible."""
    return repo.set_env_var(_pid(project), name=name, required=required, secret=secret,
                            location=location, description=description, example=example)


@mcp.tool()
def add_resource(title: str, url: str, project: str | None = None,
                 kind: str = "other", notes: str | None = None) -> dict:
    """Ajoute un lien utile : documentation, dépôt, ticket, maquette, tableau de
    bord. Sans `project`, la ressource est globale. Types : docs, repo, ticket,
    design, dashboard, other."""
    return repo.add_resource(_pid(project) if project else None, title=title, url=url,
                             kind=kind, notes=notes)


@mcp.tool()
def remove_item(kind: str, item_id: int) -> dict:
    """Supprime un élément de la fiche projet.
    `kind` : command, service, env_var, resource."""
    repo.delete_profile_item(kind, item_id)
    return {"removed": kind, "id": item_id}


# --------------------------------------------------------------------------
# Mémoire
# --------------------------------------------------------------------------

@mcp.tool()
def add_memory(title: str, body: str, project: str | None = None, kind: str = "note",
               tags: str | None = None, pinned: bool = False) -> dict:
    """Enregistre quelque chose à ne pas réapprendre : une décision d'architecture
    et son pourquoi, une convention du projet, un piège rencontré, un élément de
    contexte. Types : decision, convention, gotcha, context, note.

    Sans `project`, la mémoire est globale (vraie pour tous les projets).
    `pinned=True` la fait apparaître systématiquement en tête du briefing.

    À utiliser dès qu'une décision est prise — pas en fin de session, où elle
    sera oubliée."""
    return repo.add_memory(_pid(project) if project else None, title=title, body=body,
                           kind=kind, tags=tags, pinned=pinned)


@mcp.tool()
def update_memory(memory_id: int, title: str | None = None, body: str | None = None,
                  kind: str | None = None, tags: str | None = None,
                  pinned: bool | None = None) -> dict:
    """Met à jour une mémoire — notamment lorsqu'une décision est révisée."""
    return repo.update_memory(memory_id, title=title, body=body, kind=kind,
                              tags=tags, pinned=pinned)


@mcp.tool()
def list_memories(project: str | None = None, kind: str | None = None,
                  limit: int = 50) -> list:
    """Liste les mémoires d'un projet (les mémoires globales sont incluses)."""
    return repo.list_memories(_pid(project) if project else None, kind=kind, limit=limit)


@mcp.tool()
def delete_memory(memory_id: int) -> dict:
    """Supprime une mémoire devenue fausse."""
    repo.delete_memory(memory_id)
    return {"deleted": memory_id}


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------

@mcp.tool()
def log_work(project: str, summary: str, detail: str | None = None, kind: str = "work",
             task_id: int | None = None, session_id: str | None = None) -> dict:
    """Consigne ce qui vient d'être fait, pour que la session suivante reprenne
    le fil. Types : work, decision, note, blocker, milestone.

    Écrire une entrée après chaque étape notable : une fonctionnalité livrée,
    un bug corrigé, un blocage rencontré. Le résumé doit être compréhensible
    hors contexte, dans trois semaines."""
    return repo.log_work(_pid(project), summary=summary, detail=detail, kind=kind,
                         task_id=task_id, session_id=session_id)


@mcp.tool()
def list_journal(project: str | None = None, limit: int = 30,
                 kind: str | None = None) -> list:
    """Historique du travail, du plus récent au plus ancien.
    Sans `project`, l'historique porte sur tous les projets."""
    return repo.list_journal(_pid(project) if project else None, limit=limit, kind=kind)


# --------------------------------------------------------------------------
# Recherche
# --------------------------------------------------------------------------

@mcp.tool()
def search(query: str, project: str | None = None, types: str | None = None,
           limit: int = 30) -> list:
    """Recherche plein texte sur tout : tâches, mémoires, journal, jalons,
    ressources, technologies. `types` filtre par type d'entité, séparés par des
    virgules (task, memory, journal, milestone, resource, technology, project).

    À utiliser avant de créer une tâche ou une mémoire, pour vérifier que le
    sujet n'est pas déjà couvert."""
    entity_types = [t.strip() for t in types.split(",")] if types else None
    return repo.search(query, project_id=_pid(project) if project else None,
                       entity_types=entity_types, limit=limit)


def tool_count() -> int:
    return len(mcp._tool_manager.list_tools())
