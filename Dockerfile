FROM python:3.12-slim

# Avoid .pyc files and force unbuffered stdout (better container logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (better layer caching). The package metadata
# lives in pyproject.toml, so copy it before the source for the dep layer.
COPY pyproject.toml README.md ./
COPY epic_compliance/ ./epic_compliance/

RUN pip install --upgrade pip && pip install -e .

# Bundle the rule catalog the engine loads at runtime.
COPY rules/ ./rules/

EXPOSE 8000

# `serve` binds uvicorn to 0.0.0.0:8000 (see epic_compliance/__main__.py)
CMD ["python", "-m", "epic_compliance", "serve"]
