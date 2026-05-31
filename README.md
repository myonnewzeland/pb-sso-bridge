# pb-sso-bridge

**Puente OAuth2 / OIDC entre tu SSO corporativo (SAML, headers de proxy, JWT interno, etc.) y PocketBase.**

PocketBase soporta proveedores OAuth2 personalizados (`oidc`, `oidc2`, `oidc3`). Este servicio expone los 4 endpoints estándar de OpenID Connect que PocketBase necesita y, por dentro, resuelve la identidad del usuario contra tu SSO real.

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

**Stack:** FastAPI + PocketBase, todo en `docker-compose.yml`.

> Estado (codes y tokens) en memoria. Funciona con **un solo proceso** (`--workers 1`). Si necesitas escalar horizontalmente, ver [Hardening](#hardening-pendiente-para-producción).

---

## Endpoints expuestos

Todo lo que PocketBase necesita de un proveedor OIDC genérico:

| Método | Ruta                                  | Descripción                                                |
|--------|---------------------------------------|------------------------------------------------------------|
| GET    | `/.well-known/openid-configuration`   | Discovery document. PocketBase puede autodescubrir desde aquí. |
| GET    | `/oauth/authorize`                    | Inicia el flujo. Lee la sesión SSO y emite un `code`.     |
| POST   | `/oauth/token`                        | Intercambia el `code` por un `access_token` (JWT HS256).  |
| GET    | `/oauth/userinfo`                     | Devuelve los claims del usuario (`sub`, `email`, `name`). |
| GET    | `/health`                             | Healthcheck para Docker/K8s.                              |

Implementado en `main.py`.

---

## Cómo resuelve la identidad

`get_sso_user()` lee tres headers que **debe inyectar tu reverse proxy SSO**:

| Header        | Mapea a claim   | Obligatorio |
|---------------|-----------------|-------------|
| `x-sso-id`    | `sub` / `id`    | sí          |
| `x-sso-email` | `email`         | sí          |
| `x-sso-name`  | `name`          | no          |

Patrones típicos para alimentar esos headers:

- **SAML**: tu ACS crea una cookie de sesión, un proxy (Traefik forward-auth, oauth2-proxy, Authelia, Authentik) las traduce a headers.
- **JWT interno** del SSO: middleware aquí mismo lo decodifica y rellena los headers.
- **Headers nativos** de un IdP delante (ADFS, Azure AD App Proxy, Cloudflare Access).

Si `get_sso_user()` devuelve `None` → `401`. En producción debes redirigir al login de tu SSO (ver TODO en `main.py`).

---

## Configuración en PocketBase

PocketBase admite hasta 3 proveedores OIDC genéricos (`oidc`, `oidc2`, `oidc3`).

### 1. Crea un cliente "OAuth2" en este bridge

Defínelo por variables de entorno (sirven como credenciales del cliente PocketBase):

```env
OAUTH_CLIENT_ID=pocketbase
OAUTH_CLIENT_SECRET=<genera-uno-largo>
JWT_SECRET=<genera-otro-largo>
ISSUER=https://sso-bridge.tudominio.com
```

### 2. Habilita OIDC en la colección de auth (UI)

`Settings → Auth providers → OpenID Connect (oidc)` y rellena:

| Campo                | Valor                                                  |
|----------------------|--------------------------------------------------------|
| Client ID            | `pocketbase` (igual a `OAUTH_CLIENT_ID`)              |
| Client Secret        | el de `OAUTH_CLIENT_SECRET`                            |
| Display name         | `Corporate SSO` (lo que se mostrará en el botón)      |
| Auth URL             | `https://sso-bridge.tudominio.com/oauth/authorize`    |
| Token URL            | `https://sso-bridge.tudominio.com/oauth/token`        |
| User info URL        | `https://sso-bridge.tudominio.com/oauth/userinfo`     |
| Support PKCE         | desactivado (este bridge no lo implementa)            |

> Alternativa: en algunos builds de PocketBase puedes pegar solo el discovery URL (`/.well-known/openid-configuration`) y autorrellena el resto.

### 3. Redirect URL en este bridge

PocketBase genera el `redirect_uri` automáticamente. Lo verás en la consola del browser durante el login; típicamente:

```
https://app.tudominio.com/api/oauth2-redirect
```

No necesitas registrarlo aquí (el bridge confía en lo que mande PocketBase, pero **valida** que coincida entre `/authorize` y `/token`).

---

## Flujo completo paso a paso

1. **Usuario** click en "Login with Corporate SSO" en tu app PocketBase.
2. **PB JS SDK** llama:
   ```js
   pb.collection('users').authWithOAuth2({ provider: 'oidc' })
   ```
   Eso abre `https://sso-bridge.tudominio.com/oauth/authorize?response_type=code&client_id=pocketbase&redirect_uri=...&state=...`.
3. **Bridge** (`/oauth/authorize`):
   - Valida `client_id` (constant-time).
   - Llama `get_sso_user()` → lee headers SSO.
   - Si no hay sesión → `401` (en prod: redirige al login SAML/SSO).
   - Genera `code` (token URL-safe de 48 bytes), lo guarda en memoria con TTL 5 min.
   - Redirige a `redirect_uri?code=...&state=...`.
4. **PocketBase** recibe el `code` y hace `POST /oauth/token` con `client_secret`.
5. **Bridge** (`/oauth/token`):
   - Valida `client_id` + `client_secret` + `redirect_uri` + expiración (todo constant-time).
   - `pop()` del code: single-use anti-replay.
   - Emite JWT HS256 firmado con `JWT_SECRET`, claims: `iss`, `sub`, `email`, `name`, `iat`, `exp`, `scope`.
   - Devuelve `{ access_token, token_type: "Bearer", expires_in: 3600 }`.
6. **PocketBase** llama `GET /oauth/userinfo` con `Authorization: Bearer <jwt>`.
7. **Bridge** verifica el JWT y responde con los claims OIDC estándar (`sub`, `email`, `email_verified`, `name`, `preferred_username`).
8. **PocketBase** crea o vincula el record en la colección `users` y emite **su propio JWT** al frontend.

---

## Frontend (PocketBase JS SDK)

```js
import PocketBase from 'pocketbase'

const pb = new PocketBase('https://app.tudominio.com')

// Lanza el flujo OIDC. PocketBase abre un popup hacia /oauth/authorize.
const authData = await pb.collection('users').authWithOAuth2({
  provider: 'oidc',
})

console.log(pb.authStore.isValid)  // true
console.log(pb.authStore.record)   // user record en PB
console.log(authData.meta)         // { email, name, accessToken, ... }
```

### Hook para mapear roles

En `pb_hooks/main.pb.js` puedes interceptar cada login OIDC:

```js
onRecordAuthWithOAuth2Request((e) => {
  // e.oauth2User.rawUser tiene los claims que devolviste en /userinfo
  const claims = e.oauth2User.rawUser

  if (e.isNewRecord) {
    e.record.set('role', claims.email.endsWith('@admin.com') ? 'admin' : 'user')
  }

  e.next()
}, 'users')
```

---

## Variables de entorno

| Variable              | Requerida | Descripción                                              |
|-----------------------|-----------|----------------------------------------------------------|
| `OAUTH_CLIENT_ID`     | sí        | ID del cliente que usa PocketBase.                      |
| `OAUTH_CLIENT_SECRET` | sí        | Secret del cliente (≥ 32 chars random).                 |
| `JWT_SECRET`          | sí        | Firma HS256 de los access tokens (≥ 32 chars random).   |
| `ISSUER`              | no        | `iss` claim. Default `http://localhost:8000`.           |

Genera secretos:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## Ejecutar

### Local (dev) con uv

Este proyecto usa [uv](https://docs.astral.sh/uv/) (Astral) como gestor de paquetes y entornos. Es 10–100× más rápido que `pip` y reproduce builds exactos vía `uv.lock`.

```bash
# Instala uv si no lo tienes (macOS/Linux)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Crea el venv y sincroniza deps desde uv.lock
uv sync

# Variables de entorno
cat > .env <<'EOF'
OAUTH_CLIENT_ID=pocketbase
OAUTH_CLIENT_SECRET=dev-secret-change-me
JWT_SECRET=dev-jwt-secret-change-me
ISSUER=http://localhost:8000
EOF

# Arranca con hot-reload
uv run uvicorn main:app --reload --port 8000
```

Comandos útiles de uv:

```bash
uv add httpx              # añadir dependencia
uv add --dev pytest       # añadir dependencia de desarrollo
uv remove PyJWT           # quitar dependencia
uv sync --upgrade         # actualizar todas las deps al rango permitido
uv lock                   # regenerar el lockfile
uv tree                   # ver árbol de dependencias
uv run python -V          # ejecutar algo dentro del venv sin activarlo
```

Smoke test:

```bash
curl -s http://localhost:8000/.well-known/openid-configuration | jq

curl -s "http://localhost:8000/oauth/authorize?response_type=code&client_id=pocketbase&redirect_uri=http://x/cb&state=abc" \
  -H "x-sso-id: u-1" -H "x-sso-email: ada@corp.com" -H "x-sso-name: Ada" -i
```

### Docker Compose (recomendado)

El `docker-compose.yml` levanta **bridge + PocketBase**.

1. Crea `.env`:

   ```bash
   cat > .env <<EOF
   OAUTH_CLIENT_ID=pocketbase
   OAUTH_CLIENT_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
   JWT_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')
   ISSUER=http://localhost:8000
   EOF
   ```

2. Levanta:

   ```bash
   docker compose up -d --build
   docker compose ps
   ```

   | Servicio          | Puerto host | Notas                                   |
   |-------------------|-------------|-----------------------------------------|
   | `pbsso-bridge`    | 8000        | FastAPI + endpoints OIDC                |
   | `pbsso-pocketbase`| 8090        | UI admin: http://localhost:8090/_/      |

3. Logs / apagar:

   ```bash
   docker compose logs -f bridge
   docker compose down -v
   ```

### Solo el bridge (sin compose)

```bash
docker build -t pb-sso-bridge:latest .
docker run -d --name pb-sso \
  -e OAUTH_CLIENT_ID=pocketbase \
  -e OAUTH_CLIENT_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))') \
  -e JWT_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))') \
  -e ISSUER=https://sso-bridge.tudominio.com \
  -p 8000:8000 \
  pb-sso-bridge:latest
```

---

## Hardening pendiente para producción

Ya implementado:

- `secrets.compare_digest` en `client_id` / `client_secret` / `redirect_uri` (anti timing attacks).
- Codes single-use (`pop` atómico dentro del proceso).
- Multi-stage Dockerfile, usuario no-root, healthcheck.

Pendiente:

1. **Estado en memoria → backend distribuido**
   `AUTH_CODES` y `ACCESS_TOKENS` viven en RAM del proceso. Esto implica:
   - Solo funciona con `--workers 1` y **una réplica**.
   - Reinicio del contenedor pierde flujos OAuth en curso.
   - Sin revocación realista de tokens.

   Cuando lo necesites: mover a Redis / Dragonfly / Valkey con `SET ... EX` y `GETDEL`.

2. **Login real cuando no hay sesión SSO**
   Hoy devuelve 401. Reemplaza por:
   ```python
   return RedirectResponse(f"https://sso.tudominio.com/login?return_to={request.url}")
   ```

3. **PKCE** — añadir soporte de `code_challenge`/`code_verifier` y activarlo en PocketBase.

4. **Rate limiting** en `/oauth/token` (slowapi o detrás del proxy).

5. **HTTPS obligatorio** y `Secure`/`HttpOnly` en cualquier cookie de sesión SSO.

6. **Rotación de `JWT_SECRET`** — soporta `kid` y publica `/.well-known/jwks.json` con RS256 si quieres ser un OP "de verdad".

7. **Logs estructurados** (structlog) y trazabilidad del `state`.

---

## Referencias

- PocketBase Authentication: https://pocketbase.io/docs/authentication
- PocketBase OAuth2 API: https://pocketbase.io/docs/api-records (sección `auth-with-oauth2`)
- PocketBase Hooks: `onRecordAuthWithOAuth2Request`
- OIDC Core 1.0: https://openid.net/specs/openid-connect-core-1_0.html
