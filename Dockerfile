# syntax=docker/dockerfile:1

# Keep the Writer image as the canonical AI/development workstation base.
# WebCodex and future tooling can inherit this image and only add their own binaries.
FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Reuse the official Node 22 runtime without changing the Python base image.
COPY --from=node-runtime /usr/local/ /usr/local/

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        clangd \
        curl \
        dbus-x11 \
        fd-find \
        fluxbox \
        fonts-liberation \
        fonts-noto-cjk \
        git \
        make \
        novnc \
        openssh-client \
        pkg-config \
        procps \
        ripgrep \
        tar \
        tini \
        websockify \
        x11-utils \
        x11vnc \
        xvfb \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && npm install --global \
        pyright \
        typescript \
        typescript-language-server \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${APP_GID}" workspace \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home workspace

WORKDIR /opt/workspace

COPY requirements.txt /tmp/requirements.txt
COPY requirements-playwright.txt /tmp/requirements-playwright.txt
RUN pip install \
        --requirement /tmp/requirements.txt \
        --requirement /tmp/requirements-playwright.txt \
    && python -m playwright install --with-deps chromium \
    && chmod --recursive a+rX /ms-playwright

USER workspace
CMD ["sh", "-c", "cd \"/opt/workspace/${MCP_PROJECT_RELATIVE_PATH:-mymcp}\" && exec python server.py"]
