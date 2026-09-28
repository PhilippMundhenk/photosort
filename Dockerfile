# --- shared base: python + exiftool + runtime deps ------------------------------------------
FROM python:3.12-slim AS base
# exiftool: metadata; ffmpeg: one frame per video for thumbnails (no other video work)
RUN apt-get update && apt-get install -y --no-install-recommends libimage-exiftool-perl ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- test image: everything the test suite needs, nothing installed on the host ----------------
# docker build --target test -t photosort-test .   (or: docker compose -f docker-compose.test.yml run --rm tests)
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY app ./app
COPY tests ./tests
ENV PHOTOSORT_DATA=/tmp/photosort-data PYTHONDONTWRITEBYTECODE=1
CMD ["sh", "-c", "ruff check app tests && pytest -q --cov=app --cov-branch --cov-fail-under=99 --cov-report=term-missing:skip-covered"]

# --- runtime image (default target) -----------------------------------------------------------
FROM base AS runtime
COPY app ./app
ENV PHOTOSORT_DATA=/data
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
