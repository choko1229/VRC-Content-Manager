FROM python:3.13-slim

RUN pip install --no-cache-dir uv

WORKDIR /srv/app

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker-entrypoint.sh ./

RUN uv sync --no-dev \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /srv/app /data \
    && chmod +x docker-entrypoint.sh

USER appuser

ENV PATH="/srv/app/.venv/bin:${PATH}" \
    DATA_DIR=/data \
    PORT=8000

EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["./docker-entrypoint.sh"]
