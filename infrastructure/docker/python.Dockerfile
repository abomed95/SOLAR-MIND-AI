# One image for the API, the worker, the migrations and the simulator.
# Build context: repository root.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.32

# Dependencies first, from the lock file, so this layer is cached across code changes.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/simulator/pyproject.toml apps/simulator/pyproject.toml
COPY packages/shared-schemas/pyproject.toml packages/shared-schemas/pyproject.toml
COPY packages/energy-core/pyproject.toml packages/energy-core/pyproject.toml
COPY packages/device-adapters/pyproject.toml packages/device-adapters/pyproject.toml
COPY packages/ai-agents/pyproject.toml packages/ai-agents/pyproject.toml
RUN uv sync --frozen --no-dev --all-packages --no-install-workspace

COPY apps/api apps/api
COPY apps/simulator apps/simulator
COPY packages packages
COPY database database
RUN uv sync --frozen --no-dev --all-packages --no-editable

RUN useradd --system --uid 10001 --no-create-home solarmind \
    && mkdir -p /var/lib/solarmind/storage /var/lib/solarmind/gateway \
    && chown -R solarmind /var/lib/solarmind
USER solarmind

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "solarmind_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
