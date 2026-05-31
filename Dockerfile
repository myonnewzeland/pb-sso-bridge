# syntax=docker/dockerfile:1.7

# ────────────────────────────────────────────────────────────────
#  pb-sso-bridge · uv + Distroless Python 3.13 (Debian 13)
#
#  Runtime: gcr.io/distroless/python3-debian13:nonroot
#    • ENTRYPOINT = ["/usr/bin/python3.13"]  (fijo en la imagen)
#    • Sin shell, sin apt, sin pip.
#    • Corre como uid 65532 (nonroot).
#
#  Build:   docker build -t pb-sso-bridge:latest .
#  Run:     docker run -p 8000:8000 --env-file .env pb-sso-bridge
# ────────────────────────────────────────────────────────────────


# ── Stage 0: binario de uv ─────────────────────────────────────
# Pin por SHA256 (best practice oficial de Astral · Trivy DS-0001).
# Equivale a ghcr.io/astral-sh/uv:0.9.22 (multi-arch).
FROM ghcr.io/astral-sh/uv@sha256:2320e6c239737dc73cccce393a8bb89eba2383d17018ee91a59773df802c20e6 AS uv-bin


# ── Stage 1: deps ──────────────────────────────────────────────
# Resolve e instala dependencias en /app/.venv usando uv.
FROM python:3.13-slim-bookworm AS deps

COPY --from=uv-bin /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy     \
    UV_NO_DEV=1           \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project


# ── Stage 2: app ───────────────────────────────────────────────
# Sincroniza el proyecto encima del venv ya cacheado.
FROM deps AS app

COPY main.py ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


# ── Stage 3: runtime distroless ────────────────────────────────
FROM gcr.io/distroless/python3-debian13:nonroot AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1        \
    PYTHONFAULTHANDLER=1      \
    PYTHONPATH=/app/.venv/lib/python3.13/site-packages \
    WEB_CONCURRENCY=1

WORKDIR /app

COPY --from=app --chown=nonroot:nonroot /app /app

USER nonroot

EXPOSE 8000
STOPSIGNAL SIGINT

# Healthcheck (Trivy DS-0026). El ENTRYPOINT de la base es python3.13,
# así que el HEALTHCHECK CMD recibe argumentos para el intérprete.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"]

# La imagen base ya tiene ENTRYPOINT=["/usr/bin/python3.13"].
# CMD aporta solo los argumentos → invocación final:
#   /usr/bin/python3.13 -m uvicorn main:app ...
CMD ["-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*", \
     "--workers", "1"]
