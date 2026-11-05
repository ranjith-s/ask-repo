# syntax=docker/dockerfile:1

# ---- 1. Build the React app -------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 2. Python runtime --------------------------------------------------------
FROM python:3.12-slim AS app
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app
RUN pip install --no-cache-dir uv==0.11.32

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/app/.models \
    STATIC_DIR=/app/static

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Bake the embedding model into the image: no download at startup, and the
# container can run without internet access to Hugging Face.
ARG PRELOAD_MODEL=true
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
RUN if [ "$PRELOAD_MODEL" = "true" ]; then \
      python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBEDDING_MODEL}')"; \
    fi

COPY backend/app ./app
COPY --from=web /web/dist ./static
RUN chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
