"""Entry point for Pterodactyl's generic Python egg (parkervcp/yolks:python_3.13),
which git-clones this repo, `pip install`s requirements.txt, and runs
`python main.py` directly -- it does not use the Dockerfile in this repo.

For any other deployment (plain Docker, docker-compose, manual `uv run`),
prefer `uvicorn app.main:app --workers 1` per the README instead; this file
exists only to satisfy that specific egg's expectations.
"""

from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("SERVER_PORT") or os.environ.get("PORT") or 8000)
    # Single worker is a hard requirement -- see app/services/drive_sync_service.py.
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, workers=1)
