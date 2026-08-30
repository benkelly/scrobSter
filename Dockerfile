FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scrobster ./scrobster

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AUDIO_BACKEND=alsa \
    AUDIO_DEVICE=default \
    DB_PATH=/data/scrobster.db

# Runs as root by default. Home Assistant bind-mounts /data owned by root, and a
# bind mount keeps the host ownership, so an unprivileged user cannot create the
# database there. The add-on audio socket has the same problem.
# For an unprivileged standalone container, use a named volume and pass
# `--user 1000:1000 --group-add audio`. The scrobster user below owns /data, so
# a fresh named volume inherits an ownership that user can write.
RUN useradd --create-home --uid 1000 scrobster \
    && mkdir -p /data \
    && chown scrobster:scrobster /data
VOLUME /data
EXPOSE 8000

# `/` needs no token, unlike /api/*, so this works when API_TOKEN is set.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/', timeout=3)"

CMD ["python", "-m", "scrobster"]
