# Not what Railway uses (railway.toml picks Nixpacks; see nixpacks.toml). Kept
# working for anyone who runs the app in Docker instead.

# ── the front end: frontend/ builds to frontend/dist, served at /app ──
FROM node:20-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ── the app ──
FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-eng \
    fonts-dejavu-core \
    poppler-utils \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=frontend /frontend/dist ./frontend/dist

EXPOSE 8080
# One worker, as in the Procfile: it is async, and three measured 270MB for no gain.
CMD ["gunicorn", "web.app:app", "--workers", "1", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8080", "--timeout", "120"]
