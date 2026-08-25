FROM oven/bun:1 AS frontend-build

WORKDIR /app

COPY package.json bun.lock /app/
COPY frontend/package.json /app/frontend/

WORKDIR /app/frontend

RUN bun install

COPY ./frontend /app/frontend

ARG VITE_API_URL=

RUN bun run build


FROM python:3.12

ENV PYTHONUNBUFFERED=1

# FFmpeg is invoked as a bounded subprocess for voice normalization. Preserve
# the exact package build/configuration inside every image for release notices.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && mkdir -p /usr/share/doc/nightingale \
    && ffmpeg -version > /usr/share/doc/nightingale/ffmpeg-build.txt \
    && apt-get clean \
    && find /var/lib/apt/lists -mindepth 1 -delete

# Install uv
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#installing-uv
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/

# Compile bytecode
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#compiling-bytecode
ENV UV_COMPILE_BYTECODE=1

# uv Cache
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#caching
ENV UV_LINK_MODE=copy

# The default synthetic demo stays model-free. Production remote egress opts
# into the locked local Presidio model at image build time.
ARG INSTALL_PRESIDIO_NLP=false
ARG INSTALL_LOCAL_ASR=false
ARG INSTALL_DIARIZATION=false

WORKDIR /app/

# Place executables in the environment at the front of the path
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#using-the-environment
ENV PATH="/app/.venv/bin:$PATH"

# Install dependencies from the committed workspace lock. COPY rather than
# RUN --mount keeps the image build compatible with Docker installations that
# do not ship the optional buildx/BuildKit plugin.
COPY ./pyproject.toml ./uv.lock /app/
COPY ./backend/pyproject.toml /app/backend/pyproject.toml
RUN set -eu; \
    set -- uv sync --frozen --no-install-workspace --package app; \
    if [ "$INSTALL_PRESIDIO_NLP" = "true" ]; then set -- "$@" --group presidio-nlp; fi; \
    if [ "$INSTALL_LOCAL_ASR" = "true" ]; then set -- "$@" --group local-asr; fi; \
    if [ "$INSTALL_DIARIZATION" = "true" ]; then set -- "$@" --group diarization; fi; \
    "$@"

COPY ./LICENSE ./ATTRIBUTION.txt ./THIRD_PARTY_NOTICES.md ./MODEL_INVENTORY.md \
  /usr/share/doc/nightingale/
COPY ./THIRD_PARTY_LICENSES /usr/share/doc/nightingale/THIRD_PARTY_LICENSES
RUN test -s /usr/share/doc/nightingale/LICENSE \
    && test -s /usr/share/doc/nightingale/ATTRIBUTION.txt \
    && test -s /usr/share/doc/nightingale/THIRD_PARTY_LICENSES/Serene-Comment-Extension-MIT.txt \
    && test -s /usr/share/doc/nightingale/THIRD_PARTY_LICENSES/Tiptap-MIT.txt \
    && test -s /usr/share/doc/nightingale/THIRD_PARTY_LICENSES/idb-ISC.txt \
    && test -s /usr/share/doc/nightingale/THIRD_PARTY_LICENSES/CTranslate2-MIT.txt \
    && test -s /usr/share/doc/nightingale/THIRD_PARTY_LICENSES/PyAV-BSD-3-Clause.txt

COPY ./backend/scripts /app/backend/scripts

COPY ./backend/alembic.ini /app/backend/

COPY ./backend/app /app/backend/app

COPY --from=frontend-build /app/backend/app/frontend /app/backend/app/frontend

# Sync the project itself after source is present. Dependencies are already in
# the preceding layer and uv still verifies the same frozen lock.
RUN set -eu; \
    set -- uv sync --frozen --package app; \
    if [ "$INSTALL_PRESIDIO_NLP" = "true" ]; then set -- "$@" --group presidio-nlp; fi; \
    if [ "$INSTALL_LOCAL_ASR" = "true" ]; then set -- "$@" --group local-asr; fi; \
    if [ "$INSTALL_DIARIZATION" = "true" ]; then set -- "$@" --group diarization; fi; \
    "$@"

ARG NIGHTINGALE_SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.revision=$NIGHTINGALE_SOURCE_COMMIT

WORKDIR /app/backend/

CMD ["fastapi", "run", "--workers", "4"]
