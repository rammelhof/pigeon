# pigeon — Nextcloud Talk bot -> pi agent webhook server
FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
# core runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv python3-dev ca-certificates git \
        ripgrep curl wget bubblewrap \
    && rm -rf /var/lib/apt/lists/*

# tool belt for the agent — one RUN per group so a build failure names the
# offending layer. NOTE: office/media make the image several GB.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        less nano neovim htop tmux tree file \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        unzip zip rsync xz-utils 7zip p7zip-full unrar-free \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        jq yq pandoc dos2unix xmlstarlet fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential cmake pkg-config \
        libssl-dev zlib1g-dev libffi-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        httpie openssh-client axel netcat-openbsd dnsutils lsof \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        sqlite3 libsqlite3-dev redis-tools \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils ghostscript qpdf \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-deu \
        hunspell hunspell-en-us hunspell-de-at \
        fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer libreoffice-calc libreoffice-impress \
        imagemagick ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gh \
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
COPY pigeon/ /app/pigeon/
COPY wsgi.py .
# bundled pi skills, copied into $PI_CODING_AGENT_DIR/skills at startup
COPY skills/ /app/skills/

# default paths (overridable via env / compose)
ENV BOTS_DIR=/bots \
    PI_CODING_AGENT_DIR=/pi-home/agent
RUN mkdir -p /bots /pi-home

EXPOSE 8080
# production WSGI server; single worker (in-process state), threaded
# -c python:wsgi registers wsgi.post_fork (gunicorn only picks hooks up
# from a -c config module, never from the WSGI module itself)
CMD ["/app/.venv/bin/gunicorn", "-c", "python:wsgi", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "32", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
