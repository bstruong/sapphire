# SAPPHIRE Vault — local / compose image.
# Zero PII egress. Fail-loud contracts. Integer-cents money only.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/root/.local/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY scripts ./scripts

RUN uv sync --frozen --no-dev

EXPOSE 8000

# Compose healthcheck hits /api/v1/health; bind all interfaces in-container.
CMD ["uv", "run", "--no-dev", "uvicorn", "app.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
