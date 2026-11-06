# The decisions server: GUI + API + webhook dispatcher, and the installer for agent machines.
# Resumers don't belong here: they run on the machines where the agent sessions live.
#
#   docker build -t decisions .
#   docker run -d -p 8787:8787 -v decisions-data:/data --add-host=host.docker.internal:host-gateway \
#     -e DECISIONS_TOKEN=... -e DECISIONS_BASE_URL=http://server:8787 decisions
# --add-host lets webhooks reach agents on this same machine on Linux (Docker Desktop has it built in).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DECISIONS_HOST=0.0.0.0 \
    DECISIONS_PORT=8787 \
    DECISIONS_DB=/data/decisions.db \
    DECISIONS_CALLBACK_HOST=host.docker.internal

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY decisions ./decisions
RUN pip install --no-cache-dir . && rm -rf /src

RUN useradd --system --uid 10001 --home-dir /data decisions \
    && mkdir -p /data && chown decisions /data
USER decisions
WORKDIR /data
VOLUME /data
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"DECISIONS_PORT\"]}/api/health', timeout=4)"]

# Python turns SIGINT into a clean shutdown (stop the dispatcher, close the database).
STOPSIGNAL SIGINT
CMD ["decide", "serve"]
