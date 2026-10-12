# The web dashboard (docs/dashboard.md): the React app built in a Node stage, served with the JSON API by FastAPI.
# Build context: this directory (docker compose build dashboard).

# glibc, not Alpine: Rollup, esbuild and Tailwind ship native binaries as per-platform optional packages, and the
# musl ones are the ones most often missing from a lockfile made on a Mac.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# optional: the repository's address, for the docs link in the footer (empty: the footer names the file instead)
ARG VITE_REPO_URL=""
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
COPY api/*.py ./
COPY --from=web /web/dist ./static
# Owned by root and only readable by the service user, which has no home and no shell.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin dashboard
USER dashboard
EXPOSE 8080
# Liveness only: /api/health answers 200 while the process runs and reports the database separately.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"]
# 0.0.0.0 inside the container only; compose publishes the port on the host's 127.0.0.1 (docs/dashboard.md).
CMD ["uvicorn", "app:build", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
