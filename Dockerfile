FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 workspace \
    && useradd --uid 1000 --gid 1000 --create-home workspace

WORKDIR /opt/workspace/mymcp

COPY requirements.txt /tmp/requirements.txt
RUN pip install --requirement /tmp/requirements.txt

USER workspace
CMD ["python", "server.py"]
