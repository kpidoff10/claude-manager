"""Utilitaires partagés par les hooks Claude Code.

Les hooks tournent sur l'hôte, hors du conteneur : ils lisent le token dans
.env et attaquent le port publié en local. Aucun d'eux ne doit pouvoir faire
échouer une session — toutes les erreurs sont avalées.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MANAGER_DIR = Path("/home/dev/projects/claude-manager")
BASE_URL = "http://127.0.0.1:8099"
STATE_DIR = Path.home() / ".cache" / "claude-manager" / "sessions"
TIMEOUT = 6


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def env_value(name: str) -> str:
    env_path = MANAGER_DIR / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return ""


def token() -> str:
    env_path = MANAGER_DIR / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text().splitlines():
        if line.startswith("CM_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def api_get(path: str, params: dict) -> tuple[int, str]:
    """Retourne (code, corps). Code 0 signale une panne de transport."""
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, ""
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return 0, ""


def read_payload(stream) -> dict:
    try:
        return json.load(stream) or {}
    except (json.JSONDecodeError, ValueError):
        return {}


def state_path(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "inconnue") if c.isalnum() or c in "-_")[:80]
    return STATE_DIR / f"{safe}.json"


def load_state(session_id: str) -> dict:
    path = state_path(session_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(session_id: str, **values) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state = load_state(session_id)
        state.update(values)
        state_path(session_id).write_text(json.dumps(state))
    except OSError:
        pass
