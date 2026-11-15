FROM node:24-bookworm-slim AS web
WORKDIR /product
COPY package*.json .npmrc ./
COPY apps/web/package.json apps/web/package.json
COPY packages/ui/package.json packages/ui/package.json
RUN npm ci --ignore-scripts
COPY apps/web apps/web
COPY packages/ui packages/ui
COPY infrastructure infrastructure
COPY scripts/build-web.mjs scripts/build-web.mjs
RUN npm run build

FROM python:3.11-slim
WORKDIR /product
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY apps/api apps/api
RUN pip install --no-cache-dir './apps/api[earthdata]'
COPY services/speech services/speech
COPY app.py speech_api.py ./
COPY data/source_registry.yaml data/source_registry.yaml
COPY data/raw data/raw
COPY --from=web /product/apps/api/src/agrix/static/appbuild /usr/local/lib/python3.11/site-packages/agrix/static/appbuild
# Source imports in app.py intentionally use this same frontend build.
COPY --from=web /product/apps/api/src/agrix/static/appbuild apps/api/src/agrix/static/appbuild
ENV PYTHONPATH=/product/apps/api/src:/product/services/speech:/product
ENV DATABASE_URL=sqlite+pysqlite:////product/data/ghayth.sqlite3
ENV AGRIX_REMOTE_ASSETS=1 AGRIX_HLS_MAX_SCENES=10
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
