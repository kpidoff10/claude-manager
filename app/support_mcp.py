"""MCP « support » : ce que l'IA des signalements peut consulter, en lecture seule.

Ce n'est PAS le MCP de claude-manager. Celui-là écrit (tâches, mémoire, journal)
et lit tout — y compris l'audit, les failles et les décisions internes. Celui-ci
n'expose que trois lectures, bornées au projet du signalement en cours :

- la fiche support rédigée par Kevin ;
- les signalements déjà déposés (titre, écran, état — ni auteur ni résumé) ;
- l'état d'un signalement précis.

**Le périmètre est porté par la requête, pas par l'IA.** Le démon joint à chaque
appel le numéro du signalement et un jeton dérivé de ce numéro (HMAC sur le
secret de session). Sans jeton valide, rien ne répond ; avec, on ne voit que le
projet de CE signalement. L'IA ne peut pas demander un autre projet : aucun
outil ne prend de projet en paramètre.
"""
import hashlib
import hmac

from mcp.server.fastmcp import Context, FastMCP

from . import config, repo

support_mcp = FastMCP("support", stateless_http=True)
support_mcp.settings.streamable_http_path = "/"

ETATS = {"submitted": "reçu, en attente de validation", "done": "corrigé",
         "cancelled": "abandonné", "in_progress": "en cours de correction",
         "review": "en cours de correction", "needs_input": "en cours de correction",
         "blocked": "en cours de correction"}


def jeton(ticket_id: int) -> str:
    """Jeton d'accès au MCP support pour UN signalement. Dérivé, jamais stocké."""
    return hmac.new(config.SESSION_SECRET.encode(), f"support-mcp:{ticket_id}".encode(),
                    hashlib.sha256).hexdigest()


def ticket_de_la_requete(request) -> dict | None:
    """Le signalement que la requête a le droit de voir, ou None."""
    try:
        ticket_id = int(request.headers.get("x-support-ticket", ""))
    except ValueError:
        return None
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), jeton(ticket_id)):
        return None
    return repo.get_ticket(ticket_id)


def _ticket(ctx: Context) -> dict:
    ticket = ticket_de_la_requete(ctx.request_context.request)
    if ticket is None:
        raise PermissionError("accès refusé")
    return ticket


def _etat(t: dict) -> str:
    return ETATS.get(t.get("task_status") or t["status"], "validé, correction prévue")


@support_mcp.tool()
def fiche_support(ctx: Context) -> str:
    """La fiche support du logiciel, rédigée par l'équipe : écrans, vocabulaire,
    problèmes connus et contournements, ce qui est un comportement normal."""
    ticket = _ticket(ctx)
    projet = repo.get_project(ticket["project_id"]) or {}
    return projet.get("support_context") or projet.get("description") or "(aucune fiche rédigée)"


@support_mcp.tool()
def signalements_connus(ctx: Context, recherche: str = "") -> list[dict]:
    """Signalements déjà déposés sur ce logiciel (120 derniers jours) : numéro,
    titre, écran, état, date. `recherche` filtre sur le titre et l'écran."""
    ticket = _ticket(ctx)
    mots = [m for m in (recherche or "").lower().split() if len(m) > 2]
    out = []
    for t in repo.known_tickets(ticket["id"], limit=100):
        texte = f"{t.get('title') or ''} {t.get('page') or ''}".lower()
        if mots and not any(m in texte for m in mots):
            continue
        out.append({"numero": t["id"], "titre": t.get("title"), "ecran": t.get("page"),
                    "etat": _etat(t), "date": t.get("date")})
    return out


@support_mcp.tool()
def etat_signalement(ctx: Context, numero: int) -> dict:
    """L'état d'un signalement précis de ce logiciel, par son numéro."""
    ticket = _ticket(ctx)
    t = repo.get_ticket(int(numero))
    if not t or t["project_id"] != ticket["project_id"] or t["status"] == "draft":
        return {"numero": numero, "etat": "inconnu"}
    return {"numero": t["id"], "titre": t.get("title"), "ecran": t.get("page"),
            "etat": "refusé" if t["status"] == "rejected" else _etat(t)}
