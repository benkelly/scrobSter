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

# Run unprivileged. Reading a sound card also needs the host audio group, which
# `docker run --group-add audio` supplies.
RUN useradd --create-home --uid 1000 scrobster \
    && mkdir -p /data \
    && chown scrobster:scrobster /data
VOLUME /data
USER scrobster
EXPOSE 8000

# `/` needs no token, unlike /api/*, so this works when API_TOKEN is set.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/', timeout=3)"

CMD ["python", "-m", "scrobster"]
