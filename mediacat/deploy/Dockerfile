# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# MediaCat application image.
# Multi-stage: builder installs deps, runtime copies only what is needed.
# ---------------------------------------------------------------------------

# ---- build stage ----------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Avoid interactive prompts, keep pip quiet
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install dependencies first (layer caching)
COPY pyproject.toml README.md ./
RUN pip install --prefix=/install --no-warn-script-location .

# Copy source and install the package itself
COPY src/ src/
RUN pip install --prefix=/install --no-deps --no-warn-script-location .


# ---- runtime stage --------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

LABEL maintainer="MediaCat Contributors" \
      org.opencontainers.image.source="https://github.com/mediacat/mediacat" \
      org.opencontainers.image.description="MediaCat application server"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Prevent pip from running as root warning; we have no pip at runtime.
    PIP_NO_CACHE_DIR=1

# Runtime system deps only (libpq for psycopg, tesseract for OCR)
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        libpq5 \
        tesseract-ocr \
        tesseract-ocr-eng tesseract-ocr-deu tesseract-ocr-fra \
        tesseract-ocr-ita tesseract-ocr-jpn tesseract-ocr-kor \
        tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-rus \
        curl ca-certificates tini && \
    rm -rf /var/lib/apt/lists/*

# Non-root user.  UID 10001 chosen to avoid collision with host UIDs.
RUN groupadd --gid 10001 mediacat && \
    useradd --uid 10001 --gid mediacat --shell /usr/sbin/nologin \
            --home-dir /app --no-create-home mediacat

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy config defaults (never secrets)
COPY config/logging.yaml /app/config/logging.yaml

# Writable dirs the app needs at runtime (tmp, any cache)
RUN mkdir -p /app/tmp && chown mediacat:mediacat /app/tmp

WORKDIR /app
USER mediacat:mediacat

# Healthcheck (FastAPI exposes /healthz in Section 9; placeholder curl)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["curl", "-sf", "http://127.0.0.1:8000/healthz"]

EXPOSE 8000

# Tini as PID 1 for proper signal handling
ENTRYPOINT ["tini", "--"]

# Default: run the web server.  Workers override CMD in compose.
CMD ["python", "-m", "uvicorn", "mediacat.web.app:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--log-config", "/app/config/logging.yaml", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
