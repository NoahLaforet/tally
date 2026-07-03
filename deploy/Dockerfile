# Tally container image. Build from the repo root:
#   docker build -f deploy/Dockerfile -t tally .
# Run it publishing only to loopback (see deploy/README.md):
#   docker run -p 127.0.0.1:8787:8787 -v tally-data:/app/backend/data tally
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# poppler and tesseract are the OCR fallback for statement PDFs on non-macOS hosts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/ backend/
COPY frontend/ frontend/
COPY run.sh run.sh

WORKDIR /app/backend
# Install locked deps plus the ocr extra so pytesseract is available in the container.
RUN uv sync --frozen --extra ocr

EXPOSE 8787

# 0.0.0.0 here is only for the container network namespace. Publish the port
# bound to loopback on the host: -p 127.0.0.1:8787:8787 (see deploy/README.md).
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
