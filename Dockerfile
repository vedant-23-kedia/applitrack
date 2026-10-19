# AppliTrack web service.
#
# Notes that matter for this app specifically:
#
# * ONE worker. The background scheduler and the Gmail watch renewal both
#   assume a single process -- two workers would double every sync and, as
#   this project learned the hard way, that alone is enough to trip Gmail's
#   per-user rate limit. Concurrency comes from threads instead.
# * All state lives under /data, which is a mounted volume. The container
#   filesystem is replaced on every deploy; the volume is not.
# * No credentials are copied in. The OAuth client arrives as an environment
#   variable at runtime (APPLITRACK_CLIENT_SECRETS_JSON).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# lxml needs a compiler toolchain; drop it again so it does not ship.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libxml2-dev libxslt1-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn \
 && apt-get purge -y build-essential libxml2-dev libxslt1-dev \
 && apt-get autoremove -y

COPY applitrack/ ./applitrack/
COPY pyproject.toml README.md SERVICE.md ./

# Persistent state: both databases and the sync lock files.
ENV APPLITRACK_DB=/data/applitrack.db \
    APPLITRACK_USERS_DB=/data/users.db \
    APPLITRACK_LOCK_DIR=/data/locks \
    APPLITRACK_HTTPS=1 \
    APPLITRACK_SCHEDULER=1 \
    PORT=8080

RUN useradd --create-home --uid 10001 appli \
 && mkdir -p /data \
 && chown -R appli:appli /app /data
USER appli

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=8).status==200 else 1)"

CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "applitrack.web.app:create_app()"]
