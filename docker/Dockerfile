FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# --- Build stage ---
FROM base AS builder

ARG EXTRAS="all"

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libssl-dev || true \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY netcortex/ ./netcortex/

RUN pip install --upgrade pip && pip install ".[${EXTRAS}]"

# --- Runtime stage ---
FROM base AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        openssh-client \
        snmp \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (groupadd/useradd are in the base image)
RUN groupadd -r netcortex && useradd -r -g netcortex netcortex

# Copy installed packages and console scripts from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy source
COPY --chown=netcortex:netcortex . .

RUN mkdir -p /app/data && chown netcortex:netcortex /app/data

USER netcortex

EXPOSE 8000

# Python urllib works even when curl is unavailable (mirrors / package install flake)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "netcortex.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
