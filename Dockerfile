FROM python:3.12-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install production dependencies (no dev extras)
RUN uv sync --no-dev --frozen

# Copy source
COPY churnwall/ ./churnwall/

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run with uvicorn
CMD ["uv", "run", "uvicorn", "churnwall.app:app", "--host", "0.0.0.0", "--port", "8000"]
