"""Point d'entrée : une seule application servant l'interface web et le MCP."""
import asyncio
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse, PlainTextResponse

from . import __version__, briefing as briefing_mod, config, db, notify, rappels, repo, runlog
from .auth import AuthMiddleware
from .mcp_server import mcp
from .support_mcp import support_mcp
from .web.routes import router as web_router
from .web.support_routes import router as support_router

log = logging.getLogger("claude-manager")

# Construit avant le montage : c'est cet appel qui instancie le gestionnaire de
# sessions référencé plus bas dans le lifespan.
mcp_app = mcp.streamable_http_app()
support_mcp_app = support_mcp.streamable_http_app()


RAPPELS_SECONDES = 30


def envoie_rappels_echus() -> int:
    """Envoie les rappels arrivés à échéance. Un envoi raté est retenté au tour
    suivant ; un envoi réussi (ou sans objet) n'est jamais refait."""
    envoyes = 0
    for rappel in repo.due_reminders(rappels.en_utc(rappels.maintenant())):
        if notify.rappelle(rappel):
            repo.mark_reminded(rappel["id"])
            envoyes += 1
    return envoyes


async def _boucle_rappels() -> None:
    """Une minuterie dans le serveur, pas dans le démon de la file : le démon
    peut être arrêté, un rappel doit partir quand même."""
    while True:
        try:
            await asyncio.to_thread(envoie_rappels_echus)
        except Exception:  # noqa: BLE001 — la boucle ne doit jamais mourir
            log.exception("rappels : tour en échec")
        await asyncio.sleep(RAPPELS_SECONDES)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    boucle = asyncio.create_task(_boucle_rappels())
    try:
        async with mcp.session_manager.run(), support_mcp.session_manager.run():
            yield
    finally:
        boucle.cancel()


app = FastAPI(title="claude-manager", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(AuthMiddleware)
app.mount("/mcp", mcp_app)
app.mount("/mcp-support", support_mcp_app)
app.mount("/static", StaticFiles(directory=config.BASE_DIR / "web" / "static"), name="static")
app.include_router(web_router)
app.include_router(support_router)


@app.get("/api/briefing", response_class=PlainTextResponse)
def api_briefing(path: str = "", project: str = ""):
    """Briefing en texte brut, pour le hook SessionStart de Claude Code.

    Le hook ne connaît que son répertoire de travail : on le résout ici en
    projet, en retenant le chemin enregistré le plus spécifique.
    """
    if not project:
        resolved = repo.resolve_project_by_path(path)
        if resolved is None:
            return PlainTextResponse(
                briefing_mod.empty_briefing(path, repo.list_projects()), status_code=404)
        project = resolved["slug"]
    return briefing_mod.to_markdown(briefing_mod.build(project))


@app.get("/api/activity")
def api_activity(path: str = "", project: str = "", since: str = ""):
    """Écritures enregistrées sur un projet depuis `since`, pour le hook Stop."""
    row = repo.get_project(project) if project else repo.resolve_project_by_path(path)
    if row is None:
        return JSONResponse({"found": False}, status_code=404)
    counts = repo.activity_since(row["id"], since or "")
    open_tasks = repo.list_tasks(row["id"], limit=200)
    return {
        "found": True,
        "project": row["slug"],
        "since": since,
        "activity": counts,
        "in_progress": [{"id": t["id"], "title": t["title"]}
                        for t in open_tasks if t["status"] == "in_progress"],
    }


CLE_ORPHELINES = "notify.orphelines_annoncees"


def _previens_orphelines() -> None:
    """Alerte une fois par tâche restée « en cours » sans que rien ne la porte.

    Accrochée à l'état de la file plutôt qu'à une minuterie : le démon interroge
    cette route toutes les dix secondes, ce qui donne une vérification régulière
    sans rien ajouter côté hôte — donc sans redémarrage du démon.

    On retient l'`updated_at` annoncé, pas seulement le numéro : une tâche
    reprise puis relaissée doit alerter de nouveau, alors qu'une tâche immobile
    ne doit alerter qu'une fois. Sans cette nuance, dix secondes de sondage
    deviendraient un message toutes les dix secondes.
    """
    try:
        orphelines = repo.orphan_tasks()
        connues = json.loads(repo.get_setting(CLE_ORPHELINES, "{}") or "{}")
    except (ValueError, TypeError):
        connues = {}
    except Exception:  # noqa: BLE001 — prévenir ne doit rien casser en amont
        log.exception("détection des tâches orphelines impossible")
        return

    vues = {}
    for tache in orphelines:
        cle, marque = str(tache["id"]), tache.get("updated_at") or ""
        vues[cle] = marque
        if connues.get(cle) == marque:
            continue
        heures = tache["idle_minutes"] // 60
        depuis = f"{heures} h" if heures else f"{tache['idle_minutes']} min"
        notify.previens(
            "task_orphan",
            f"🕸 Tâche en cours que plus rien ne porte — {tache.get('project_name', '?')}",
            [f"#{tache['id']} {tache.get('title', '')}",
             f"sans activité depuis {depuis}, aucune exécution en cours",
             "La file ne la réclamera pas : elle ne prend que les tâches en file."],
            f"{config.PUBLIC_URL.rstrip('/')}/",
        )
    # On n'écrit qu'au changement : cette route est appelée toutes les dix
    # secondes, et une écriture par sondage userait la base pour rien.
    if vues != connues:
        repo.set_setting(CLE_ORPHELINES, json.dumps(vues))


@app.get("/api/queue/state")
def api_queue_state():
    """Vue complète de la file, pour le démon côté hôte."""
    _previens_orphelines()
    return {
        "paused": repo.get_setting("queue_paused", "0") == "1",
        "stop_requested": repo.get_setting("queue_stop", "0") == "1",
        "running": repo.running_run(),
        "running_all": repo.running_runs(),
        "max_parallel": config.MAX_PARALLEL,
        "pending": repo.queue_pending(),
        "agent_timeout": config.AGENT_TIMEOUT_SECONDS,
    }


@app.post("/api/queue/claim")
def api_queue_claim(payload: dict = Body(...)):
    run = repo.claim_task(int(payload["task_id"]),
                          commit_before=payload.get("commit_before"),
                          log_path=payload.get("log_path"),
                          group_id=payload.get("group_id"),
                          allow_parallel=bool(payload.get("allow_parallel")),
                          branch=payload.get("branch"),
                          worktree=payload.get("worktree"))
    if run is None:
        # Un agent a démarré entre-temps, ou la tâche a quitté la file.
        return JSONResponse({"claimed": False}, status_code=409)
    task = repo.get_task(run["task_id"])
    project = repo.get_project(task["project_id"])
    # Les services voyagent avec : c'est là qu'est le gabarit d'URL de l'aperçu.
    project["services"] = repo.list_services(project["id"])
    # Les erreurs déjà commises sur ce projet partent avec la tâche : elles
    # entrent dans le prompt même si le hook SessionStart ne répond pas.
    erreurs = repo.list_memories(project["id"], kind="erreur", limit=20)
    return {"claimed": True, "run": run, "task": task, "project": project,
            "commands": repo.list_commands(project["id"]), "erreurs": erreurs}


@app.post("/api/runs/{run_id}/finish")
def api_run_finish(run_id: int, payload: dict = Body(...)):
    # Les champs sont recopiés depuis la charge utile, et la couche données
    # décide lesquels elle accepte. Les énumérer deux fois — ici et là-bas —
    # avait fait perdre `branch` en route.
    champs = {key: payload.get(key) for key in
              ("commit_after", "diff_stat", "tests_command", "tests_ok", "tests_output",
               "summary", "exit_code", "log_path", "foreign_files", "collision",
               "branch", "base_branch", "worktree", "preview_url", "preview_port",
               "lesson_note")}
    run = repo.finish_run(run_id, status=payload.get("status", "error"),
                          task_status=payload.get("task_status", "review"), **champs)
    task = repo.get_task(run["task_id"])
    _previens_fin(run, task)
    return {"run": run, "task": task}


# Une exécution qui se termine, traduite en événement de notification. `passed`
# veut dire « tests verts, à relire » : c'est le seul cas où quelque chose attend
# vraiment Kevin plutôt que de lui apprendre un échec.
FIN_EN_EVENEMENT = {"passed": "run_passed", "failed": "run_failed"}
TITRE_DE_FIN = {
    "run_passed": "✅ Tests verts — à relire",
    "run_failed": "❌ Tests en échec",
    "run_error": "⚠️ Exécution interrompue",
}


def _previens_fin(run: dict, task: dict) -> None:
    """Prévient de la fin d'une exécution. N'interrompt jamais la requête."""
    evenement = FIN_EN_EVENEMENT.get(run.get("status"), "run_error")
    projet = repo.get_project(run.get("project_id")) or {}
    lignes = [f"#{task['id']} {task.get('title', '')}"]
    # Ce que l'agent dit avoir fait, pas seulement qu'il a fini. Sans ça il faut
    # ouvrir l'interface pour savoir si ça vaut le déplacement.
    resume = runlog.resume(run.get("log_path"))
    if resume:
        lignes += ["", resume, ""]
    if run.get("branch"):
        lignes.append(f"branche : {run['branch']}")
    if run.get("preview_url"):
        lignes.append(f"essai : {run['preview_url']}")
    notify.previens(
        evenement,
        f"{TITRE_DE_FIN.get(evenement, evenement)} — {projet.get('name', '?')}",
        lignes,
        notify.lien_run(run["id"]),
    )


@app.get("/api/support/pending")
def api_support_pending():
    """Discussions de signalement qui attendent la réponse de l'IA (démon).

    Chacune porte son jeton d'accès au MCP support, borné à ce signalement."""
    from .support_mcp import jeton
    tickets = repo.pending_ai()
    for t in tickets:
        t["mcp_token"] = jeton(t["id"])
    return {"tickets": tickets}


@app.post("/api/support/{ticket_id}/reply")
def api_support_reply(ticket_id: int, payload: dict = Body(...)):
    repo.save_ai_reply(ticket_id, payload.get("content"), draft=payload.get("draft"),
                       error=payload.get("error"), session=payload.get("session"))
    return {"ok": True}


@app.post("/api/support/{ticket_id}/progress")
def api_support_progress(ticket_id: int, payload: dict = Body(...)):
    repo.set_ai_progress(ticket_id, payload.get("text"))
    return {"ok": True}


@app.post("/api/support/{ticket_id}/warm")
def api_support_warm(ticket_id: int, payload: dict = Body(...)):
    repo.save_warmup(ticket_id, payload.get("session"), bool(payload.get("ok")))
    return {"ok": True}


@app.get("/api/lessons/pending")
def api_lessons_pending():
    """Exécutions ratées dont il reste à tirer la leçon (voir repo._flag_lesson)."""
    return {"lessons": repo.pending_lessons()}


@app.post("/api/lessons/{run_id}/result")
def api_lesson_result(run_id: int, payload: dict = Body(...)):
    repo.set_lesson_result(run_id, payload.get("state", "done"))
    return {"ok": True}


@app.get("/api/doc-edits/pending")
def api_doc_edits_pending():
    """Modifications de documentation à appliquer côté hôte.

    Le conteneur ne peut pas écrire dans les dépôts : c'est le démon, qui tourne
    en `dev`, qui pose le fichier et commite.
    """
    return {"edits": repo.pending_doc_edits()}


@app.post("/api/doc-edits/{edit_id}/result")
def api_doc_edit_result(edit_id: int, payload: dict = Body(...)):
    return repo.set_doc_edit_result(edit_id, payload.get("state", "failed"),
                                    payload.get("detail"), payload.get("commit"))


@app.get("/api/worktrees")
def api_worktrees():
    """Worktrees vivants et état de leur tâche, pour le ménage du démon.

    Les commandes du projet voyagent avec, sinon le démon devrait les redemander
    projet par projet pour savoir comment arrêter un aperçu.
    """
    items = repo.live_worktrees()
    commandes: dict[str, list] = {}
    for item in items:
        slug = item["project_slug"]
        if slug not in commandes:
            projet = repo.get_project(slug)
            commandes[slug] = repo.list_commands(projet["id"]) if projet else []
        item["commands"] = commandes[slug]
    return {"worktrees": items, "used_ports": repo.used_ports()}


@app.post("/api/worktrees/{run_id}/forget")
def api_worktree_forget(run_id: int):
    repo.forget_worktree(run_id)
    return {"ok": True}


@app.get("/api/merges/pending")
def api_merges_pending():
    """Fusions demandées depuis l'interface, à exécuter côté hôte.

    Les commandes du projet partent avec : le démon doit pouvoir relancer les
    tests **sur le résultat de la fusion**, pas seulement dans le worktree de
    l'agent. Un commit partiel passe au vert dans le worktree et casse la base.
    """
    merges = repo.pending_merges()
    for demande in merges:
        demande["commands"] = repo.list_commands(demande["project_id"])
    return {"merges": merges}


@app.post("/api/merges/{run_id}/result")
def api_merge_result(run_id: int, payload: dict = Body(...)):
    etat = payload.get("state", "conflict")
    run = repo.set_merge_result(run_id, etat, payload.get("detail"))
    if etat in ("reverted", "broken"):
        # Le cas qui doit réveiller quelqu'un : la branche a fusionné, la base
        # ne tient plus. Défaite ou non, personne ne doit l'apprendre demain.
        task = repo.get_task(run["task_id"]) if run.get("task_id") else {}
        projet = repo.get_project(run.get("project_id")) or {}
        titre = ("↩️ Fusion défaite — tests rouges sur la base"
                 if etat == "reverted" else
                 "🔥 Base rouge après fusion, NON défaite")
        notify.previens(
            "merge_broken", f"{titre} — {projet.get('name', '?')}",
            [f"#{task.get('id')} {task.get('title', '')}",
             f"branche conservée : {run.get('branch') or '?'}"],
            notify.lien_run(run_id))
    return run


@app.post("/api/queue/ack-stop")
def api_queue_ack_stop():
    """Le démon accuse réception de la demande d'arrêt et la remet à zéro."""
    repo.set_setting("queue_stop", "0")
    return {"ok": True}


@app.post("/telegram/{jeton}")
async def telegram_webhook(jeton: str, request: Request):
    """Reçoit les réponses de Kevin depuis Telegram.

    **Trois verrous, dans cet ordre.** Le chemin porte un fragment du secret,
    l'en-tête convenu avec Telegram est comparé en temps constant, et seule la
    conversation enregistrée est écoutée. Cette route est la seule ouverte sans
    cookie : elle doit se défendre seule.

    On répond toujours 200. Un code d'erreur ferait retenter Telegram en boucle
    pour un message qu'on ne traitera jamais mieux la deuxième fois.
    """
    attendu = notify.secret_webhook()
    entete = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not (hmac.compare_digest(jeton, attendu[:12])
            and hmac.compare_digest(entete, attendu)):
        log.warning("webhook telegram : jeton invalide")
        return JSONResponse({"ok": True})
    try:
        update = await request.json()
    except (ValueError, TypeError):
        return JSONResponse({"ok": True})
    try:
        _traite_telegram(update)
    except Exception:  # noqa: BLE001 — ne jamais faire retenter Telegram
        log.exception("webhook telegram : traitement impossible")
    return JSONResponse({"ok": True})


def _traite_telegram(update: dict) -> None:
    """Un appui sur bouton, ou une réponse écrite."""
    attendu = notify.chat_id()

    rappel = update.get("callback_query")
    if rappel:
        chat = str(((rappel.get("message") or {}).get("chat") or {}).get("id", ""))
        if chat != attendu:
            return
        morceaux = str(rappel.get("data", "")).split(":")
        if len(morceaux) == 3 and morceaux[0] == "rp":
            _bouton_rappel(rappel.get("id"), int(morceaux[1]), morceaux[2])
            return
        notify.appel("answerCallbackQuery", {"callback_query_id": rappel.get("id")})
        if len(morceaux) != 3 or morceaux[0] != "r":
            return
        task_id, index = int(morceaux[1]), int(morceaux[2])
        suivi = notify.question_du_message((rappel.get("message") or {}).get("message_id"))
        options = (suivi or {}).get("options") or []
        reponse = options[index] if index < len(options) else f"option {index + 1}"
        _enregistre_reponse(task_id, reponse)
        return

    message = update.get("message") or {}
    if str((message.get("chat") or {}).get("id", "")) != attendu:
        return
    texte = (message.get("text") or "").strip()
    if not texte or texte.startswith("/"):
        return

    # Trois façons de savoir à quoi Kevin répond, de la plus sûre à la plus
    # faible. On ne devine jamais : deux questions ouvertes et une réponse
    # attribuée au hasard, c'est une consigne donnée à la mauvaise tâche.
    suivi = notify.question_du_message((message.get("reply_to_message") or {}).get("message_id"))
    task_id = (suivi or {}).get("task")
    if task_id is None:
        prefixe = re.match(r"^#(\d+)\s+(.*)$", texte, re.S)
        if prefixe:
            task_id, texte = int(prefixe.group(1)), prefixe.group(2).strip()
    if task_id is None:
        ouvertes = repo.tasks_awaiting_answer()
        if len(ouvertes) == 1:
            task_id = ouvertes[0]["id"]
        else:
            liste = "\n".join(f"#{t['id']} {t['title'][:50]}" for t in ouvertes[:5])
            notify.previens("needs_input", "À quelle question réponds-tu ?",
                            [liste or "Aucune question en attente.",
                             "", "Réponds au message de la question, "
                             "ou commence par son numéro : « #42 ta réponse »."])
            return
    _enregistre_reponse(task_id, texte)


def _bouton_rappel(callback_id, reminder_id: int, action: str) -> None:
    """Les trois boutons d'un rappel : repousser d'une heure, à demain matin,
    ou le retirer. La confirmation s'affiche en bulle, sans nouveau message."""
    try:
        rappel = repo.get_reminder(reminder_id)
        sujet = f"#{rappel['task_id']}" if rappel.get("task_id") else rappel["project_name"]
        if action == "ok":
            repo.delete_reminder(reminder_id)
            texte = f"Rappel retiré ✓ ({sujet})"
        else:
            quand = rappels.en_utc(rappels.interprete("+1h" if action == "1h" else "demain"))
            repo.update_reminder(reminder_id, remind_at=quand)
            texte = f"{sujet} : rappel {rappels.libelle(quand)}"
    except repo.NotFound:
        texte = "Ce rappel n'existe plus."
    notify.appel("answerCallbackQuery", {"callback_query_id": callback_id, "text": texte})


def _enregistre_reponse(task_id: int, reponse: str) -> None:
    """Applique la réponse et en accuse réception, dans les deux sens."""
    try:
        task = repo.get_task(task_id)
    except repo.NotFound:
        notify.envoie(f"Tâche #{task_id} introuvable.")
        return
    if task.get("status") != "needs_input":
        notify.envoie(f"#{task_id} n'attend plus de réponse "
                      f"(elle est « {task.get('status')} »). Rien changé.")
        return
    repo.answer_question(task_id, reponse)
    projet = repo.get_project(task.get("project_id")) or {}
    repo.log_work(task["project_id"], task_id=task_id, kind="work", actor="user",
                  summary=f"Réponse de Kevin depuis Telegram sur la tâche #{task_id}.",
                  detail=reponse)
    notify.envoie(f"✅ Noté sur #{task_id} — {task.get('title', '')}\n"
                  f"{notify.abrege(reponse, 200)}\n\n"
                  f"La tâche repart en file sur {projet.get('name', '?')}.")


@app.exception_handler(repo.NotFound)
async def _introuvable(request: Request, erreur: repo.NotFound):
    """Une entité absente est une 404, pas une panne.

    Sans ce raccord, demander un projet qui n'existe pas remontait une trace
    d'exception et un 500 — vu en éprouvant le lecteur de documentation, où un
    chemin normalisé par le navigateur retombe sur un slug inventé.
    """
    if request.url.path.startswith(("/api", "/mcp")):
        return JSONResponse({"error": str(erreur)}, status_code=404)
    return PlainTextResponse(str(erreur), status_code=404)


@app.get("/health")
def health():
    try:
        projects = len(repo.list_projects())
        return {"status": "ok", "version": __version__, "projects": projects}
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)
