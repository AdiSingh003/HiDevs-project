# syntax=docker/dockerfile:1

# ---- 1. build the arena UI ------------------------------------------------------------------
FROM node:20-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. runtime: FastAPI serves the API + the built UI --------------------------------------
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    FRONTEND_DIST=/app/frontend/dist \
    AUTOMATA_WORKDIR=/tmp/automata
WORKDIR /app

COPY requirements.txt ./
# lyzr-automata 0.1.3 declares python<3.12 and pins an old openai client it does not need here;
# its code is pure Python and only imports requests/pydantic, so install it without its pins.
RUN pip install -r requirements.txt \
 && pip install --no-deps --ignore-requires-python lyzr-automata==0.1.3

COPY agents/ agents/
COPY backend/ backend/
COPY --from=ui /ui/dist frontend/dist

RUN useradd --create-home --uid 10001 negotiator \
 && mkdir -p /data /tmp/automata \
 && chown -R negotiator /data /tmp/automata
USER negotiator

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
