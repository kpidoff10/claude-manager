"""Interface web : la face « humaine » de la base.

Chaque action de modification renvoie soit le panneau réactualisé (appel
JavaScript), soit une redirection vers l'onglet (formulaire classique) — de
sorte que l'interface reste utilisable même sans JavaScript.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from markupsafe import Markup

from .. import (auth, briefing as briefing_mod, config, db, gitinfo, markup, rappels,
                notify, repo, runlog, scanner)

router = APIRouter()
templates = Jinja2Templates(directory=config.BASE_DIR / "web" / "templates")
# `markdown` rend le texte des agents. Il échappe AVANT de mettre en forme et
# n'émet qu'une liste close de balises : `markupsafe.Markup` est donc sûr ici,
# et seulement ici. Ne jamais l'appliquer à du texte non passé par markup.rendu.
templates.env.filters["markdown"] = lambda texte: Markup(markup.rendu(texte or ""))
templates.env.filters["rappel"] = rappels.libelle
templates.env.filters["rappel_echu"] = rappels.echu
templates.env.tests["rappel_echu"] = rappels.echu
templates.env.filters["rappel_champ"] = rappels.valeur_champ
templates.env.globals["rappel_raccourcis"] = rappels.RACCOURCIS
templates.env.globals["test_results"] = config.TEST_RESULTS
templates.env.filters["markdown_ligne"] = lambda texte: Markup(markup.en_ligne(texte or ""))
# Même garantie : les cases cliquables sont posées par markup.rendu, après échappement.
templates.env.filters["markdown_taches"] = lambda texte, slug, task_id, view: Markup(markup.rendu(
    texte or "", cases={"action": f"/p/{slug}/tasks/{task_id}/check",
                        "champs": {"tab": "tasks", "view": view}}))
# Les mêmes cases dans la fiche (boîte de dialogue) : le clic réaffiche la fiche.
templates.env.filters["markdown_fiche"] = lambda texte, slug, task_id: Markup(markup.rendu(
    texte or "", cases={"action": f"/p/{slug}/tasks/{task_id}/check",
                        "champs": {"tab": "tasks", "retour": "fiche"},
                        "cible": "task-dialog-body"}))
# Suffixe d'URL des fichiers statiques : il change à chaque déploiement et
# force le navigateur à redemander la feuille de style. Voir config.
templates.env.globals["static_v"] = config.STATIC_VERSION

TABS = [("tasks", "Tâches"), ("milestones", "Jalons"), ("method", "Méthodologie"),
        ("stack", "Stack"), ("profile", "Fiche"), ("memory", "Mémoire"),
        ("journal", "Journal"), ("signalements", "Signalements")]
TAB_KEYS = [t[0] for t in TABS]


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------

def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _int(value):
    value = _clean(value)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _is_ajax(request: Request) -> bool:
    return request.headers.get("x-cm-panel") == "1"


KANBAN_COLUMNS = [("todo", "À faire"), ("queued", "File d'attente"),
                  ("in_progress", "En cours"), ("needs_input", "Pour moi"),
                  ("review", "À vérifier"), ("done", "Terminé")]
# « Bloqué » n'a pas sa colonne : un empêchement et une question de l'agent
# appellent la même chose — ton intervention. Les deux se rangent donc au même
# endroit, distingués par un badge.
COLUMN_ALIASES = {"blocked": "needs_input"}


def _panel_context(request: Request, project: dict, tab: str, view: str = "liste") -> dict:
    pid = project["id"]
    ctx = {"request": request, "project": project, "tab": tab, "view": view,
           "priorities": config.PRIORITIES, "statuses": config.TASK_STATUSES,
           "memory_kinds": config.MEMORY_KINDS,
           "tech_categories": config.TECH_CATEGORIES,
           "tech_statuses": config.TECH_STATUSES,
           "kanban_columns": KANBAN_COLUMNS}
    if tab == "tasks":
        tasks = repo.list_tasks(pid, include_done=True, limit=500)
        ctx["milestones"] = repo.list_milestones(pid)
        # Les sous-tâches forment la checklist de leur parente. Elles quittent la
        # liste principale, sauf si la parente est close : une sous-tâche encore
        # ouverte sous une tâche terminée ne doit pas disparaître de la vue.
        by_id = {t["id"]: t for t in tasks}
        subtasks: dict[int, list] = {}
        rappels_taches = repo.reminders_by_task(pid)
        tests = repo.tests_for_project(pid)
        for task in tasks:
            task["tests"] = tests.get(task["id"], [])
            task["tests_summary"] = repo.summarise_tests(task["tests"])
            task["checks"] = markup.compter_cases(task.get("body"))
            task["reminder"] = rappels_taches.get(task["id"])
            parent = by_id.get(task.get("parent_id"))
            task["nested"] = bool(parent and parent["status"] not in ("done", "cancelled"))
            if parent:
                subtasks.setdefault(parent["id"], []).append(task)
        for items in subtasks.values():
            items.sort(key=lambda t: (t["order_index"], t["id"]))
        ctx["subtasks"] = subtasks
        # Au kanban, une sous-tâche imbriquée ne garde sa carte que si elle
        # demande de l'attention (en file, en cours, à relire…).
        tasks = [t for t in tasks
                 if not (t["nested"] and view == "kanban"
                         and t["status"] in ("todo", "done", "cancelled"))]
        ctx["tasks"] = tasks
        columns = {key: [] for key, _ in KANBAN_COLUMNS}
        for task in tasks:
            columns.setdefault(COLUMN_ALIASES.get(task["status"], task["status"]), []) \
                   .append(task)
        ctx["columns"] = columns
        # Un tag dont toutes les tâches sont terminées n'a plus rien à filtrer :
        # il n'encombre pas la barre. Le vocabulaire complet reste rendu par
        # `repo.list_tags`, que le MCP interroge pour éviter les doublons.
        ctx["tags"] = [t for t in repo.list_tags(pid) if t["open"]]
        # Toute tâche qui a connu un agent mérite son lien vers le journal, pas
        # seulement celles en attente de relecture.
        ctx["runs"] = repo.latest_runs(
            [t["id"] for t in tasks
             if t["status"] in ("in_progress", "review", "needs_input", "done")])
        ctx["questions"] = {t["id"]: repo.question_out(t.get("question"))
                            for t in tasks if t["status"] == "needs_input"}
        ctx["awaiting"] = repo.awaiting_user(pid)
        ctx["queue"] = _queue_state()
        # Pourquoi la file ne démarre pas : la question se pose devant l'écran,
        # la réponse doit donc s'y trouver.
        ctx["queue_blocker"] = gitinfo.blocker(project.get("path")) \
            if columns.get("queued") else None
    elif tab == "milestones":
        ctx["milestones"] = repo.list_milestones(pid)
        # Un jalon qui annonce « 0/2 » doit montrer lesquelles. On récupère tout
        # en une fois et on regroupe, plutôt qu'une requête par jalon.
        tasks = repo.list_tasks(pid, include_done=True, limit=500)
        grouped: dict[int, list] = {}
        for task in tasks:
            grouped.setdefault(task["milestone_id"], []).append(task)
        ctx["tasks_by_milestone"] = grouped
        ctx["orphan_tasks"] = grouped.get(None, [])
    elif tab == "method":
        ctx["practices"] = repo.list_practices(pid)
        ctx["practice_categories"] = config.PRACTICE_CATEGORIES
    elif tab == "stack":
        ctx["technologies"] = repo.list_technologies(pid)
        ctx["preferences"] = repo.list_preferences()
    elif tab == "profile":
        ctx["docs"] = repo.list_docs(pid)
        ctx["commands"] = repo.list_commands(pid)
        ctx["services"] = repo.list_services(pid)
        ctx["env_vars"] = repo.list_env_vars(pid)
        ctx["resources"] = repo.list_resources(pid)
    elif tab == "memory":
        ctx["memories"] = repo.list_memories(pid, limit=300)
    elif tab == "journal":
        ctx["journal"] = repo.list_journal(pid, limit=200)
    elif tab == "signalements":
        tickets = repo.list_tickets(project_id=pid, limit=300)
        ctx["a_valider"] = [t for t in tickets if t["status"] == "submitted"]
        ctx["en_discussion"] = [t for t in tickets
                                if t["status"] == "draft" and t["user_messages"]]
        ctx["traites"] = [t for t in tickets if t["status"] in ("accepted", "rejected")]
        ctx["signaleurs"] = [r for r in repo.list_reporters() if pid in r["project_ids"]]
    return ctx


def _render_panel(request: Request, project: dict, tab: str,
                  view: str = "liste") -> HTMLResponse:
    ctx = _panel_context(request, project, tab, view)
    return templates.TemplateResponse(request, f"partials/{tab}.html", ctx)


def _respond(request: Request, slug: str, tab: str, form=None):
    """Fragment si l'appel vient du JavaScript, redirection sinon.

    Un même formulaire peut être servi depuis plusieurs onglets et plusieurs
    vues — modifier une tâche depuis les jalons doit réafficher les jalons, et
    déplacer une carte du kanban doit redonner le kanban. Les champs cachés
    `tab` et `view` portent cette information.
    """
    view = "liste"
    if form is not None:
        requested = _clean(form.get("tab"))
        if requested in TAB_KEYS:
            tab = requested
        view = _clean(form.get("view")) or "liste"
    project = repo.require_project(slug)
    if _is_ajax(request):
        return _render_panel(request, project, tab, view)
    suffix = "?view=kanban" if view == "kanban" else ""
    return RedirectResponse(f"/p/{slug}/{tab}{suffix}", status_code=303)


# --------------------------------------------------------------------------
# Connexion
# --------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": None})


@router.post("/login")
async def login(request: Request):
    form = await request.form()
    target = _clean(form.get("next")) or "/"
    if not auth.check_password(str(form.get("password") or "")):
        return templates.TemplateResponse(
            request, "login.html",
            {"next": target, "error": "Mot de passe incorrect."}, status_code=401)
    response = RedirectResponse(target, status_code=303)
    auth.issue_cookie(response)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    auth.clear_cookie(response)
    return response


# --------------------------------------------------------------------------
# Tableau de bord
# --------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    projects = repo.list_projects()
    return templates.TemplateResponse(request, "dashboard.html", {
        "page": "dashboard",
        "projects": projects,
        "journal": repo.list_journal(limit=25),
        "hot_tasks": _hot_tasks(projects),
        # Les tâches « en cours » que plus aucune exécution ne porte. Sans ce
        # signal elles disparaissent des radars : la file ne les réclame pas, et
        # le tableau les montre en cours.
        "orphans": repo.orphan_tasks(),
        "preferences": repo.list_preferences(),
    })


def _hot_tasks(projects: list[dict]) -> list[dict]:
    """Tâches prioritaires tous projets confondus, pour la vue d'ensemble."""
    rows = []
    for project in projects:
        if project["status"] != "active":
            continue
        for task in repo.list_tasks(project["id"], min_priority=3, limit=10):
            task["project_slug"] = project["slug"]
            task["project_name"] = project["name"]
            rows.append(task)
    order = {"in_progress": 0, "blocked": 1, "todo": 2}
    rows.sort(key=lambda t: (-t["priority"], order.get(t["status"], 3)))
    return rows[:20]


@router.post("/projects/new")
async def create_project(request: Request):
    form = await request.form()
    slug = _clean(form.get("slug"))
    if not slug:
        return RedirectResponse("/", status_code=303)
    project = repo.upsert_project(
        slug=slug, name=_clean(form.get("name")) or slug, path=_clean(form.get("path")),
        description=_clean(form.get("description")), tags=form.get("tags") or None)
    if project.get("path") and _clean(form.get("scan")):
        try:
            _apply_scan(project)
        except Exception:
            pass  # un scan raté ne doit pas empêcher la création du projet
    return RedirectResponse(f"/p/{slug}/tasks", status_code=303)


# --------------------------------------------------------------------------
# Projet
# --------------------------------------------------------------------------

def _il_y_a(stamp: str | None) -> str:
    """« à l'instant », « il y a 4 min », « il y a 2 h », « il y a 3 j »."""
    if not stamp:
        return ""
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    secondes = max(0, (datetime.now(timezone.utc) - moment).total_seconds())
    if secondes < 60:
        return "à l'instant"
    for unite, taille in (("j", 86400), ("h", 3600), ("min", 60)):
        if secondes >= taille:
            return f"il y a {int(secondes // taille)} {unite}"
    return ""


def _activity_context(project: dict) -> dict:
    activity = repo.project_activity(project["id"])
    last = activity["last_journal"]
    activity["last_age"] = _il_y_a(last["created_at"]) if last else ""
    activity["recent"] = bool(last and repo._is_recent(last["created_at"]))
    return {"activity": activity}


@router.get("/p/{slug}/live")
def project_live(request: Request, slug: str):
    """Interrogé toutes les quelques secondes par la page du projet : l'empreinte
    dit s'il faut recharger le panneau, le fragment met le bandeau à jour."""
    project = repo.require_project(slug)
    html = templates.get_template("partials/activity.html").render(
        {"request": request, "project": project, **_activity_context(project)})
    return JSONResponse({"version": repo.project_version(project["id"]), "activity": html})


@router.get("/task/{task_id}", response_class=HTMLResponse)
def task_card(request: Request, task_id: int):
    """Fiche d'une tâche : dans la boîte de dialogue ouverte d'un clic sur
    « ↳ #N », ou en page entière si le JavaScript ne charge pas."""
    try:
        task = repo.get_task(task_id)
    except repo.NotFound:
        raise HTTPException(status_code=404, detail=f"tâche #{task_id} introuvable")
    project = repo.get_project(task["project_id"])
    task["checks"] = markup.compter_cases(task.get("body"))
    task["reminders"] = repo.list_reminders(task_id=task_id)
    task["tests"] = repo.list_tests(task_id)
    task["tests_summary"] = repo.summarise_tests(task["tests"])
    for s in task.get("subtasks") or []:
        s["tests_summary"] = repo.summarise_tests(repo.list_tests(s["id"]))
    ctx = {"t": task, "project": project,
           "milestone": next((m for m in repo.list_milestones(project["id"])
                              if m["id"] == task.get("milestone_id")), None)}
    if _is_ajax(request):
        return templates.TemplateResponse(request, "partials/task_card.html", ctx)
    ctx.update({"projects": repo.list_projects()})
    return templates.TemplateResponse(request, "task.html", ctx)


@router.get("/p/{slug}/briefing.md", response_class=PlainTextResponse)
def project_briefing(slug: str):
    """Le briefing tel que Claude le reçoit — pratique pour vérifier ce qu'il voit."""
    return briefing_mod.to_markdown(briefing_mod.build(slug))


@router.get("/p/{slug}", response_class=HTMLResponse)
def project_root(slug: str):
    return RedirectResponse(f"/p/{slug}/tasks", status_code=303)


@router.get("/p/{slug}/{tab}", response_class=HTMLResponse)
def project_tab(request: Request, slug: str, tab: str):
    if tab not in TAB_KEYS:
        return RedirectResponse(f"/p/{slug}/tasks", status_code=303)
    project = repo.require_project(slug)
    project.update(repo.project_stats(project["id"]))
    view = "kanban" if request.query_params.get("view") == "kanban" else "liste"
    if _is_ajax(request):
        return _render_panel(request, project, tab, view)
    ctx = _panel_context(request, project, tab, view)
    ctx.update({"projects": repo.list_projects(), "tabs": TABS,
                "tab_label": dict(TABS).get(tab, tab),
                "live_version": repo.project_version(project["id"]),
                "project_reminders": repo.list_reminders(project["id"]),
                "support_attente": len(repo.list_tickets(project_id=project["id"],
                                                         status="submitted")),
                **_activity_context(project)})
    return templates.TemplateResponse(request, "project.html", ctx)


@router.post("/p/{slug}/settings")
async def update_project(request: Request, slug: str):
    form = await request.form()
    repo.upsert_project(slug=slug, name=_clean(form.get("name")),
                        path=_clean(form.get("path")),
                        repo_url=_clean(form.get("repo_url")),
                        description=_clean(form.get("description")),
                        status=_clean(form.get("status")),
                        # Champ vide = tags effacés ; champ absent = inchangés.
                        tags=form.get("tags"))
    return RedirectResponse(f"/p/{slug}/tasks", status_code=303)


@router.post("/p/{slug}/delete")
def delete_project(slug: str):
    repo.delete_project(slug)
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# Tâches
# --------------------------------------------------------------------------

@router.post("/p/{slug}/tasks/new")
async def new_task(request: Request, slug: str):
    form = await request.form()
    title = _clean(form.get("title"))
    if title:
        repo.create_task(
            repo.require_project(slug)["id"], title=title, body=_clean(form.get("body")),
            priority=_clean(form.get("priority")) or "normal",
            owner=_clean(form.get("owner")) or "claude",
            tags=_clean(form.get("tags")), parent_id=_int(form.get("parent_id")),
            milestone_id=_int(form.get("milestone_id")))
    return _respond(request, slug, "tasks", form)


@router.post("/p/{slug}/tasks/{task_id}/update")
async def edit_task(request: Request, slug: str, task_id: int):
    form = await request.form()
    milestone = form.get("milestone_id")
    repo.update_task(
        task_id, status=_clean(form.get("status")), priority=_clean(form.get("priority")),
        title=_clean(form.get("title")), body=_clean(form.get("body")),
        tags=form.get("tags") if form.get("tags") is not None else None,
        owner=_clean(form.get("owner")),
        blocked_reason=_clean(form.get("blocked_reason")),
        cancel_reason=_clean(form.get("cancel_reason")), actor="user",
        milestone_id=_int(milestone) if _clean(milestone) else None)
    return _respond(request, slug, "tasks", form)


@router.post("/p/{slug}/tasks/{task_id}/remind")
async def remind_task(request: Request, slug: str, task_id: int):
    """Pose, déplace ou retire le rappel d'une tâche. `quand` vient d'un
    raccourci (« demain ») ou du champ date-heure ; « retirer » l'efface."""
    form = await request.form()
    if _clean(form.get("retirer")):
        repo.set_task_reminder(task_id, None)
    else:
        quand = _quand(form)
        if quand:
            repo.set_task_reminder(task_id, quand, _clean(form.get("note")))
    return _respond(request, slug, "tasks", form)


def _quand(form) -> str | None:
    """La date d'un formulaire de rappel, en UTC : un raccourci (« demain »)
    ou le champ date-heure. None si rien de compréhensible."""
    texte = _clean(form.get("quand")) or _clean(form.get("quand_precis"))
    if not texte:
        return None
    try:
        return rappels.en_utc(rappels.interprete(texte))
    except rappels.DateIncomprise:
        return None


# --------------------------------------------------------------------------
# Rappels de projet
# --------------------------------------------------------------------------

def _reminders_fragment(request: Request, project: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/project_reminders.html", {
        "project": project, "project_reminders": repo.list_reminders(project["id"])})


def _reminders_block(request: Request, project: dict):
    """Le bloc « Rappels » d'un projet : fragment pour le JavaScript, retour
    à la page sinon."""
    if _is_ajax(request):
        return _reminders_fragment(request, project)
    return RedirectResponse(request.headers.get("referer") or f"/p/{project['slug']}/tasks",
                            status_code=303)


@router.get("/p/{slug}/reminders", response_class=HTMLResponse)
def project_reminders(request: Request, slug: str):
    return _reminders_fragment(request, repo.require_project(slug))


@router.post("/p/{slug}/reminders/new")
async def project_reminder_new(request: Request, slug: str):
    form = await request.form()
    project = repo.require_project(slug)
    quand = _quand(form)
    if quand:
        repo.add_reminder(project["id"], quand, _clean(form.get("note")))
    return _reminders_block(request, project)


@router.post("/p/{slug}/reminders/{reminder_id}/snooze")
async def project_reminder_snooze(request: Request, slug: str, reminder_id: int):
    form = await request.form()
    quand = _quand(form)
    if quand:
        repo.update_reminder(reminder_id, remind_at=quand)
    return _reminders_block(request, repo.require_project(slug))


@router.post("/p/{slug}/reminders/{reminder_id}/delete")
async def project_reminder_delete(request: Request, slug: str, reminder_id: int):
    repo.delete_reminder(reminder_id)
    return _reminders_block(request, repo.require_project(slug))


def _after_test(request: Request, slug: str, form, task_id: int):
    """Depuis la fiche (boîte de dialogue), on renvoie la fiche ; depuis la
    liste, le panneau."""
    if _clean(form.get("retour")) == "fiche":
        if _is_ajax(request):
            return task_card(request, task_id)
        # Sans JavaScript, la fiche est une page entière : on y revient.
        return RedirectResponse(f"/task/{task_id}", status_code=303)
    return _respond(request, slug, "tasks", form)


@router.post("/p/{slug}/tasks/{task_id}/tests")
async def add_task_test(request: Request, slug: str, task_id: int):
    form = await request.form()
    what = _clean(form.get("what"))
    if what and _clean(form.get("result")):
        try:
            repo.add_test(task_id, what, _clean(form.get("result")),
                          environment=_clean(form.get("environment")),
                          detail=_clean(form.get("detail")), actor="user")
        except ValueError:
            pass
    return _after_test(request, slug, form, task_id)


@router.post("/p/{slug}/tasks/{task_id}/tests/{test_id}/delete")
async def delete_task_test(request: Request, slug: str, task_id: int, test_id: int):
    form = await request.form()
    repo.delete_test(test_id)
    return _after_test(request, slug, form, task_id)


@router.post("/p/{slug}/tasks/{task_id}/files")
async def add_task_files(request: Request, slug: str, task_id: int):
    """Captures et fichiers joints à une tâche, depuis sa fiche."""
    from .. import captures
    form = await request.form()
    captures.joins_tache(await captures.lis_tous(form.getlist("fichiers")), task_id)
    return _after_test(request, slug, form, task_id)


@router.post("/p/{slug}/tasks/{task_id}/files/{file_id}/delete")
async def delete_task_file(request: Request, slug: str, task_id: int, file_id: int):
    from .. import captures
    form = await request.form()
    fichier = repo.get_task_file(file_id)
    if fichier and fichier["task_id"] == task_id:
        captures.efface(repo.delete_task_file(file_id))
    return _after_test(request, slug, form, task_id)


@router.get("/task-file/{file_id}")
def task_file(file_id: int):
    from .. import captures
    return captures.reponse(repo.get_task_file(file_id))


@router.post("/p/{slug}/tasks/{task_id}/check")
async def check_task_item(request: Request, slug: str, task_id: int):
    """Coche ou décoche une case `- [ ]` de la description d'une tâche."""
    form = await request.form()
    task = repo.get_task(task_id)
    body = markup.basculer_case(task.get("body") or "", _int(form.get("rang")) or 0,
                                _clean(form.get("empreinte")))
    if body is not None:
        repo.update_task(task_id, body=body)
    return _after_test(request, slug, form, task_id)


@router.post("/p/{slug}/tasks/{task_id}/delete")
async def remove_task(request: Request, slug: str, task_id: int):
    form = await request.form()
    repo.delete_task(task_id)
    return _respond(request, slug, "tasks", form)


def _queue_state() -> dict:
    return {"paused": repo.get_setting("queue_paused", "0") == "1",
            "running": repo.running_run(),
            "running_all": repo.running_runs(),
            "max_parallel": config.MAX_PARALLEL,
            "pending": len(repo.queue_pending())}


@router.post("/p/{slug}/tasks/{task_id}/validate")
async def validate_task(request: Request, slug: str, task_id: int):
    """La relecture a eu lieu et le travail est accepté."""
    form = await request.form()
    repo.update_task(task_id, status="done")
    fusion = _clean(form.get("merge"))
    detail = ""
    if fusion:
        runs = repo.list_runs(task_id, limit=1)
        if runs and runs[0].get("branch"):
            repo.request_merge(runs[0]["id"])
            detail = f" Fusion de {runs[0]['branch']} demandée."
    repo.log_work(repo.require_project(slug)["id"], task_id=task_id, kind="work", actor="user",
                  summary=f"Travail de l'agent validé sur la tâche #{task_id}.{detail}")
    return _respond(request, slug, "tasks", form)


@router.post("/p/{slug}/tasks/{task_id}/reject")
async def reject_task(request: Request, slug: str, task_id: int):
    """Le commentaire est une consigne : la tâche repart en file avec lui."""
    form = await request.form()
    comment = _clean(form.get("comment"))
    if comment:
        repo.append_feedback(task_id, comment)
    return _respond(request, slug, "tasks", form)


COMMIT_TASK_TITLE = "Commiter les modifications qui traînent"
COMMIT_TASK_BODY = """Le dépôt porte des modifications non commitées, ce qui bloque la \
file : tant que l'arbre n'est pas net, aucune autre tâche ne peut démarrer sans \
travailler par-dessus du travail non relu.

Cette tâche ne fait que **ranger l'existant**. Elle ne modifie pas le code.

- Lire d'abord le diff complet : `git status`, puis `git diff` et `git diff --staged`.
- Regarder `git log` pour reprendre la convention de messages du dépôt.
- Regrouper en **commits cohérents** : un commit par intention, pas un fourre-tout.
- Ne rien annuler, ne rien supprimer, ne rien réécrire. Si quelque chose semble \
inachevé ou cassé, ce n'est pas à toi d'en décider.
- Lancer les tests du projet **avant** de commiter. S'ils échouent, ne commite pas.
- Si les modifications paraissent incohérentes, à moitié faites, ou si tu ne \
comprends pas leur intention : appelle \
`update_task(status='needs_input', blocked_reason="<ce qui te retient>")` et arrête-toi. \
Mieux vaut demander que figer un état douteux dans l'historique."""


@router.post("/p/{slug}/queue-commit")
async def queue_commit_task(request: Request, slug: str):
    """Crée l'agent qui rangera le dépôt — la seule tâche autorisée sur un arbre sale."""
    form = await request.form()
    project = repo.require_project(slug)
    existing = [t for t in repo.list_tasks(project["id"],
                                           status=["queued", "in_progress"], limit=50)
                if t["title"] == COMMIT_TASK_TITLE]
    if not existing:
        repo.create_task(project["id"], title=COMMIT_TASK_TITLE, body=COMMIT_TASK_BODY,
                         priority="urgent", status="queued", owner="claude",
                         tags="git", allow_dirty=True)
    return _respond(request, slug, "tasks", form)


@router.post("/p/{slug}/tasks/{task_id}/answer")
async def answer_task(request: Request, slug: str, task_id: int):
    """Réponse à la question d'un agent : un choix, un texte libre, ou les deux."""
    form = await request.form()
    choice, free = _clean(form.get("choice")), _clean(form.get("comment"))
    answer = " — ".join(part for part in (choice, free) if part)
    if answer:
        repo.answer_question(task_id, answer)
    return _respond(request, slug, "tasks", form)


@router.post("/queue/{action}")
async def queue_control(request: Request, action: str):
    form = await request.form()
    if action == "pause":
        repo.set_setting("queue_paused", "1")
    elif action == "resume":
        repo.set_setting("queue_paused", "0")
    elif action == "stop":
        repo.set_setting("queue_stop", "1")
    slug = _clean(form.get("slug"))
    if slug:
        return _respond(request, slug, "tasks", form)
    return RedirectResponse("/", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int):
    """Le travail d'un agent, raconté pas à pas."""
    with db.cursor() as conn:
        row = conn.execute(
            """SELECT r.*, t.title AS task_title, t.status AS task_status,
                      p.slug AS project_slug, p.name AS project_name
               FROM runs r JOIN tasks t ON t.id = r.task_id
               JOIN projects p ON p.id = r.project_id WHERE r.id = ?""",
            (run_id,)).fetchone()
    if row is None:
        return RedirectResponse("/", status_code=303)

    run = dict(row)
    projet = repo.get_project(run["project_slug"])
    # La branche disparaît à la fusion, mais les commits restent : on compare
    # donc par commits dès qu'on les a, et par branche seulement à défaut.
    # Sans cela, un travail fusionné devenait illisible juste après l'avoir été.
    depart = run.get("commit_before") or run.get("base_branch")
    arrivee = run.get("commit_after") or run.get("branch")
    fichiers, tronque = gitinfo.patch_files((projet or {}).get("path"), depart, arrivee)
    orphelins = gitinfo.orphan_files((projet or {}).get("path"), depart, arrivee)
    parsed = runlog.parse(run.get("log_path"))
    ctx = {"run": run, "steps": parsed["steps"], "meta": parsed["meta"],
           "counts": runlog.counts(parsed["steps"]),
           "live": run["status"] == "running", "project": projet,
           "patch": fichiers, "patch_truncated": tronque, "orphans": orphelins}
    if _is_ajax(request):
        return templates.TemplateResponse(request, "partials/run_steps.html", ctx)
    ctx.update({"projects": repo.list_projects(), "page": "run",
                "page_label": f"Agent · tâche #{run['task_id']}"})
    return templates.TemplateResponse(request, "run.html", ctx)


@router.get("/runs/{run_id}/log", response_class=PlainTextResponse)
def run_log(run_id: int, tail: int = 400):
    """Les dernières lignes du journal d'exécution d'un agent."""
    with db.cursor() as conn:
        run = conn.execute("SELECT log_path FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None or not run["log_path"]:
        return "Aucun journal pour cette exécution."
    path = Path(run["log_path"])
    if not path.exists():
        return f"Journal introuvable : {path}"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(_readable_log(lines[-tail:]))


def _readable_log(lines: list[str]) -> list[str]:
    """Rend lisible le flux d'événements de l'agent.

    En `--output-format stream-json`, chaque ligne est un événement JSON. Brut,
    c'est illisible ; on n'en garde que ce qui raconte le travail : le texte de
    l'agent et les outils qu'il appelle.
    """
    out = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            out.append(line)
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        kind = event.get("type")
        if kind == "system":
            out.append(f"· session démarrée ({event.get('model', 'modèle inconnu')})")
        elif kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    out.append(block["text"].strip())
                elif block.get("type") == "tool_use":
                    detail = block.get("input", {})
                    label = (detail.get("command") or detail.get("file_path")
                             or detail.get("pattern") or detail.get("title") or "")
                    out.append(f"→ {block.get('name')} {str(label)[:120]}")
        elif kind == "result":
            out.append(f"■ terminé en {event.get('duration_ms', 0) // 1000} s "
                       f"({event.get('num_turns', '?')} tours)")
            if event.get("result"):
                out.append(str(event["result"])[:2000])
    return out


@router.post("/p/{slug}/tasks/{task_id}/reorder")
async def move_task(request: Request, slug: str, task_id: int):
    form = await request.form()
    repo.reorder_task(task_id, _int(form.get("after_id")))
    return _respond(request, slug, "tasks")


# --------------------------------------------------------------------------
# Jalons
# --------------------------------------------------------------------------

@router.post("/p/{slug}/milestones/new")
async def new_milestone(request: Request, slug: str):
    form = await request.form()
    name = _clean(form.get("name"))
    if name:
        repo.create_milestone(repo.require_project(slug)["id"], name=name,
                              description=_clean(form.get("description")),
                              target_date=_clean(form.get("target_date")))
    return _respond(request, slug, "milestones")


@router.post("/p/{slug}/milestones/{milestone_id}/update")
async def edit_milestone(request: Request, slug: str, milestone_id: int):
    form = await request.form()
    repo.update_milestone(milestone_id, name=_clean(form.get("name")),
                          description=_clean(form.get("description")),
                          status=_clean(form.get("status")),
                          target_date=_clean(form.get("target_date")))
    return _respond(request, slug, "milestones")


@router.post("/p/{slug}/milestones/{milestone_id}/delete")
def remove_milestone(request: Request, slug: str, milestone_id: int):
    repo.delete_milestone(milestone_id)
    return _respond(request, slug, "milestones")


# --------------------------------------------------------------------------
# Stack
# --------------------------------------------------------------------------

MAX_DOC = 400_000


@router.get("/p/{slug}/doc/{chemin:path}", response_class=HTMLResponse)
def lire_doc(request: Request, slug: str, chemin: str, ref: str = ""):
    """Affiche un fichier de documentation, **lu à la demande dans le dépôt**.

    On ne stocke pas le contenu : il changerait de son côté et le nôtre
    mentirait. On le lit à chaque affichage, ce qui le rend toujours vrai — et
    consultable depuis un téléphone, où un chemin de fichier ne sert à rien.

    Le contrôle du chemin est la seule chose qui compte ici : on résout le
    chemin demandé et on vérifie qu'il tombe **sous la racine du projet**. Sans
    ça, `../../.env` serait lisible par quiconque a le mot de passe.
    """
    projet = repo.require_project(slug)
    racine = Path(projet.get("path") or "").resolve()
    try:
        cible = (racine / chemin).resolve()
        cible.relative_to(racine)
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="chemin hors du projet")
    if cible.suffix.lower() != ".md":
        raise HTTPException(status_code=404, detail="documentation uniquement (.md)")

    # **La documentation dépend de la branche.** Une branche d'agent peut
    # réécrire un document, en ajouter un, ou n'en avoir aucun. Lire le disque
    # revenait à ne montrer que la branche sortie dans la copie principale, sans
    # même le dire. On lit donc par `git show <ref>:<chemin>`, ce qui donne
    # n'importe quelle branche sans toucher à la copie de travail de personne.
    branches = gitinfo.branches(str(racine))
    reference = ref if ref in branches else (branches[0] if branches else "")
    contenu, commit = gitinfo.read_blob(str(racine), reference, chemin, MAX_DOC) \
        if reference else (None, "")

    # Hors dépôt git, ou fichier pas encore commité : on retombe sur le disque,
    # en le disant. Un document ajouté à l'instant vaut mieux qu'une page vide.
    depuis_disque = False
    if contenu is None:
        if not cible.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"« {chemin} » n'existe pas sur « {reference or 'le disque'} »")
        contenu = cible.read_text(encoding="utf-8", errors="replace")[:MAX_DOC]
        depuis_disque = True

    fiche = next((d for d in repo.list_docs(projet["id"]) if d["path"] == chemin), {})
    return templates.TemplateResponse(request, "doc.html", {
        "page": "projects", "project": projet, "projects": repo.list_projects(),
        "chemin": chemin, "contenu": contenu, "fiche": fiche,
        "branches": branches, "reference": reference, "commit": commit,
        "depuis_disque": depuis_disque,
        "tronque": len(contenu) >= MAX_DOC, "taille": len(contenu),
        # On ne propose l'édition que sur la branche sortie : écrire ailleurs
        # demanderait une copie de travail, et personne n'attend ça d'un bouton.
        "modifiable": bool(branches) and reference == branches[0],
        "demande": repo.doc_edit_state(projet["id"], chemin),
    })


@router.post("/p/{slug}/doc/{chemin:path}")
async def modifier_doc(request: Request, slug: str, chemin: str):
    """Dépose la modification ; c'est le démon qui écrira et commitera.

    Le conteneur monte les dépôts en lecture seule — délibérément. Écrire d'ici
    créerait en plus des fichiers appartenant à root, inéditables ensuite par
    `dev`. La demande transite donc par la base et le démon l'applique.
    """
    form = await request.form()
    projet = repo.require_project(slug)
    racine = Path(projet.get("path") or "").resolve()
    try:
        (racine / chemin).resolve().relative_to(racine)
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="chemin hors du projet")
    if not chemin.lower().endswith(".md"):
        raise HTTPException(status_code=400, detail="documentation uniquement (.md)")

    contenu = str(form.get("contenu") or "")
    branches = gitinfo.branches(str(racine))
    repo.request_doc_edit(projet["id"], path=chemin, content=contenu,
                          branch=branches[0] if branches else None)
    return RedirectResponse(f"/p/{slug}/doc/{chemin}", status_code=303)


@router.post("/p/{slug}/docs/scan")
async def scan_docs(request: Request, slug: str):
    """Parcourt le dépôt et inscrit les markdown trouvés, sans rien écraser."""
    form = await request.form()
    projet = repo.require_project(slug)
    repo.scan_docs(projet["id"], root=projet.get("path") or "")
    return _respond(request, slug, "profile", form)


@router.post("/p/{slug}/docs/set")
async def set_doc(request: Request, slug: str):
    form = await request.form()
    chemin = _clean(form.get("path"))
    if chemin:
        repo.set_doc(repo.require_project(slug)["id"], path=chemin,
                     covers=_clean(form.get("covers")))
    return _respond(request, slug, "profile", form)


@router.post("/p/{slug}/docs/{doc_id}/delete")
async def remove_doc(request: Request, slug: str, doc_id: int):
    form = await request.form()
    repo.delete_doc(doc_id)
    return _respond(request, slug, "profile", form)


@router.post("/p/{slug}/method/set")
async def set_practice(request: Request, slug: str):
    form = await request.form()
    titre = _clean(form.get("title"))
    if titre:
        repo.set_practice(repo.require_project(slug)["id"], title=titre,
                          body=_clean(form.get("body")),
                          category=_clean(form.get("category")))
    return _respond(request, slug, "method", form)


@router.post("/p/{slug}/method/{practice_id}/delete")
async def remove_practice(request: Request, slug: str, practice_id: int):
    form = await request.form()
    repo.delete_practice(practice_id)
    return _respond(request, slug, "method", form)


@router.post("/p/{slug}/stack/set")
async def set_tech(request: Request, slug: str):
    form = await request.form()
    name = _clean(form.get("name"))
    if name:
        repo.set_technology(
            repo.require_project(slug)["id"], name=name,
            category=_clean(form.get("category")), version=_clean(form.get("version")),
            role=_clean(form.get("role")), status=_clean(form.get("status")),
            notes=_clean(form.get("notes")), docs_url=_clean(form.get("docs_url")))
    return _respond(request, slug, "stack")


@router.post("/p/{slug}/stack/remove")
async def remove_tech(request: Request, slug: str):
    form = await request.form()
    name = _clean(form.get("name"))
    if name:
        repo.remove_technology(repo.require_project(slug)["id"], name)
    return _respond(request, slug, "stack")


@router.post("/p/{slug}/stack/scan")
def scan_project(request: Request, slug: str):
    project = repo.require_project(slug)
    try:
        _apply_scan(project)
    except Exception as exc:
        repo.log_work(project["id"], summary=f"Scan de la stack en échec : {exc}",
                      kind="note", actor="user")
    return _respond(request, slug, "stack")


def _apply_scan(project: dict) -> dict:
    """Applique un scan de stack. Partagé entre l'interface web et l'outil MCP."""
    found = scanner.scan(project["path"])
    pid = project["id"]
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
        if found.get("repo_url") and not project.get("repo_url"):
            repo.upsert_project(project["slug"], repo_url=found["repo_url"], conn=conn)
        repo.log_work(pid, kind="note", actor="user",
                      summary=f"Scan de la stack : {len(found['technologies'])} technologies, "
                              f"{len(found['commands'])} commandes",
                      detail=", ".join(found["scanned"]), conn=conn)
    return found


@router.get("/reglages", response_class=HTMLResponse)
def reglages_page(request: Request, message: str = "", ok: str = ""):
    return templates.TemplateResponse(request, "reglages.html", {
        "page": "reglages", "page_label": "Notifications",
        "projects": repo.list_projects(),
        "chat_id": notify.chat_id(),
        "token_present": bool(notify.token()),
        "evenements": notify.etat(),
        "webhook": notify.etat_webhook(),
        "webhook_url": notify.url_webhook(),
        # Le verdict de l'essai revient par l'URL après une redirection : la page
        # doit rester utilisable sans JavaScript, donc pas de réponse en place.
        "message": message,
        "message_ok": ok == "1",
    })


@router.post("/reglages/set")
async def set_reglages(request: Request):
    form = await request.form()
    repo.set_setting(notify.CLE_CHAT, _clean(form.get("chat_id")) or "")
    # Une case décochée n'est **pas** envoyée par le navigateur : on écrit donc
    # tous les événements à chaque fois, à partir de ceux qui sont revenus.
    coches = set(form.getlist("event"))
    for cle in notify.EVENEMENTS:
        repo.set_setting(notify.PREFIXE + cle, "1" if cle in coches else "0")
    return RedirectResponse(
        "/reglages?" + urlencode({"message": "Réglages enregistrés.", "ok": "1"}),
        status_code=303)


@router.post("/reglages/webhook")
def branche_webhook():
    """Déclare à Telegram où nous joindre, pour pouvoir répondre depuis le tchat."""
    reussi, detail = notify.branche_webhook()
    return RedirectResponse(
        "/reglages?" + urlencode({"message": detail, "ok": "1" if reussi else "0"}),
        status_code=303)


@router.post("/reglages/test")
def test_reglages():
    """Envoi synchrone : un bouton d'essai qui ne dit rien ne sert à rien."""
    reussi, detail = notify.envoie(
        "🧭 claude-manager — message d'essai.\n"
        "Si tu lis ceci, les notifications sont en place.")
    return RedirectResponse(
        "/reglages?" + urlencode({"message": detail, "ok": "1" if reussi else "0"}),
        status_code=303)


@router.post("/preferences/set")
async def set_preference(request: Request):
    form = await request.form()
    name = _clean(form.get("name"))
    if name:
        repo.set_preference(name=name, preference=_clean(form.get("preference")) or "",
                            level=_clean(form.get("level")) or "preferred",
                            category=_clean(form.get("category")))
    slug = _clean(form.get("slug"))
    return _respond(request, slug, "stack") if slug \
        else RedirectResponse("/preferences", status_code=303)


# --------------------------------------------------------------------------
# Tous les rappels
# --------------------------------------------------------------------------

def _rappels_context() -> dict:
    groupes = {cle: [] for cle, _ in rappels.GROUPES}
    for r in repo.list_reminders():
        groupes[rappels.groupe(r["remind_at"])].append(r)
    return {"groupes": [(cle, label, groupes[cle]) for cle, label in rappels.GROUPES],
            "total": sum(len(v) for v in groupes.values()),
            "all_projects": repo.list_projects()}


def _rappels_respond(request: Request):
    if _is_ajax(request):
        return templates.TemplateResponse(request, "partials/all_reminders.html", _rappels_context())
    return RedirectResponse("/rappels", status_code=303)


@router.get("/rappels", response_class=HTMLResponse)
def reminders_page(request: Request):
    if _is_ajax(request):
        return templates.TemplateResponse(request, "partials/all_reminders.html", _rappels_context())
    ctx = _rappels_context()
    ctx.update({"page": "rappels", "page_label": "Rappels", "projects": ctx["all_projects"]})
    return templates.TemplateResponse(request, "reminders.html", ctx)


@router.post("/rappels/new")
async def reminders_new(request: Request):
    form = await request.form()
    projet = repo.get_project(_clean(form.get("project")))
    quand = _quand(form)
    if projet and quand:
        repo.add_reminder(projet["id"], quand, _clean(form.get("note")))
    return _rappels_respond(request)


@router.post("/rappels/{reminder_id}/snooze")
async def reminders_snooze(request: Request, reminder_id: int):
    form = await request.form()
    quand = _quand(form)
    if quand:
        repo.update_reminder(reminder_id, remind_at=quand)
    return _rappels_respond(request)


@router.post("/rappels/{reminder_id}/delete")
async def reminders_delete(request: Request, reminder_id: int):
    repo.delete_reminder(reminder_id)
    return _rappels_respond(request)


@router.get("/preferences", response_class=HTMLResponse)
def preferences_page(request: Request):
    technologies = repo.list_technologies()
    return templates.TemplateResponse(request, "preferences.html", {
        "page": "preferences", "page_label": "Préférences techniques",
        "projects": repo.list_projects(),
        "preferences": repo.list_preferences(),
        "technologies": technologies,
        "usage": {t["name"]: repo.projects_using(t["name"]) for t in technologies},
        "tech_categories": config.TECH_CATEGORIES,
    })


# --------------------------------------------------------------------------
# Fiche projet
# --------------------------------------------------------------------------

@router.post("/p/{slug}/profile/{kind}")
async def set_profile_item(request: Request, slug: str, kind: str):
    form = await request.form()
    pid = repo.require_project(slug)["id"]
    name = _clean(form.get("name")) or _clean(form.get("title"))
    if name:
        if kind == "command":
            repo.set_command(pid, name=name, command=_clean(form.get("command")),
                             workdir=_clean(form.get("workdir")),
                             description=_clean(form.get("description")))
        elif kind == "service":
            repo.set_service(pid, name=name, kind=_clean(form.get("kind")) or "url",
                             url=_clean(form.get("url")), port=_int(form.get("port")),
                             container=_clean(form.get("container")),
                             environment=_clean(form.get("environment")) or "prod",
                             notes=_clean(form.get("notes")))
        elif kind == "env_var":
            repo.set_env_var(pid, name=name, required=bool(_clean(form.get("required"))),
                             secret=bool(_clean(form.get("secret"))),
                             location=_clean(form.get("location")),
                             description=_clean(form.get("description")),
                             example=_clean(form.get("example")))
        elif kind == "resource":
            url = _clean(form.get("url"))
            if url:
                repo.add_resource(pid, title=name, url=url,
                                  kind=_clean(form.get("kind")) or "other",
                                  notes=_clean(form.get("notes")))
    return _respond(request, slug, "profile")


@router.post("/p/{slug}/profile/{kind}/{item_id}/delete")
def remove_profile_item(request: Request, slug: str, kind: str, item_id: int):
    repo.delete_profile_item(kind, item_id)
    return _respond(request, slug, "profile")


# --------------------------------------------------------------------------
# Mémoire et journal
# --------------------------------------------------------------------------

@router.post("/p/{slug}/memory/new")
async def new_memory(request: Request, slug: str):
    form = await request.form()
    title, body = _clean(form.get("title")), _clean(form.get("body"))
    if title and body:
        repo.add_memory(repo.require_project(slug)["id"], title=title, body=body,
                        kind=_clean(form.get("kind")) or "note",
                        tags=_clean(form.get("tags")),
                        pinned=bool(_clean(form.get("pinned"))))
    return _respond(request, slug, "memory")


@router.post("/p/{slug}/memory/{memory_id}/update")
async def edit_memory(request: Request, slug: str, memory_id: int):
    form = await request.form()
    repo.update_memory(memory_id, title=_clean(form.get("title")),
                       body=_clean(form.get("body")), kind=_clean(form.get("kind")),
                       tags=form.get("tags") if form.get("tags") is not None else None,
                       pinned=bool(_clean(form.get("pinned"))))
    return _respond(request, slug, "memory")


@router.post("/p/{slug}/memory/{memory_id}/delete")
def remove_memory(request: Request, slug: str, memory_id: int):
    repo.delete_memory(memory_id)
    return _respond(request, slug, "memory")


@router.post("/p/{slug}/journal/new")
async def new_journal(request: Request, slug: str):
    form = await request.form()
    summary = _clean(form.get("summary"))
    if summary:
        repo.log_work(repo.require_project(slug)["id"], summary=summary,
                      detail=_clean(form.get("detail")),
                      kind=_clean(form.get("kind")) or "note", actor="user")
    return _respond(request, slug, "journal")


# --------------------------------------------------------------------------
# Recherche
# --------------------------------------------------------------------------

@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", project: str = ""):
    project_row = repo.get_project(project) if project else None
    results = repo.search(q, project_id=project_row["id"] if project_row else None) if q else []
    return templates.TemplateResponse(request, "search.html", {
        "page": "search", "page_label": "Recherche",
        "projects": repo.list_projects(), "q": q, "results": results,
        "selected": project,
    })
