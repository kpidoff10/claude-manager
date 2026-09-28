"""Prévenir Kevin par Telegram quand quelque chose l'attend.

La file est **séquentielle** : un agent qui pose une question bloque tout ce qui
suit. Sans message sortant, on ne l'apprend qu'en ouvrant l'interface — et une
question posée à 14 h découverte à 17 h, ce sont trois heures de file à l'arrêt.

Telegram parce qu'il n'y a rien à tenir : pas de serveur de courrier, pas
d'application à installer, une requête HTTPS et c'est fini.

## Deux règles

**Un envoi qui échoue ne fait jamais échouer ce qu'il annonce.** Prévenir est
accessoire ; enregistrer la fin d'une exécution ne l'est pas. Tout part donc
dans un fil détaché, et la moindre erreur y est avalée. Un réseau coupé ne doit
pas laisser une exécution à moitié écrite.

**Le jeton du bot vit dans l'environnement, jamais en base.** C'est la règle du
projet. L'identifiant de conversation, lui, n'est pas un secret : il va dans la
table `settings`, où Kevin peut le changer depuis l'interface.
"""
import hashlib
import hmac
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import config, repo

log = logging.getLogger("claude-manager.notify")

API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 10

# Clés de réglage, préfixées pour ne pas se mêler à celles de la file.
CLE_CHAT = "notify.telegram_chat_id"
PREFIXE = "notify.event."

# Les moments où l'on dérange quelqu'un. L'ordre est celui de la page de
# réglages : d'abord ce qui attend une action, ensuite ce qui n'est qu'une
# nouvelle. `defaut` dit ce qui est coché tant que Kevin n'a rien touché.
EVENEMENTS: dict[str, dict] = {
    "needs_input": {
        "label": "Un agent pose une question",
        "detail": "Le plus urgent : la file est séquentielle, "
                  "une question sans réponse arrête tout ce qui suit.",
        "defaut": True,
    },
    "run_passed": {
        "label": "Une exécution finit, tests verts",
        "detail": "Le travail attend ta relecture avant fusion.",
        "defaut": True,
    },
    "run_failed": {
        "label": "Une exécution finit, tests en échec",
        "detail": "L'agent a rendu du travail que les tests refusent.",
        "defaut": True,
    },
    "run_error": {
        "label": "Une exécution s'arrête anormalement",
        "detail": "Délai dépassé, boucle détectée, ou arrêt imprévu.",
        "defaut": True,
    },
    "merge_broken": {
        "label": "Une fusion laisse la base en échec",
        "detail": "Les tests passaient chez l'agent mais pas après fusion — "
                  "le cas d'un commit partiel. La fusion est défaite si personne "
                  "n'a commité depuis.",
        "defaut": True,
    },
    "reminder": {
        "label": "Un rappel arrive à échéance",
        "detail": "Un rappel posé sur un projet ou une tâche (« teste ça demain »), "
                  "avec des boutons pour le repousser d'une heure, à demain, ou le retirer.",
        "defaut": True,
    },
    "ticket_submitted": {
        "label": "Un utilisateur d'agence envoie un signalement",
        "detail": "Il attend ta validation avant de devenir une tâche : rien ne part "
                  "en file sans toi.",
        "defaut": True,
    },
    "task_orphan": {
        "label": "Une tâche reste « en cours » sans que rien ne la porte",
        "detail": "Marquée en cours puis laissée là : la file ne la réclamera "
                  "pas, et elle disparaît des radars. Une seule alerte par tâche.",
        "defaut": True,
    },
}


def token() -> str:
    """Jeton du bot, depuis l'environnement. Vide = rien n'est envoyé."""
    return config.TELEGRAM_TOKEN


def chat_id(conn=None) -> str:
    return repo.get_setting(CLE_CHAT, conn=conn).strip()


def configure(conn=None) -> bool:
    """Y a-t-il de quoi envoyer ? Jeton et destinataire, les deux."""
    return bool(token() and chat_id(conn=conn))


def est_actif(evenement: str, conn=None) -> bool:
    if evenement not in EVENEMENTS:
        return False
    defaut = "1" if EVENEMENTS[evenement]["defaut"] else "0"
    return repo.get_setting(PREFIXE + evenement, default=defaut, conn=conn) == "1"


def etat(conn=None) -> list[dict]:
    """Les événements et leur case, pour la page de réglages."""
    return [{"cle": cle, **info, "actif": est_actif(cle, conn=conn)}
            for cle, info in EVENEMENTS.items()]


def secret_webhook() -> str:
    """Jeton que Telegram nous renverra en en-tête à chaque appel.

    **Dérivé, jamais stocké.** La règle du projet interdit une valeur secrète en
    base, et une variable d'environnement de plus serait une chose à régler pour
    rien : on le calcule à partir du secret de session, qui existe déjà et ne
    sort jamais. Changer `CM_SESSION_SECRET` invalide le webhook — c'est le
    comportement voulu, il suffit de le rebrancher.
    """
    return hmac.new(config.SESSION_SECRET.encode(), b"telegram-webhook",
                    hashlib.sha256).hexdigest()[:32]


def url_webhook() -> str:
    return f"{config.PUBLIC_URL.rstrip('/')}/telegram/{secret_webhook()[:12]}"


def appel(methode: str, charge: dict) -> tuple[bool, dict | str]:
    """Un appel à l'API Telegram. Renvoie (réussi, résultat ou explication)."""
    if not token():
        return False, "aucun jeton de bot"
    donnees = json.dumps(charge).encode()
    requete = urllib.request.Request(
        f"https://api.telegram.org/bot{token()}/{methode}",
        data=donnees, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(requete, timeout=TIMEOUT) as reponse:
            reçu = json.loads(reponse.read().decode("utf-8", "replace"))
        if reçu.get("ok"):
            return True, reçu.get("result", {})
        return False, str(reçu.get("description", "refus sans explication"))
    except urllib.error.HTTPError as erreur:
        detail = erreur.read().decode("utf-8", "replace")[:300]
        try:
            detail = json.loads(detail).get("description", detail)
        except (ValueError, AttributeError):
            pass
        return False, f"{erreur.code} : {detail}"
    except (urllib.error.URLError, OSError, ValueError) as erreur:
        return False, str(erreur)


def branche_webhook() -> tuple[bool, str]:
    """Déclare à Telegram où nous joindre. À rappeler si l'URL publique change."""
    ok, res = appel("setWebhook", {
        "url": url_webhook(),
        "secret_token": secret_webhook(),
        # On ne veut que les messages et les appuis sur boutons : le reste est du
        # bruit qu'il faudrait filtrer à chaque appel.
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    })
    return (True, f"Webhook branché sur {url_webhook()}") if ok else (False, f"Telegram a refusé : {res}")


def etat_webhook() -> dict:
    ok, res = appel("getWebhookInfo", {})
    return res if ok and isinstance(res, dict) else {}


def envoie(texte: str, conn=None) -> tuple[bool, str]:
    """Envoi **synchrone**, pour le bouton d'essai qui doit rendre un verdict.

    Renvoie (réussi, explication). L'explication est destinée à l'écran : elle
    dit quoi corriger, pas seulement que ça n'a pas marché.
    """
    if not token():
        return False, ("Aucun jeton de bot. Poser CM_TELEGRAM_TOKEN dans "
                       "l'environnement du conteneur, puis redémarrer.")
    destinataire = chat_id(conn=conn)
    if not destinataire:
        return False, "Aucun identifiant de conversation enregistré."

    # Texte brut, sans `parse_mode` : un titre de tâche contenant `<` ou `&`
    # ferait rejeter tout le message par Telegram si on lui promettait du HTML.
    # Les liens restent cliquables, Telegram les reconnaît seul.
    corps = urllib.parse.urlencode({
        "chat_id": destinataire,
        "text": texte,
        "disable_web_page_preview": "true",
    }).encode()
    requete = urllib.request.Request(API.format(token=token()), data=corps)
    try:
        with urllib.request.urlopen(requete, timeout=TIMEOUT) as reponse:
            charge = json.loads(reponse.read().decode("utf-8", "replace"))
        if charge.get("ok"):
            return True, "Message envoyé."
        return False, f"Telegram a refusé : {charge.get('description', '?')}"
    except urllib.error.HTTPError as erreur:
        # Telegram explique le refus dans le corps, pas dans le code : « chat not
        # found » et « unauthorized » arrivent tous deux en 400/401, et c'est la
        # description qui dit lequel des deux réglages est en cause.
        detail = erreur.read().decode("utf-8", "replace")[:300]
        try:
            detail = json.loads(detail).get("description", detail)
        except (ValueError, AttributeError):
            pass
        return False, f"Telegram a refusé ({erreur.code}) : {detail}"
    except (urllib.error.URLError, OSError, ValueError) as erreur:
        return False, f"Envoi impossible : {erreur}"


def previens(evenement: str, titre: str, lignes: list[str] | None = None,
             lien: str | None = None) -> None:
    """Prévient si l'événement est coché. **Ne lève jamais**, ne bloque jamais.

    Appelée depuis les points de bascule de la file, qui tournent dans la requête
    du démon : un Telegram lent y retarderait la tâche suivante. D'où le fil
    détaché — on n'attend pas de savoir si le message est parti.
    """
    try:
        if not est_actif(evenement) or not configure():
            return
        texte = "\n".join([titre, *(lignes or []), *( [lien] if lien else [] )])
    except Exception:  # noqa: BLE001 — prévenir ne doit rien casser en amont
        log.exception("notification %s : préparation impossible", evenement)
        return

    def _envoi() -> None:
        try:
            reussi, detail = envoie(texte)
            if not reussi:
                log.warning("notification %s non partie : %s", evenement, detail)
        except Exception:  # noqa: BLE001
            log.exception("notification %s : envoi impossible", evenement)

    threading.Thread(target=_envoi, daemon=True).start()


CLE_QUESTIONS = "notify.questions_posees"
MAX_SUIVI = 40


def demande(task_id: int, titre: str, lignes: list[str], options: list[str] | None,
            lien: str | None = None) -> None:
    """Pose la question sur Telegram, options en boutons, et retient le message.

    **Synchrone, contrairement aux autres envois** : on a besoin de l'identifiant
    du message pour rattacher une réponse libre à sa tâche. Un appui sur bouton
    porte déjà le numéro de tâche ; une réponse écrite, non — elle n'a que le
    message auquel elle répond.

    Reste sans effet et sans bruit si rien n'est configuré.
    """
    try:
        if not est_actif("needs_input") or not configure():
            return
        texte = "\n".join([titre, *lignes, *([lien] if lien else [])])
        clavier = None
        if options:
            # Le numéro de tâche voyage dans le bouton : au retour, aucun doute
            # sur ce à quoi l'on répond, même si trois questions sont ouvertes.
            clavier = {"inline_keyboard": [
                [{"text": abrege(o, 60), "callback_data": f"r:{task_id}:{i}"}]
                for i, o in enumerate(options[:3])]}
        charge = {"chat_id": chat_id(), "text": texte,
                  "disable_web_page_preview": True}
        if clavier:
            charge["reply_markup"] = clavier
        ok, res = appel("sendMessage", charge)
        if ok and isinstance(res, dict) and res.get("message_id"):
            retiens_question(res["message_id"], task_id, options or [])
        elif not ok:
            log.warning("question #%s non envoyée : %s", task_id, res)
    except Exception:  # noqa: BLE001 — poser la question ne doit rien casser
        log.exception("question #%s : envoi impossible", task_id)


def rappelle(rappel: dict) -> bool:
    """Envoie un rappel avec ses boutons. Vrai si c'est parti — ou s'il n'y
    avait rien à envoyer (Telegram non configuré, événement décoché) : dans les
    deux cas, inutile de retenter. Faux seulement sur un échec d'envoi."""
    try:
        if not est_actif("reminder") or not configure():
            return True
        lignes = [f"⏰ Rappel — {rappel.get('project_name', '?')}"]
        if rappel.get("task_id"):
            lignes.append(f"#{rappel['task_id']} {rappel.get('task_title') or ''}")
        if rappel.get("note"):
            lignes.append(f"👉 {rappel['note']}")
        lignes.append(lien_tache(rappel["task_id"]) if rappel.get("task_id")
                      else lien_projet(rappel.get("project_slug", "")))
        rid = rappel["id"]
        clavier = {"inline_keyboard": [[
            {"text": "+1 h", "callback_data": f"rp:{rid}:1h"},
            {"text": "Demain 9 h", "callback_data": f"rp:{rid}:demain"},
            {"text": "✓ C'est bon", "callback_data": f"rp:{rid}:ok"},
        ]]}
        ok, res = appel("sendMessage", {"chat_id": chat_id(), "text": "\n".join(lignes),
                                        "disable_web_page_preview": True,
                                        "reply_markup": clavier})
        if not ok:
            log.warning("rappel %s non envoyé : %s", rid, res)
        return ok
    except Exception:  # noqa: BLE001 — un rappel ne doit rien casser
        log.exception("rappel %s : envoi impossible", rappel.get("id"))
        return False


def lien_projet(slug: str) -> str:
    return f"{config.PUBLIC_URL.rstrip('/')}/p/{slug}/tasks"


def lien_tache(task_id: int) -> str:
    return f"{config.PUBLIC_URL.rstrip('/')}/task/{task_id}"


def abrege(texte: str, taille: int) -> str:
    texte = " ".join((texte or "").split())
    return texte if len(texte) <= taille else texte[:taille - 1] + "…"


def retiens_question(message_id: int, task_id: int, options: list[str]) -> None:
    """Retient à quelle tâche répond un message, pour les réponses écrites."""
    try:
        suivi = json.loads(repo.get_setting(CLE_QUESTIONS, "{}") or "{}")
    except (ValueError, TypeError):
        suivi = {}
    suivi[str(message_id)] = {"task": task_id, "options": options[:3]}
    # Borné : sans cela le réglage grossit sans fin. Les messages anciens ne
    # servent plus — une question résolue ne se répond pas deux fois.
    if len(suivi) > MAX_SUIVI:
        for vieux in sorted(suivi, key=int)[:len(suivi) - MAX_SUIVI]:
            suivi.pop(vieux, None)
    repo.set_setting(CLE_QUESTIONS, json.dumps(suivi))


def question_du_message(message_id: int | None) -> dict | None:
    if not message_id:
        return None
    try:
        suivi = json.loads(repo.get_setting(CLE_QUESTIONS, "{}") or "{}")
    except (ValueError, TypeError):
        return None
    return suivi.get(str(message_id))


def lien_run(run_id: int) -> str:
    return f"{config.PUBLIC_URL.rstrip('/')}/runs/{run_id}"


def lien_taches(slug: str) -> str:
    return f"{config.PUBLIC_URL.rstrip('/')}/p/{slug}/tasks?view=kanban"
