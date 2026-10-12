"""gunicorn entrypoint (production server for pigeon).

Run (as the Dockerfile CMD):
    gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 32 wsgi:app

`--workers 1` matters: pigeon keeps its state (task registry, room queues,
heartbeat) in process memory, and post_fork runs startup() exactly once
per worker — after gunicorn's own signal setup, so pigeon's fast-shutdown
handler (kill active pi runs on SIGTERM) stays in charge.
"""
from app import app, startup  # noqa: F401 (app is the WSGI object)


def post_fork(server, worker):
    startup()
