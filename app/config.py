"""Configuration, lue depuis l'environnement."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CM_DATA_DIR", BASE_DIR.parent / "data"))
DB_PATH = DATA_DIR / "manager.db"

# Token bearer présenté par Claude sur /mcp, mot de passe pour l'interface web.
API_TOKEN = os.environ.get("CM_API_TOKEN", "")
WEB_PASSWORD = os.environ.get("CM_WEB_PASSWORD", "")
SESSION_SECRET = os.environ.get("CM_SESSION_SECRET", "dev-secret-change-me")

PUBLIC_URL = os.environ.get("CM_PUBLIC_URL", "http://localhost:8099")
# Résultats possibles d'un test consigné sur une tâche.
TEST_RESULTS = {"ok": "✅", "ko": "❌", "partial": "◐"}
TEST_RESULT_ALIASES = {"ok": "ok", "pass": "ok", "passed": "ok", "vert": "ok", "réussi": "ok",
                       "ko": "ko", "fail": "ko", "failed": "ko", "échec": "ko", "echec": "ko",
                       "rouge": "ko", "partial": "partial", "partiel": "partial"}

# Fuseau dans lequel « demain 9 h » se comprend et les rappels s'affichent.
TIMEZONE = os.environ.get("CM_TZ", "Europe/Paris")


def _empreinte_statique() -> str:
    """Empreinte des fichiers servis tels quels, pour casser le cache.

    Sans elle, l'URL `/static/app.css` ne change jamais : un navigateur — et
    surtout un navigateur mobile — garde la feuille en cache et ne redemande
    rien, même après un déploiement. On a corrigé une interface pendant
    plusieurs échanges sans que le résultat n'apparaisse jamais à l'écran.

    Calculée une fois au démarrage, sur la date de modification : le conteneur
    redémarre à chaque déploiement, donc l'empreinte change exactement quand il
    faut, et jamais entre-temps.
    """
    dossier = BASE_DIR / "web" / "static"
    try:
        dates = [f.stat().st_mtime for f in dossier.rglob("*") if f.is_file()]
        return format(int(max(dates)), "x") if dates else "0"
    except OSError:
        return "0"


STATIC_VERSION = _empreinte_statique()

# Jeton du bot Telegram qui prévient Kevin. Vide = aucun message ne part, et
# rien ne casse. L'identifiant de conversation, lui, n'est pas secret : il vit
# dans la table `settings` et se règle depuis l'interface.
TELEGRAM_TOKEN = os.environ.get("CM_TELEGRAM_TOKEN", "")

# Racine sous laquelle vivent les projets, pour résoudre un cwd en projet.
PROJECTS_ROOT = os.environ.get("CM_PROJECTS_ROOT", "/home/dev/projects")

PRIORITIES = {0: "someday", 1: "low", 2: "normal", 3: "high", 4: "urgent"}
PRIORITY_BY_NAME = {v: k for k, v in PRIORITIES.items()}

# Le pipeline de la file d'agents : todo → queued → in_progress → review → done.
# `needs_input` est la sortie de secours quand l'agent bute sur une décision
# humaine ; `blocked` reste l'empêchement d'origine externe.
TASK_STATUSES = ["todo", "queued", "in_progress", "needs_input", "review",
                 "blocked", "done", "cancelled"]
OPEN_TASK_STATUSES = ["todo", "queued", "in_progress", "needs_input", "review", "blocked"]
# Statuts qui réclament une action de l'humain : ce qui remonte dans le bandeau.
AWAITING_USER_STATUSES = ["review", "needs_input", "blocked"]

AGENT_TIMEOUT_SECONDS = int(os.environ.get("CM_AGENT_TIMEOUT", "2700"))
# Les journaux d'agents sont écrits par le démon, sur l'hôte, et relus par le
# conteneur grâce au montage au même chemin. Le serveur pose le chemin dès la
# réclamation : sinon le lien « journal » ne marche qu'une fois l'agent terminé,
# c'est-à-dire quand on n'en a plus besoin.
RUN_LOG_DIR = os.environ.get("CM_RUN_LOG_DIR",
                             "/home/dev/projects/claude-manager/logs/runs")
# Nombre de renvois en file après un refus ou des tests rouges, avant de laisser
# la tâche à l'humain. Évite qu'un agent boucle indéfiniment sur le même mur.
MAX_RETRIES = int(os.environ.get("CM_MAX_RETRIES", "3"))

# Le dossier de données vu depuis l'HÔTE, où tournent les agents : c'est là
# qu'ils ouvrent les pièces jointes des signalements. Par défaut, déduit du
# dossier des journaux (<dépôt>/logs/runs → <dépôt>/data).
HOST_DATA_DIR = os.environ.get("CM_HOST_DATA_DIR") or str(Path(RUN_LOG_DIR).parent.parent / "data")
# Nombre maximal d'agents simultanés, tous projets confondus. Au-delà de deux ou
# trois, la relecture humaine devient le goulot et le risque de collision monte
# plus vite que le gain de temps.
MAX_PARALLEL = int(os.environ.get("CM_MAX_PARALLEL", "3"))
# `erreur` : une faute commise par un agent (ou par Claude en session), écrite
# comme une règle à suivre. Elles ont leur propre section dans le briefing, pour
# qu'aucune autre mémoire ne les en chasse.
MEMORY_KINDS = ["decision", "convention", "gotcha", "erreur", "context", "note"]
# Catégories de la méthodologie d'un projet. Volontairement peu nombreuses : une
# liste longue se remplit au hasard et ne classe plus rien.
PRACTICE_CATEGORIES = ["demarrage", "termine", "perimetre", "revue", "tests",
                       "livraison", "documentation", "general"]
TECH_CATEGORIES = ["language", "framework", "database", "infra", "lib", "tool", "service"]
TECH_STATUSES = ["active", "considered", "deprecated"]
