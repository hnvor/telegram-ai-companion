FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/hf_cache \
    SENTENCE_TRANSFORMERS_HOME=/app/hf_cache

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

# Torch отдельно, с CPU-only индекса (иначе тянется ~2GB CUDA)
RUN pip install --no-cache-dir \
        "torch>=2.2" \
        --index-url https://download.pytorch.org/whl/cpu

# Остальное — с обычного PyPI
RUN pip install --no-cache-dir \
        "aiogram>=3.13" \
        "anthropic>=0.39" \
        "asyncpg>=0.29" \
        "apscheduler>=3.10" \
        "sqlalchemy>=2.0" \
        "pgvector>=0.3" \
        "sentence-transformers>=3.0" \
        "pydantic>=2.6" \
        "pydantic-settings>=2.2" \
        "structlog>=24.1" \
        "httpx>=0.27" \
        "tzdata>=2024.1" \
        "python-dateutil>=2.9"

COPY src ./src
COPY migrations ./migrations
COPY pyproject.toml ./

RUN mkdir -p /app/logs /app/hf_cache

CMD ["python", "-m", "src.main"]
