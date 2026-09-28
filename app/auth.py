"""Authentification : token bearer pour le MCP, cookie signé pour l'interface web."""
import hashlib
import hmac
import secrets

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from . import config

COOKIE_NAME = "cm_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 jours

_serializer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="cm-web")

# `/telegram` est ouvert parce que Telegram appelle sans cookie ni bearer. Il
# n'est pas pour autant sans garde : la route exige l'en-tête secret convenu au
# moment de brancher le webhook, et n'accepte que la conversation enregistrée.
OPEN_PATHS = {"/login", "/health", "/static", "/telegram"}

# Espace des signaleurs (utilisateurs d'agence). Il a SA session, distincte de
# celle de Kevin : autre cookie, autre sel. Un cookie de signaleur n'ouvre que
# /support — jamais l'interface, l'API ni le MCP, qui ne lisent que cm_session.
SUPPORT_PREFIX = "/support"
REPORTER_COOKIE = "cm_signaleur"
_reporter_serializer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="cm-support")


def hash_password(password: str) -> str:
    sel = secrets.token_hex(16)
    empreinte = hashlib.scrypt(password.encode(), salt=bytes.fromhex(sel), n=2 ** 14, r=8, p=1)
    return f"scrypt${sel}${empreinte.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, sel, attendue = stored.split("$")
        empreinte = hashlib.scrypt(password.encode(), salt=bytes.fromhex(sel), n=2 ** 14, r=8, p=1)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(empreinte.hex(), attendue)


def issue_reporter_cookie(response, reporter_id: int) -> None:
    response.set_cookie(
        REPORTER_COOKIE, _reporter_serializer.dumps(reporter_id), max_age=COOKIE_MAX_AGE,
        httponly=True, samesite="lax", secure=config.PUBLIC_URL.startswith("https"),
        path=SUPPORT_PREFIX,
    )


def clear_reporter_cookie(response) -> None:
    response.delete_cookie(REPORTER_COOKIE, path=SUPPORT_PREFIX)


def reporter_id(request) -> int | None:
    token = request.cookies.get(REPORTER_COOKIE)
    if not token:
        return None
    try:
        return int(_reporter_serializer.loads(token, max_age=COOKIE_MAX_AGE))
    except (BadSignature, ValueError, TypeError):
        return None


def issue_cookie(response) -> None:
    response.set_cookie(
        COOKIE_NAME, _serializer.dumps("ok"), max_age=COOKIE_MAX_AGE,
        httponly=True, samesite="lax", secure=config.PUBLIC_URL.startswith("https"),
    )


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME)


def is_logged_in(request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=COOKIE_MAX_AGE)
        return True
    except BadSignature:
        return False


def check_password(candidate: str) -> bool:
    return bool(config.WEB_PASSWORD) and hmac.compare_digest(candidate, config.WEB_PASSWORD)


def check_bearer(request) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not config.API_TOKEN:
        return False
    return hmac.compare_digest(token.strip(), config.API_TOKEN)


class AuthMiddleware(BaseHTTPMiddleware):
    """Le MCP s'authentifie par token, l'interface web par cookie."""

    async def dispatch(self, request, call_next):
        path = request.url.path

        # MCP support : jeton propre à UN signalement (voir support_mcp). Testé
        # avant /mcp, dont il partage le préfixe : ni le jeton de Kevin ni son
        # cookie n'y sont utiles, et ce jeton-là n'ouvre rien d'autre.
        if path == "/mcp-support" or path.startswith("/mcp-support/"):
            from .support_mcp import ticket_de_la_requete
            if ticket_de_la_requete(request) is None:
                return JSONResponse({"error": "accès refusé"}, status_code=401)
            return await call_next(request)

        if path.startswith("/mcp") or path.startswith("/api"):
            # Un cookie web valide autorise aussi le MCP, ce qui permet de tester
            # le point de terminaison depuis le navigateur une fois connecté.
            if not (check_bearer(request) or is_logged_in(request)):
                return JSONResponse({"error": "token invalide ou absent"}, status_code=401)
            return await call_next(request)

        if any(path == p or path.startswith(p + "/") for p in OPEN_PATHS):
            return await call_next(request)

        # L'espace signaleurs vérifie sa propre session, route par route (le
        # compte peut avoir été désactivé depuis la pose du cookie).
        if path == SUPPORT_PREFIX or path.startswith(SUPPORT_PREFIX + "/"):
            return await call_next(request)

        if not is_logged_in(request):
            if request.headers.get("hx-request"):
                response = JSONResponse({"error": "session expirée"}, status_code=401)
                response.headers["HX-Redirect"] = "/login"
                return response
            return RedirectResponse(f"/login?next={path}", status_code=303)

        return await call_next(request)
