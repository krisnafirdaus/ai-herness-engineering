# ─────────────────────────────────────────────────────────────────────────────
# Harness control-plane image (API server + worker). The worker talks to the
# Docker daemon to launch sandbox containers, so deployments mount the host
# Docker socket (docker-out-of-docker) — see docker-compose.yml.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY dummy-repos ./dummy-repos
COPY pyproject.toml ./

ENV HARNESS_DATABASE_URL=postgresql://harness:harness@postgres:5432/harness \
    HARNESS_QUEUE=redis \
    HARNESS_REDIS_URL=redis://redis:6379/0 \
    HARNESS_SANDBOX=docker \
    HARNESS_SANDBOX_IMAGE=harness-sandbox:latest

# Default command runs the API; the worker service overrides it (see compose).
EXPOSE 8000
CMD ["uvicorn", "src.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
