# pb-sso-bridge

> Puente OAuth2 / OIDC entre tu **SSO corporativo** (SAML, headers de proxy, JWT interno…) y **PocketBase**.

[![CI](https://github.com/myonnewzeland/pb-sso-bridge/actions/workflows/security-scan.yml/badge.svg)](https://github.com/myonnewzeland/pb-sso-bridge/actions/workflows/security-scan.yml)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue)
![Distroless](https://img.shields.io/badge/runtime-distroless-success)
![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey)

PocketBase soporta proveedores OAuth2 personalizados (`oidc`, `oidc2`, `oidc3`). Este servicio expone los **4 endpoints estándar de OpenID Connect** que PocketBase necesita y, por dentro, resuelve la identidad del usuario contra tu SSO real.

```
┌──────────┐    1. login    ┌─────────────────┐   2. /authorize   ┌──────────────┐
│ Browser  │ ─────────────▶ │   PocketBase    │ ────────────────▶ │ pb-sso-bridge│
│          │                │ (oidc provider) │                   │  (FastAPI)   │
└──────────┘                └─────────────────┘                   └──────┬───────┘
      ▲                              ▲                                   │
      │ 5. JWT PB                    │ 4. /token + /userinfo             │ 3. lee SSO
      │                              │                                   ▼
      │                              └────────────────────────── ┌──────────────┐
      └──────────────────────────────────────────────────────────│  SSO real    │
                                                                 │ (SAML, proxy │
                                                                 │  headers...) │
                                                                 └──────────────┘
```

> ℹ️ **PocketBase es externo a este repo.** Aquí solo vive el bridge. Apunta tu instancia existente de PocketBase a esta API.

---

## Tabla de contenidos

1. [Características](#características)
2. [Endpoints](#endpoints)
3. [Cómo resuelve la identidad](#cómo-resuelve-la-identidad)
4. [Configuración en PocketBase](#configuración-en-pocketbase)
5. [Flujo paso a paso](#flujo-paso-a-paso)
6. [Variables de entorno](#variables-de-entorno)
7. [Ejecutar](#ejecutar)
   - [Local con uv](#local-dev-con-uv)
   - [Docker](#docker-recomendado)
   - [Docker Compose](#docker-compose)
8. [Frontend (PocketBase JS SDK)](#frontend-pocketbase-js-sdk)
9. [Seguridad](#seguridad)
10. [CI/CD](#cicd)
11. [Hardening pendiente](#hardening-pendiente-para-producción)
12. [Estructura del repo](#estructura-del-repo)
13. [Referencias](#referencias)

---

## Características

| | |
|---|---|
| **Lenguaje** | Python 3.13 |
| **Framework** | FastAPI 0.136+ con uvicorn[standard] |
| **Gestor de paquetes** | [uv](https://docs.astral.sh/uv/) (Astral) — lockfile reproducible |
| **Runtime** | Distroless (`gcr.io/distroless/python3-debian13:nonroot`) — sin shell, sin apt, uid 65532 |
| **Tamaño imagen** | ~112 MB |
| **Estado** | En memoria (single-process). Para multi-réplica: ver [hardening](#hardening-pendiente-para-producción) |
| **Tokens** | JWT HS256 con expiración 1 h, codes single-use con TTL 5 min |
| **CI/CD** | Trivy (vuln + misconfig + secrets) y Dependabot configurados |

---

## Endpoints

| Método | Ruta                                  | Descripción                                                |
|--------|---------------------------------------|------------------------------------------------------------|
| GET    | `/.well-known/openid-configuration`   | Discovery document. PocketBase puede autodescubrir desde aquí. |
| GET    | `/oauth/authorize`                    | Inicia el flujo. Lee la sesión SSO y emite un `code`.     |
| POST   | `/oauth/token`                        | Intercambia el `code` por un `access_token` (JWT HS256).  |
| GET    | `/oauth/userinfo`                     | Devuelve los claims del usuario (`sub`, `email`, `name`). |
| GET    | `/health`                             | Healthcheck para Docker / Kubernetes.                     |

Implementación completa en [`main.py`](main.py).

---

## Cómo resuelve la identidad

`get_sso_user()` lee tres headers que **debe inyectar tu reverse proxy SSO**:

| Header        | Mapea a claim   | Obligatorio |
|---------------|-----------------|-------------|
| `x-sso-id`    | `sub` / `id`    | sí          |
| `x-sso-email` | `email`         | sí          |
| `x-sso-name`  | `name`          | no          |

Patrones típicos para alimentar esos headers:

- **SAML** — tu ACS crea una cookie de sesión, un proxy (Traefik forward-auth, oauth2-proxy, Authelia, Authentik) la traduce a headers.
- **JWT interno** — middleware aquí mismo lo decodifica y rellena los headers.
- **Headers nativos** de un IdP delante (ADFS, Azure AD App Proxy, Cloudflare Access).

Si `get_sso_user()` devuelve `None` → `401`. En producción debes redirigir al login de tu SSO (ver TODO en `main.py`).

---

## Configuración en PocketBase

PocketBase admite hasta 3 proveedores OIDC genéricos (`oidc`, `oidc2`, `oidc3`).

### 1. Define el cliente OAuth2 en este bridge

Vía variables de entorno (sirven como credenciales del cliente que usará PocketBase):

```env
OAUTH_CLIENT_ID=pocketbase
OAUTH_CLIENT_SECRET=<token-urlsafe-48>
JWT_SECRET=<token-urlsafe-48>
ISSUER=https://sso-bridge.tudominio.com
```

### 2. Habilita OIDC en la colección de auth (UI de PocketBase)

`Settings → Auth providers → OpenID Connect (oidc)` y rellena:

| Campo          | Valor                                                  |
|----------------|--------------------------------------------------------|
| Client ID      | `pocketbase` (= `OAUTH_CLIENT_ID`)                     |
| Client Secret  | el de `OAUTH_CLIENT_SECRET`                            |
| Display name   | `Corporate SSO` (texto del botón)                      |
| Auth URL       | `https://sso-bridge.tudominio.com/oauth/authorize`     |
| Token URL      | `https://sso-bridge.tudominio.com/oauth/token`         |
| User info URL  | `https://sso-bridge.tudominio.com/oauth/userinfo`      |
| Support PKCE   | desactivado (este bridge aún no implementa PKCE)       |

> En algunos builds de PocketBase puedes pegar solo el discovery URL (`/.well-known/openid-configuration`) y autorrellena el resto.

### 3. Redirect URL

PocketBase genera el `redirect_uri` automáticamente — típicamente `https://app.tudominio.com/api/oauth2-redirect`. No necesitas registrarlo aquí, pero el bridge **valida** que coincida entre `/authorize` y `/token`.

---

## Flujo paso a paso

1. **Usuario** click en "Login with Corporate SSO" en tu app PocketBase.
2. **PB JS SDK** llama `pb.collection('users').authWithOAuth2({ provider: 'oidc' })`, que abre `https://sso-bridge.tudominio.com/oauth/authorize?response_type=code&client_id=pocketbase&redirect_uri=...&state=...`.
3. **Bridge** (`/oauth/authorize`):
   - Valida `client_id` con `secrets.compare_digest` (constant-time).
   - `get_sso_user()` lee headers SSO. Si no hay sesión → `401`.
   - Genera `code` (`secrets.token_urlsafe(48)`), lo guarda con TTL 5 min.
   - Redirige a `redirect_uri?code=...&state=...`.
4. **PocketBase** intercambia el `code` con `POST /oauth/token` enviando `client_secret`.
5. **Bridge** (`/oauth/token`):
   - Valida `client_id` + `client_secret` + `redirect_uri` (constant-time).
   - `pop()` del code → single-use anti-replay.
   - Emite JWT HS256 con claims `iss`, `sub`, `email`, `name`, `iat`, `exp`, `scope`.
   - Devuelve `{ access_token, token_type: "Bearer", expires_in: 3600 }`.
6. **PocketBase** llama `GET /oauth/userinfo` con `Authorization: Bearer <jwt>`.
7. **Bridge** verifica el JWT y devuelve los claims OIDC: `sub`, `email`, `email_verified`, `name`, `preferred_username`.
8. **PocketBase** crea o vincula el record en `users` y emite **su propio JWT** al frontend.

---

## Variables de entorno

| Variable              | Requerida | Default                  | Descripción                                              |
|-----------------------|:---------:|--------------------------|----------------------------------------------------------|
| `OAUTH_CLIENT_ID`     | sí        | —                        | ID del cliente que usa PocketBase.                       |
| `OAUTH_CLIENT_SECRET` | sí        | —                        | Secret del cliente (≥ 32 chars random).                  |
| `JWT_SECRET`          | sí        | —                        | Firma HS256 de los access tokens (≥ 32 chars random).    |
| `ISSUER`              | no        | `http://localhost:8000`  | `iss` claim del JWT.                                     |

Genera secretos:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## Ejecutar

### Local (dev) con uv

Este proyecto usa [uv](https://docs.astral.sh/uv/) (Astral) — 10–100× más rápido que `pip` y reproducible vía `uv.lock`.

```bash
# 1. Instalar uv (macOS / Linux)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Sincronizar deps desde uv.lock
uv sync

# 3. Variables de entorno
cp .env.example .env
# edita .env con secretos reales

# 4. Arrancar con hot-reload
uv run uvicorn main:app --reload --port 8000
```

Comandos útiles de `uv`:

```bash
uv add httpx              # añadir dependencia
uv add --dev pytest       # añadir dependencia de desarrollo
uv remove PyJWT           # quitar dependencia
uv sync --upgrade         # actualizar al rango permitido y regenerar lock
uv tree                   # árbol de dependencias
uv run python -V          # ejecutar dentro del venv sin activarlo
```

Smoke test:

```bash
curl -s http://localhost:8000/.well-known/openid-configuration | jq

curl -s "http://localhost:8000/oauth/authorize?response_type=code&client_id=pocketbase&redirect_uri=http://x/cb&state=abc" \
  -H "x-sso-id: u-1" -H "x-sso-email: ada@corp.com" -H "x-sso-name: Ada" -i
```

### Docker (recomendado)

```bash
docker build -t pb-sso-bridge:latest .

docker run -d --name pb-sso \
  -p 8000:8000 \
  -e OAUTH_CLIENT_ID=pocketbase \
  -e OAUTH_CLIENT_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  -e JWT_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  -e ISSUER=https://sso-bridge.tudominio.com \
  pb-sso-bridge:latest
```

### Docker Compose

```bash
cp .env.example .env
# edita .env con secretos reales

docker compose up -d --build
docker compose ps
docker compose logs -f bridge
```

| Servicio       | Puerto host | Notas                                  |
|----------------|-------------|----------------------------------------|
| `pbsso-bridge` | 8000        | FastAPI + endpoints OIDC               |

> PocketBase se ejecuta por separado. Apunta tu instancia existente a `http://<host>:8000`.

---

## Frontend (PocketBase JS SDK)

```js
import PocketBase from 'pocketbase'

const pb = new PocketBase('https://app.tudominio.com')

const authData = await pb.collection('users').authWithOAuth2({
  provider: 'oidc',
})

console.log(pb.authStore.isValid)  // true
console.log(pb.authStore.record)   // user record en PB
console.log(authData.meta)         // { email, name, accessToken, ... }
```

### Hook para mapear roles

En `pb_hooks/main.pb.js` (lado PocketBase):

```js
onRecordAuthWithOAuth2Request((e) => {
  const claims = e.oauth2User.rawUser

  if (e.isNewRecord) {
    e.record.set('role', claims.email.endsWith('@admin.com') ? 'admin' : 'user')
  }

  e.next()
}, 'users')
```

---

## Seguridad

### Implementado

- **Distroless runtime** — sin shell, sin apt, sin pip. Reduce drásticamente la superficie de ataque.
- **Usuario no-root** — uid 65532 (`nonroot`), compatible con Pod Security Standards `restricted`.
- **`secrets.compare_digest`** en `client_id`, `client_secret`, `redirect_uri` (resistente a timing attacks).
- **Codes single-use** vía `pop()` atómico (anti-replay).
- **JWT con `algorithms=["HS256"]` explícito** y validación de `iss` (anti algorithm-confusion).
- **Cliente Docker pin por SHA256** (`uv` y base distroless).
- **`uv.lock` con 33 paquetes lockeados** (incluye transitivas + hashes).

### Resultados de escaneo (Trivy)

| Target | Vulns | Misconfig | Secrets |
|--------|------:|----------:|--------:|
| `uv.lock` (deps Python) | **0** | — | — |
| `Dockerfile` | — | **0** | — |
| Imagen final · libs Python | **0** | — | — |
| Imagen final · paquetes SO | 4 C / 5 H | — | 0 |

> Las CVEs CRITICAL/HIGH del SO vienen del **base distroless** (libpython, libexpat, libncurses) y no tienen fix upstream aún. Se corrigen en cuanto Google rebuilda la imagen base; Dependabot abrirá el PR automáticamente.

---

## CI/CD

### `.github/dependabot.yml`

Actualizaciones automáticas semanales para:
- **uv** (deps Python) — agrupa minor + patch en un solo PR.
- **docker** (imagen base + uv pinneado por SHA).
- **github-actions** (acciones del workflow) — mensual.

### `.github/workflows/security-scan.yml`

En cada push a `main`, cada PR y semanalmente (cron lunes 06:00 UTC):

1. **Trivy filesystem** — escanea `uv.lock`, `Dockerfile` y secretos en el repo.
2. **Trivy image** — construye la imagen y escanea por CVEs (CRITICAL/HIGH con fix disponible bloquean el merge).
3. **Snyk** (opcional) — solo si configuras `SNYK_TOKEN` en `Settings → Secrets`.

Resultados publicados como **SARIF** en la pestaña **Security → Code scanning**.

---

## Hardening pendiente para producción

1. **Estado en memoria → backend distribuido**
   `AUTH_CODES` y `ACCESS_TOKENS` viven en RAM del proceso → solo `--workers 1`, sin réplicas, reinicio pierde flujos en curso.
   Mover a Redis / Dragonfly / Valkey con `SET ... EX` y `GETDEL`.

2. **Login real cuando no hay sesión SSO**
   Hoy devuelve `401`. Reemplazar por redirección al login del IdP:
   ```python
   return RedirectResponse(f"https://sso.tudominio.com/login?return_to={request.url}")
   ```

3. **PKCE** — soportar `code_challenge` / `code_verifier` y activarlo en PocketBase.

4. **Rate limiting** en `/oauth/token` (slowapi o detrás del proxy).

5. **HTTPS obligatorio** y `Secure` / `HttpOnly` en cualquier cookie de sesión SSO.

6. **RS256 + JWKS** — rotación de `JWT_SECRET` con `kid` y publicación de `/.well-known/jwks.json` para ser un OP "de verdad".

7. **Logs estructurados** (structlog) y trazabilidad del `state`.

---

## Estructura del repo

```
pb-sso-bridge/
├── .github/
│   ├── dependabot.yml             # Updates automáticos: uv + docker + actions
│   └── workflows/
│       └── security-scan.yml      # Trivy fs + image (+ Snyk opcional)
├── .dockerignore
├── .env.example                   # Plantilla de variables (copiar a .env)
├── .gitignore                     # Excluye .env, venv, __pycache__, etc.
├── Dockerfile                     # Multi-stage: uv-bin → deps → app → distroless
├── README.md                      # Este archivo
├── docker-compose.yml             # Solo el bridge (PocketBase es externo)
├── main.py                        # FastAPI + endpoints OIDC
├── pyproject.toml                 # PEP 621 + grupos dev
└── uv.lock                        # Lockfile reproducible (33 paquetes)
```

---

## Referencias

- **PocketBase Authentication** — https://pocketbase.io/docs/authentication
- **PocketBase OAuth2 API** — https://pocketbase.io/docs/api-records (sección `auth-with-oauth2`)
- **PocketBase Hooks** — `onRecordAuthWithOAuth2Request`
- **OIDC Core 1.0** — https://openid.net/specs/openid-connect-core-1_0.html
- **uv (Astral)** — https://docs.astral.sh/uv/
- **Distroless images** — https://github.com/GoogleContainerTools/distroless

---

## Licencia

MIT. Ver [`LICENSE`](LICENSE) (pendiente de añadir).
