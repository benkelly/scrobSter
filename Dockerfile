# PyPI has no 32-bit ARM wheels for numpy or shazamio-core, so a linux/arm/v7
# build compiles them here. shazamio-core is Rust, and its audio crate links
# ALSA. Other platforms leave /wheels empty and install from PyPI as before.
FROM python:3.12-slim AS wheels
ARG TARGETARCH
ARG TARGETVARIANT
COPY requirements.txt .
RUN mkdir /wheels \
    && if [ "${TARGETARCH}/${TARGETVARIANT}" = "arm/v7" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends \
            build-essential cargo pkg-config libasound2-dev \
        && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt; \
    fi

FROM python:3.12-slim

# libasound2-plugins provides libasound_module_pcm_pulse.so. Home Assistant
# mounts an /etc/asound.conf that routes ALSA to PulseAudio, and ALSA cannot
# load that route without this plugin.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libasound2-plugins \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# The wheels are mounted, not copied, so they and the compilers stay out of
# the image.
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    pip install --no-cache-dir --find-links /wheels -r requirements.txt
COPY scrobster ./scrobster

# Do not set AUDIO_BACKEND or AUDIO_DEVICE here. A real environment variable
# outranks /data/options.json, so setting them would silently ignore the Home
# Assistant add-on options. The defaults in config.py already give alsa on Linux.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
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
