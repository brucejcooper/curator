# The curator service: the reader app, its API and the curating loops.
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
COPY curator ./curator
COPY agent ./agent
COPY web ./web
ENV UV_PYTHON_PREFERENCE=only-system UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN uv sync --frozen --no-dev --extra server

# Bake the embedding model into the image so start-up needs no download.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
RUN uv run --no-sync python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

RUN useradd --uid 5000 --create-home curator && mkdir -p /data && chown -R curator /data /opt/fastembed
USER curator
ENV PATH=/app/.venv/bin:$PATH \
    CURATOR_DATA=/data CURATOR_REPO=/data/repo PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["curator"]
