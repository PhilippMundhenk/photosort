# Model: open-weights (Apache-2.0) external decision model by a vendor, served behind
# an Jev wire API (POST /v1/systemone) by nvkudva/model-server. CPU only, ~2.2 GB RSS once
# loaded, ~200-500 ms per request on a laptop CPU. The 846 MB checkpoint is downloaded from
# Hugging Face on first start into /models (mount it as a volume so this happens once).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

# Pinned model-server commit (2026-09-22, v0.2.0 API). Bump deliberately.
ARG MODEL_SERVER_REPO=https://github.com/nvkudva/model-server.git
ARG MODEL_SERVER_REF=0a2928f7ab415e8dd14bde483dc793e566c9f8fb
WORKDIR /srv
RUN git clone --quiet ${MODEL_SERVER_REPO} . && git checkout --quiet ${MODEL_SERVER_REF} \
    && rm -rf .git

# uv.lock pins torch to the CPU wheel index on linux; use the image's python, no downloads.
ENV UV_PYTHON=/usr/local/bin/python3.12 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/srv/.venv
RUN uv sync --frozen --no-dev --no-install-project && uv sync --frozen --no-dev

ENV HF_HOME=/models HF_HUB_DISABLE_TELEMETRY=1
VOLUME /models
EXPOSE 8000
# GET / answers only once the model is loaded; the first start also downloads the weights.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15m --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/ >/dev/null || exit 1

ENTRYPOINT ["uv", "run", "--frozen", "--no-sync", "model-server", "serve"]
# model name (model | model-multilingual | model-typed-decisions), then server options
CMD ["model", "--host", "0.0.0.0", "--port", "8000", "--no-browser"]
