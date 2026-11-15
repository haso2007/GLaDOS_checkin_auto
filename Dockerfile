FROM ubuntu:24.04 AS builder
ARG DEBIAN_FRONTEND=noninteractive
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV PATH=/opt/venv/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/opt/camoufox-cache

RUN export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY"; \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt ./
RUN export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY"; \
    python3 -m venv /opt/venv \
    && pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -m camoufox fetch \
    && python -m camoufox version

FROM ubuntu:24.04 AS runtime
ARG DEBIAN_FRONTEND=noninteractive
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG APP_UID=10001
ARG APP_GID=10001
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app \
    XDG_CACHE_HOME=/opt/camoufox-cache \
    DISPLAY=:99 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8791 \
    GLADOS_DATA_DIR=/app/data \
    GLADOS_SECRETS_DIR=/app/secret

RUN export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY"; \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates dumb-init gosu python3 xvfb xauth \
      libasound2t64 libatk1.0-0t64 libavcodec60 \
      libcairo-gobject2 libcairo2 libdbus-1-3 \
      libfontconfig1 libfreetype6 libgdk-pixbuf-2.0-0 \
      libglib2.0-0 libgtk-3-0t64 libpango-1.0-0 \
      libpangocairo-1.0-0 libx11-6 libx11-xcb1 \
      libxcb-shm0 libxcb1 libxcomposite1 libxcursor1 \
      libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 \
      libxrender1 libxtst6 fonts-liberation fonts-noto-color-emoji \
    && groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --shell /bin/bash app \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --chown=app:app --from=builder /opt/venv /opt/venv
COPY --chown=app:app --from=builder /opt/camoufox-cache /opt/camoufox-cache
# Explicit modes prevent permission-less source files copied from NAS shares from breaking Python imports.
COPY --chown=app:app --chmod=644 glados.py logging_config.py browser_env.py browser_checkin.py turnstile.py config_store.py web_service.py ./
COPY --chown=app:app --chmod=644 web/ ./web/
COPY --chown=app:app --chmod=755 docker/entrypoint.sh ./docker/entrypoint.sh
RUN install -d -o app -g app /app/data /app/secret

VOLUME ["/app/data"]
EXPOSE 8791
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8791/api/health', timeout=3).read()"
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/app/docker/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "web_service:app", "--host", "0.0.0.0", "--port", "8791"]
