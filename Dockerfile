# PyPI has no 32-bit ARM wheel for shazamio-core, so a linux/arm/v7 build
# compiles it here. shazamio-core is Rust, and it builds the libopus it bundles
# with CMake and a C compiler. Other platforms leave /wheels empty and install
# from PyPI as before.
# shazamio-core 2 declares rust-version 1.87, newer than Debian's rustc, so
# rustup installs it. rustup-init is a pinned release whose checksum is checked
# before it runs, from
# https://static.rust-lang.org/rustup/archive/<version>/armv7-unknown-linux-gnueabihf/rustup-init.sha256
# The build backend, maturin, is pinned in build-constraints.txt.
# libopus is built without SIMD intrinsics: its CMake compiles the NEON files
# without -mfpu=neon on 32-bit ARM, so they fail against Rust's default
# -mfpu=vfpv3-d16. Opus only decodes short browser clips, so speed is no matter,
# and the result runs on ARMv7 CPUs without NEON too.
FROM python:3.12-slim AS wheels
ARG TARGETARCH
ARG TARGETVARIANT
ARG RUSTUP_VERSION=1.29.2
ARG RUSTUP_SHA256=fb8cc6c0ff713fd405a6940c80138516da9b75e6649e280cb65342afa3dcf188
ENV PATH="/root/.cargo/bin:${PATH}"
COPY requirements.txt build-constraints.txt ./
RUN mkdir /wheels \
    && if [ "${TARGETARCH}/${TARGETVARIANT}" = "arm/v7" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends \
            build-essential ca-certificates cmake curl \
        && curl --proto '=https' --tlsv1.2 -sSfo /tmp/rustup-init \
            "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/armv7-unknown-linux-gnueabihf/rustup-init" \
        && echo "${RUSTUP_SHA256}  /tmp/rustup-init" | sha256sum -c - \
        && chmod +x /tmp/rustup-init \
        && /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain 1.87.0 \
        && rm /tmp/rustup-init \
        && echo 'set(OPUS_DISABLE_INTRINSICS ON CACHE BOOL "" FORCE)' > /tmp/opus.cmake \
        && CMAKE_TOOLCHAIN_FILE_armv7_unknown_linux_gnueabihf=/tmp/opus.cmake \
            PIP_CONSTRAINT=/build-constraints.txt \
            pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt; \
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
