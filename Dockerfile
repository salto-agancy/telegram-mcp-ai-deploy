ARG PYTHON_IMAGE=python:3.13-alpine
FROM ${PYTHON_IMAGE}

ARG UV_VERSION=0.10.6
ARG APP_UID=10001
ARG APP_GID=10001
ENV PATH="/app/.venv/bin:$PATH" \
    HOME="/tmp" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

# The archive extra is installed even though archive reads stay opt-in: the driver
# has to exist in the image, or ARCHIVE_DSN cannot be switched on in a deployed
# container at all. Behaviour is still governed by configuration, not by the build.
COPY pyproject.toml uv.lock ./
RUN mkdir -p src \
    && touch src/__init__.py \
    && uv sync --frozen --no-dev --no-install-project --extra archive

COPY --chown=${APP_UID}:${APP_GID} src/ ./src/
COPY --chown=${APP_UID}:${APP_GID} README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra archive \
    && mkdir -p /data/sessions /data/oauth \
    && chown -R "${APP_UID}:${APP_GID}" /app /data

USER ${APP_UID}:${APP_GID}
EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["python", "-m", "src.server"]
