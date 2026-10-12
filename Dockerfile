# pigeon — Nextcloud Talk bot -> pi agent webhook server
FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv ca-certificates git ripgrep curl \
    && rm -rf /var/lib/apt/lists/*

# pi coding agent (CLI)
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent

WORKDIR /app
# bake the git commit into the image (compose passes it as build arg,
# see docker-compose.yml / README)
ARG GIT_COMMIT=unknown
ENV PIGEON_COMMIT=$GIT_COMMIT
COPY requirements.txt .
# PEP 668: Debian python is externally managed -> use a venv
RUN python3 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir -r requirements.txt
COPY app.py wsgi.py .

# default paths (overridable via env / compose)
ENV BOTS_DIR=/bots \
    PI_CODING_AGENT_DIR=/pi-home/agent
RUN mkdir -p /bots /pi-home

EXPOSE 8080
# production WSGI server; single worker (in-process state), threaded
# -c python:wsgi registers wsgi.post_fork (gunicorn only picks hooks up
# from a -c config module, never from the WSGI module itself)
CMD ["/app/.venv/bin/gunicorn", "-c", "python:wsgi", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "32", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
