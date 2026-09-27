FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    APP_DEBUG=false \
    KZHOME_SIMULATOR_ENABLED=false

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install -r requirements.txt \
    && groupadd --gid 10001 kzhome \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin kzhome

# Explicit copies keep tests, local configuration, and developer tools out.
COPY app/ ./app/
# Imported by app.main even when the simulator is disabled in production.
COPY simulator/ ./simulator/
# Required for readiness and explicit, separately invoked migrations.
COPY alembic/ ./alembic/
COPY alembic.ini ./

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); r.close()"]

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
