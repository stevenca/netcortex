FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# --- Build stage ---
FROM base AS builder

ARG EXTRAS="all"

# gcc/libssl for native wheels; libxmlsec1/libxml2 + pkg-config so the
# `xmlsec` wheel (pulled in by python3-saml, the in-app SAML SP) builds
# and links against the system crypto libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        pkg-config \
        libssl-dev \
        libxml2-dev \
        libxmlsec1-dev \
        libxmlsec1-openssl \
    || true \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY netcortex/ ./netcortex/

RUN pip install --upgrade pip && pip install ".[${EXTRAS}]"

# --- Runtime stage ---
FROM base AS runtime

# Runtime shared libs for xmlsec/lxml (SAML signature verification).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        openssh-client \
        snmp \
        libxml2 \
        libxmlsec1 \
        libxmlsec1-openssl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user with a fixed numeric UID so Kubernetes can verify runAsNonRoot.
# UID 1001 is safe on Debian slim (outside the system-user range).
RUN groupadd -r -g 1001 netcortex && useradd -r -u 1001 -g netcortex netcortex

# Copy installed packages and console scripts from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy source
COPY --chown=netcortex:netcortex . .

RUN mkdir -p /app/data \
    # OpenShift runs containers as a random UID in GID 0 (root group).
    # Granting group ownership + g=u ensures the process can write /app/data
    # regardless of which UID the platform assigns at runtime.
    && chown -R netcortex:0 /app/data \
    && chmod -R g=u /app/data

USER 1001

EXPOSE 8000

# Python urllib works even when curl is unavailable (mirrors / package install flake)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "netcortex.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
