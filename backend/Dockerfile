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

WORKDIR /app/

# Place executables in the environment at the front of the path
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#using-the-environment
ENV PATH="/app/.venv/bin:$PATH"

# Install dependencies from the committed workspace lock. COPY rather than
# RUN --mount keeps the image build compatible with Docker installations that
# do not ship the optional buildx/BuildKit plugin.
COPY ./pyproject.toml ./uv.lock /app/
COPY ./backend/pyproject.toml /app/backend/pyproject.toml
RUN if [ "$INSTALL_PRESIDIO_NLP" = "true" ]; then \
      uv sync --frozen --no-install-workspace --package app --group presidio-nlp; \
    else \
      uv sync --frozen --no-install-workspace --package app; \
    fi

COPY ./backend/scripts /app/backend/scripts

COPY ./backend/alembic.ini /app/backend/

COPY ./backend/app /app/backend/app

COPY --from=frontend-build /app/backend/app/frontend /app/backend/app/frontend

# Sync the project itself after source is present. Dependencies are already in
# the preceding layer and uv still verifies the same frozen lock.
RUN if [ "$INSTALL_PRESIDIO_NLP" = "true" ]; then \
      uv sync --frozen --package app --group presidio-nlp; \
    else \
      uv sync --frozen --package app; \
    fi

WORKDIR /app/backend/

CMD ["fastapi", "run", "--workers", "4"]
