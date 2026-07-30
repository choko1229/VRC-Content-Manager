#!/bin/sh
set -e

# Single worker is a hard requirement: the SQLite<->Google Drive sync design
# (app/services/drive_sync_service.py) assumes one process holds the local
# SQLite file. Bootstrap/migration logic lives in the FastAPI lifespan
# (app/main.py), not here, since a fresh DB may require interactive OAuth.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
