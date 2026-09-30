"""Signalements : l'espace des utilisateurs d'agence, et sa relecture par Kevin.

Deux mondes qui ne se touchent pas :

- `/support/…` — le signaleur, avec sa propre session (auth.REPORTER_COOKIE).
  Il discute avec une IA qui l'aide à décrire son problème, puis envoie le
  ticket proposé. C'est tout : il ne voit que ses tickets, jamais le reste.
- `/signalements/…` — Kevin, derrière la session administrateur habituelle.
  Il crée les comptes, rédige la fiche support des projets, et décide : un
  signalement ne devient une tâche que par `repo.accept_ticket`, appelé d'ici.

L'IA elle-même tourne côté hôte (le démon lance `claude -p`, sans aucun outil) :
voir `worker/agent_worker.py`, `process_support`.
"""
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import auth, captures, config, notify, repo
from .routes import _clean, _int, _is_ajax, templates

router = APIRouter()

MESSAGE_MAX = 4000
MESSAGES_PAR_TICKET = 40

# Libellés vus par le signaleur. Il ne voit jamais le vocabulaire interne
# (file, agent, branche) : seulement où en est SON problème.
def statut_signaleur(t: dict) -> tuple[str, str]:
    if t["status"] == "draft":
        return "brouillon", "Brouillon — pas encore envoyé"
    if t["status"] == "submitted":
        return "recu", "Reçu — en attente de validation"
    if t["status"] == "rejected":
        return "refuse", "Refusé"
    task = t.get("task_status")
    if task == "done":
        return "corrige", "Corrigé"
    if task == "cancelled":
        return "refuse", "Abandonné"
    if task in ("in_progress", "review", "needs_input", "blocked"):
        return "en-cours", "En cours de correction"
    return "valide", "Validé — correction prévue"


templates.env.globals["statut_signaleur"] = statut_signaleur
templates.env.globals["signalements_en_attente"] = lambda: repo.count_submitted_tickets()
templates.env.globals["public_url"] = config.PUBLIC_URL.rstrip("/")
templates.env.globals["est_image"] = captures.est_image
templates.env.globals["ticket_checks"] = repo.ticket_checks
templates.env.globals["taille_lisible"] = captures.taille_lisible
templates.env.globals["pieces_acceptees"] = captures.ACCEPTE


# --------------------------------------------------------------------------
# Espace signaleur
# --------------------------------------------------------------------------

# Échecs de connexion récents, par identifiant : cinq essais, puis un quart
# d'heure d'attente. En mémoire suffit — un redémarrage remet à zéro, sans
# conséquence pour un service de cette taille.
_echecs: dict[str, list[float]] = {}
ECHECS_MAX, ECHECS_FENETRE = 5, 15 * 60


def _bloque(cle: str) -> bool:
    recents = [t for t in _echecs.get(cle, []) if time.time() - t < ECHECS_FENETRE]
    _echecs[cle] = recents
    return len(recents) >= ECHECS_MAX


def _signaleur(request: Request) -> dict:
    """Le signaleur connecté, ou une redirection vers la connexion."""
    rid = auth.reporter_id(request)
    reporter = repo.get_reporter(rid) if rid else None
    if not reporter or not reporter["active"]:
        raise HTTPException(status_code=303, headers={"Location": "/support/login"})
    return reporter


def _son_ticket(reporter: dict, ticket_id: int) -> dict:
    """Un signaleur ne voit que SES tickets : un numéro deviné répond 404."""
    ticket = repo.get_ticket(ticket_id)
    if not ticket or ticket["reporter_id"] != reporter["id"]:
        raise HTTPException(status_code=404, detail="signalement introuvable")
    return ticket


@router.get("/support/login", response_class=HTMLResponse)
def support_login_form(request: Request):
    return templates.TemplateResponse(request, "support/login.html", {"error": None})


@router.post("/support/login")
async def support_login(request: Request):
    form = await request.form()
    login = (_clean(form.get("login")) or "").lower()
    password = str(form.get("password") or "")
    if _bloque(login):
        return templates.TemplateResponse(
            request, "support/login.html",
            {"error": "Trop d'essais. Réessayez dans un quart d'heure.", "login": login},
            status_code=429)
    reporter = repo.get_reporter_by_login(login)
    if not reporter or not reporter["active"] or \
            not auth.verify_password(password, reporter["password_hash"]):
        _echecs.setdefault(login, []).append(time.time())
        return templates.TemplateResponse(
            request, "support/login.html",
            {"error": "Identifiant ou mot de passe incorrect.", "login": login},
            status_code=401)
    _echecs.pop(login, None)
    repo.update_reporter(reporter["id"], seen=True)
    response = RedirectResponse("/support", status_code=303)
    auth.issue_reporter_cookie(response, reporter["id"])
    return response


@router.get("/support/logout")
def support_logout():
    response = RedirectResponse("/support/login", status_code=303)
    auth.clear_reporter_cookie(response)
    return response


@router.get("/support", response_class=HTMLResponse)
def support_home(request: Request):
    reporter = _signaleur(request)
    tickets = [t for t in repo.list_tickets(reporter_id=reporter["id"])
               if t["status"] != "draft" or t["user_messages"]]
    return templates.TemplateResponse(request, "support/home.html",
                                      {"reporter": reporter, "tickets": tickets,
                                       "plusieurs": len(reporter["projects"]) > 1})


def accueil(reporter: dict, projet: dict) -> str:
    prenom = (reporter["name"] or "").split(" ")[0]
    return (f"Bonjour {prenom} ! Quel problème rencontrez-vous sur {projet['name']} ? "
            "Dites-moi sur quel écran vous étiez et ce qui s'est passé.")


@router.post("/support/new")
async def support_new(request: Request):
    reporter = _signaleur(request)
    form = await request.form()
    projets = {p["id"]: p for p in reporter["projects"]}
    # Un seul projet : on ne demande rien. Plusieurs : le choix vient du
    # formulaire, et il doit être l'un des SIENS.
    choisi = _int(form.get("project_id"))
    if len(projets) == 1:
        choisi = next(iter(projets))
    if choisi not in projets:
        return RedirectResponse("/support", status_code=303)
    # Un brouillon encore vide sur ce projet sert de nouveau : cliquer dix fois
    # sur « Signaler » ne crée pas dix tickets.
    for t in repo.list_tickets(reporter_id=reporter["id"], status="draft"):
        if not t["user_messages"] and t["project_id"] == choisi:
            return RedirectResponse(f"/support/t/{t['id']}", status_code=303)
    ticket = repo.create_ticket(reporter, choisi, greeting=accueil(reporter, projets[choisi]))
    return RedirectResponse(f"/support/t/{ticket['id']}", status_code=303)


def _ticket_ctx(reporter: dict, ticket: dict) -> dict:
    return {"reporter": reporter, "t": ticket,
            "messages": repo.ticket_messages_with_files(ticket["id"]),
            "quota_atteint": repo.user_messages_today(reporter["id"])
                             >= repo.SUPPORT_MAX_MESSAGES_PAR_JOUR}


@router.get("/support/t/{ticket_id}", response_class=HTMLResponse)
def support_ticket(request: Request, ticket_id: int):
    reporter = _signaleur(request)
    ticket = _son_ticket(reporter, ticket_id)
    gabarit = "support/_conversation.html" if _is_ajax(request) else "support/ticket.html"
    return templates.TemplateResponse(request, gabarit, _ticket_ctx(reporter, ticket))


@router.post("/support/t/{ticket_id}/message")
async def support_message(request: Request, ticket_id: int):
    reporter = _signaleur(request)
    ticket = _son_ticket(reporter, ticket_id)
    form = await request.form()
    texte = (_clean(form.get("content")) or "")[:MESSAGE_MAX]
    lues = await captures.lis_tous(form.getlist("captures"))
    if ((texte or lues) and ticket["status"] == "draft" and not ticket["awaiting_ai"]
            and ticket["messages"] < MESSAGES_PAR_TICKET
            and repo.user_messages_today(reporter["id"]) < repo.SUPPORT_MAX_MESSAGES_PAR_JOUR):
        message_id = repo.add_user_message(ticket_id, texte or "(pièce jointe)")
        captures.joins(lues, ticket, message_id)
    if _is_ajax(request):
        return templates.TemplateResponse(request, "support/_conversation.html",
                                          _ticket_ctx(reporter, repo.get_ticket(ticket_id)))
    return RedirectResponse(f"/support/t/{ticket_id}", status_code=303)


def _sert(fichier: dict | None):
    # Image affichée (nosniff), tout le reste en téléchargement : voir captures.
    return captures.reponse(fichier)


@router.get("/support/capture/{file_id}")
def support_capture(request: Request, file_id: int):
    """Une capture d'un de SES signalements (jointe par lui ou montrée par l'IA)."""
    reporter = _signaleur(request)
    fichier = repo.get_support_file(file_id)
    if not fichier or not fichier["ticket_id"]:
        raise HTTPException(status_code=404, detail="capture introuvable")
    _son_ticket(reporter, fichier["ticket_id"])
    return _sert(fichier)


@router.post("/support/t/{ticket_id}/submit")
def support_submit(request: Request, ticket_id: int):
    reporter = _signaleur(request)
    ticket = _son_ticket(reporter, ticket_id)
    if repo.submit_ticket(ticket_id):
        notify.previens(
            "ticket_submitted",
            f"📨 Signalement — {ticket['project_name']}",
            [f"{reporter['name']}" + (f" ({reporter['agency']})" if reporter["agency"] else ""),
             "", ticket["title"] or "(sans titre)",
             notify.abrege(ticket["summary"] or "", 300)]
            + ([f"↺ doublon probable du signalement n° {ticket['duplicate_of']}"]
               if ticket.get("duplicate_of") else []),
            f"{config.PUBLIC_URL.rstrip('/')}/signalements/{ticket_id}")
    return RedirectResponse(f"/support/t/{ticket_id}", status_code=303)


# --------------------------------------------------------------------------
# Côté Kevin
# --------------------------------------------------------------------------

def _admin_ctx(**extra) -> dict:
    return {"page": "signalements", "page_label": "Signalements",
            "projects": repo.list_projects(), **extra}


@router.get("/signalements", response_class=HTMLResponse)
def signalements(request: Request, nouveau: str = "", remise: str = ""):
    login, motdepasse, _ = _a_remettre.pop(remise, (nouveau, "", 0))
    tickets = [t for t in repo.list_tickets() if t["status"] != "draft"]
    brouillons = [t for t in repo.list_tickets(status="draft") if t["user_messages"]]
    return templates.TemplateResponse(request, "signalements.html", _admin_ctx(
        a_valider=[t for t in tickets if t["status"] == "submitted"],
        traites=[t for t in tickets if t["status"] != "submitted"][:50],
        brouillons=brouillons[:20],
        reporters=repo.list_reporters(),
        # Le mot de passe généré n'est montré qu'une fois, au retour de la
        # création : il n'est stocké nulle part en clair.
        nouveau=login, motdepasse=motdepasse))


@router.get("/signalements/{ticket_id}", response_class=HTMLResponse)
def signalement(request: Request, ticket_id: int):
    ticket = repo.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="signalement introuvable")
    return templates.TemplateResponse(request, "signalement.html", _admin_ctx(
        t=ticket, messages=repo.ticket_messages_with_files(ticket_id),
        priorities=config.PRIORITIES))


@router.post("/signalements/{ticket_id}/accept")
async def signalement_accept(request: Request, ticket_id: int):
    form = await request.form()
    try:
        task = repo.accept_ticket(ticket_id, queue=form.get("queue") == "1",
                                  priority=_int(form.get("priority")) or 2,
                                  note=_clean(form.get("note")))
    except repo.NotFound:
        return RedirectResponse(f"/signalements/{ticket_id}", status_code=303)
    ticket = repo.get_ticket(ticket_id)
    return RedirectResponse(f"/p/{ticket['project_slug']}/tasks#task-{task['id']}",
                            status_code=303)


@router.post("/signalements/{ticket_id}/reject")
async def signalement_reject(request: Request, ticket_id: int):
    form = await request.form()
    raison = _clean(form.get("reason"))
    if raison:
        repo.reject_ticket(ticket_id, raison)
    return RedirectResponse("/signalements", status_code=303)


# Mot de passe généré, remis une seule fois. Il ne passe PAS par l'URL de
# redirection : il finirait dans l'historique du navigateur et le journal
# d'accès du serveur. On le garde en mémoire sous un jeton à usage unique.
_a_remettre: dict[str, tuple[str, str, float]] = {}


def _remets(login: str, password: str) -> str:
    for cle in [k for k, (_, _, t) in _a_remettre.items() if time.time() - t > 600]:
        _a_remettre.pop(cle, None)
    jeton = secrets.token_urlsafe(12)
    _a_remettre[jeton] = (login, password, time.time())
    return jeton


def _mot_de_passe() -> str:
    # Sans caractères ambigus : un I pris pour un l a déjà coûté vingt minutes.
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


@router.post("/signalements/reporters")
async def reporter_create(request: Request):
    form = await request.form()
    name, login = _clean(form.get("name")), (_clean(form.get("login")) or "").lower()
    ids = _projets_du_formulaire(form)
    if not (name and login and ids) or repo.get_reporter_by_login(login):
        return RedirectResponse("/signalements?nouveau=erreur#comptes", status_code=303)
    password = _mot_de_passe()
    repo.create_reporter(name, login, auth.hash_password(password), ids,
                         agency=_clean(form.get("agency")))
    return RedirectResponse(f"/signalements?remise={_remets(login, password)}#comptes",
                            status_code=303)


def _projets_du_formulaire(form) -> list[int]:
    ids = []
    for slug in form.getlist("projects"):
        projet = repo.get_project(_clean(slug) or None)
        if projet and projet["id"] not in ids:
            ids.append(projet["id"])
    return ids


@router.post("/signalements/reporters/{reporter_id}/projects")
async def reporter_projects(request: Request, reporter_id: int):
    form = await request.form()
    ids = _projets_du_formulaire(form)
    if ids and repo.get_reporter(reporter_id):
        repo.set_reporter_projects(reporter_id, ids)
    return RedirectResponse("/signalements#comptes", status_code=303)


@router.post("/signalements/reporters/{reporter_id}/toggle")
def reporter_toggle(reporter_id: int):
    reporter = repo.get_reporter(reporter_id)
    if reporter:
        repo.update_reporter(reporter_id, active=not reporter["active"])
    return RedirectResponse("/signalements#comptes", status_code=303)


@router.post("/signalements/reporters/{reporter_id}/reset")
def reporter_reset(reporter_id: int):
    reporter = repo.get_reporter(reporter_id)
    if not reporter:
        return RedirectResponse("/signalements#comptes", status_code=303)
    password = _mot_de_passe()
    repo.update_reporter(reporter_id, password_hash=auth.hash_password(password))
    return RedirectResponse(f"/signalements?remise={_remets(reporter['login'], password)}#comptes",
                            status_code=303)


@router.get("/signalements/capture/{file_id}")
def admin_capture(file_id: int):
    return _sert(repo.get_support_file(file_id))


@router.get("/signalements/fiche/{slug}", response_class=HTMLResponse)
def fiche_support(request: Request, slug: str):
    project = repo.require_project(slug)
    return templates.TemplateResponse(request, "fiche_support.html", _admin_ctx(
        project=project, references=repo.reference_files(project["id"])))


@router.post("/signalements/fiche/{slug}/captures")
async def fiche_capture_ajout(request: Request, slug: str):
    """Captures de référence : ce que l'IA peut MONTRER au signaleur (« voici
    où se trouve le bouton »). Chacune avec une légende, que l'IA lit."""
    project = repo.require_project(slug)
    form = await request.form()
    legende = _clean(form.get("caption"))
    for donnees, mime, ext, nom in await captures.lis_tous(form.getlist("captures"),
                                                            images_seules=True):
        stored = captures.range_(donnees, ext, f"references/{project['slug']}")
        repo.add_support_file("reference", project["id"], nom, mime, len(donnees), stored,
                              caption=legende or nom)
    return RedirectResponse(f"/signalements/fiche/{slug}#captures", status_code=303)


@router.post("/signalements/fiche/{slug}/captures/{file_id}/delete")
def fiche_capture_retrait(slug: str, file_id: int):
    fichier = repo.get_support_file(file_id)
    if fichier and fichier["source"] == "reference":
        captures.efface(repo.delete_support_file(file_id))
    return RedirectResponse(f"/signalements/fiche/{slug}#captures", status_code=303)


@router.post("/signalements/fiche/{slug}")
async def fiche_support_save(request: Request, slug: str):
    project = repo.require_project(slug)
    form = await request.form()
    repo.set_support_context(project["id"], _clean(form.get("support_context")),
                             git=_clean(form.get("support_git")),
                             branch=_clean(form.get("support_branch")))
    return RedirectResponse(f"/signalements/fiche/{slug}?ok=1", status_code=303)
