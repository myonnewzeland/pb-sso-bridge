import os
import time
import secrets
from typing import Optional

import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse

load_dotenv()

app = FastAPI(title="PocketBase SSO Bridge")

OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET")
JWT_SECRET = os.getenv("JWT_SECRET")
ISSUER = os.getenv("ISSUER", "http://localhost:8000")

# TTLs (segundos)
CODE_TTL = 300        # 5 min
TOKEN_TTL = 3600      # 1 h

# Estado en memoria.
# OJO: solo funciona con --workers 1 (un único proceso).
# Para producción multi-worker / multi-réplica usa Redis o similar.
AUTH_CODES: dict[str, dict] = {}
ACCESS_TOKENS: dict[str, dict] = {}


def _purge_expired() -> None:
    """Limpia entradas expiradas en memoria (best-effort)."""
    now = time.time()
    for k, v in list(AUTH_CODES.items()):
        if v["expires_at"] < now:
            AUTH_CODES.pop(k, None)
    for k, v in list(ACCESS_TOKENS.items()):
        if v["expires_at"] < now:
            ACCESS_TOKENS.pop(k, None)


def get_sso_user(request: Request) -> Optional[dict]:
    """
    Aquí conectas tu SSO/SAML real.

    Opciones comunes:
    - Leer headers que te pasa un reverse proxy SSO.
    - Leer cookie de sesión creada por tu ACS SAML.
    - Validar JWT interno emitido por tu SSO.
    - Consultar /me del sistema SSO.

    Este ejemplo espera headers:
    x-sso-id
    x-sso-email
    x-sso-name
    """

    sso_id = request.headers.get("x-sso-id")
    email = request.headers.get("x-sso-email")
    name = request.headers.get("x-sso-name")

    if not sso_id or not email:
        return None

    return {
        "id": sso_id,
        "email": email,
        "name": name or email,
    }


def _check_client(client_id: str, client_secret: Optional[str] = None) -> None:
    """Comparación constant-time para evitar timing attacks."""
    if not secrets.compare_digest(client_id or "", OAUTH_CLIENT_ID or ""):
        raise HTTPException(status_code=401, detail="Invalid client_id")
    if client_secret is not None:
        if not secrets.compare_digest(client_secret or "", OAUTH_CLIENT_SECRET or ""):
            raise HTTPException(status_code=401, detail="Invalid client credentials")


@app.get("/oauth/authorize")
async def authorize(
    request: Request,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    scope: str = "",
    state: str = "",
):
    if response_type != "code":
        raise HTTPException(status_code=400, detail="response_type must be code")

    _check_client(client_id)

    user = get_sso_user(request)

    if not user:
        """
        Aquí rediriges a tu login SAML/SSO real.

        Ejemplo:
        return RedirectResponse(
            f"https://sso.tudominio.com/login?redirect={request.url}"
        )

        Si tu SSO ya está delante con proxy, entonces esta ruta nunca debería
        llegar sin usuario.
        """
        raise HTTPException(
            status_code=401,
            detail="No SSO session found",
        )

    _purge_expired()

    code = secrets.token_urlsafe(48)

    AUTH_CODES[code] = {
        "user": user,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "expires_at": time.time() + CODE_TTL,
    }

    separator = "&" if "?" in redirect_uri else "?"

    return RedirectResponse(
        f"{redirect_uri}{separator}code={code}&state={state}"
    )


@app.post("/oauth/token")
async def token(
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="Unsupported grant_type")

    _check_client(client_id, client_secret)

    # pop = single-use (anti-replay) dentro de un mismo proceso.
    data = AUTH_CODES.pop(code, None)

    if not data:
        raise HTTPException(status_code=400, detail="Invalid code")

    if data["expires_at"] < time.time():
        raise HTTPException(status_code=400, detail="Expired code")

    if not secrets.compare_digest(data["redirect_uri"], redirect_uri):
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")

    user = data["user"]
    now = int(time.time())

    access_token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": user["id"],
            "email": user["email"],
            "name": user["name"],
            "iat": now,
            "exp": now + TOKEN_TTL,
            "scope": data["scope"],
        },
        JWT_SECRET,
        algorithm="HS256",
    )

    ACCESS_TOKENS[access_token] = {
        "user": user,
        "expires_at": now + TOKEN_TTL,
    }

    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL,
        "scope": data["scope"],
    }


@app.get("/oauth/userinfo")
async def userinfo(request: Request):
    authorization = request.headers.get("authorization", "")

    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    access_token = authorization.split(" ", 1)[1]

    try:
        payload = jwt.decode(
            access_token,
            JWT_SECRET,
            algorithms=["HS256"],
            issuer=ISSUER,
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    return {
        "sub": payload["sub"],
        "id": payload["sub"],
        "email": payload["email"],
        "email_verified": True,
        "name": payload.get("name") or payload["email"],
        "preferred_username": payload["email"],
    }


@app.get("/.well-known/openid-configuration")
async def openid_configuration():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/oauth/authorize",
        "token_endpoint": f"{ISSUER}/oauth/token",
        "userinfo_endpoint": f"{ISSUER}/oauth/userinfo",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"],
        "scopes_supported": ["openid", "email", "profile"],
        "claims_supported": [
            "sub",
            "email",
            "email_verified",
            "name",
            "preferred_username",
        ],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
        ],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
