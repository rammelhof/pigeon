# pigeon — Nextcloud Talk bot -> pi agent webhook server
FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-pip ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/*

# pi coding agent (CLI)
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# default paths (overridable via env / compose)
ENV BOTS_DIR=/bots \
    PI_CODING_AGENT_DIR=/pi-home/agent
RUN mkdir -p /bots /pi-home

EXPOSE 8080
CMD ["python3", "app.py"]
