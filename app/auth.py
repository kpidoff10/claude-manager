"""Authentification : token bearer pour le MCP, cookie signé pour l'interface web."""
import hmac

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

        if path.startswith("/mcp") or path.startswith("/api"):
            # Un cookie web valide autorise aussi le MCP, ce qui permet de tester
            # le point de terminaison depuis le navigateur une fois connecté.
            if not (check_bearer(request) or is_logged_in(request)):
                return JSONResponse({"error": "token invalide ou absent"}, status_code=401)
            return await call_next(request)

        if any(path == p or path.startswith(p + "/") for p in OPEN_PATHS):
            return await call_next(request)

        if not is_logged_in(request):
            if request.headers.get("hx-request"):
                response = JSONResponse({"error": "session expirée"}, status_code=401)
                response.headers["HX-Redirect"] = "/login"
                return response
            return RedirectResponse(f"/login?next={path}", status_code=303)

        return await call_next(request)
