FROM python:3.12-slim-bookworm

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        dbus-x11 \
        fluxbox \
        fonts-liberation \
        fonts-noto-cjk \
        git \
        novnc \
        websockify \
        x11-utils \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${APP_GID}" workspace \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home workspace

WORKDIR /opt/workspace/mymcp

COPY requirements.txt /tmp/requirements.txt
COPY requirements-playwright.txt /tmp/requirements-playwright.txt
RUN pip install \
        --requirement /tmp/requirements.txt \
        --requirement /tmp/requirements-playwright.txt \
    && python -m playwright install --with-deps chromium \
    && chmod --recursive a+rX /ms-playwright

USER workspace
CMD ["python", "server.py"]
