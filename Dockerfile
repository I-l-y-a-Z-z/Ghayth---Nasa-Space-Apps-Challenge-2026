FROM docker.io/library/node:24-bookworm-slim AS web
WORKDIR /product
COPY package*.json .npmrc ./
COPY apps/web/package.json apps/web/package.json
COPY packages/ui/package.json packages/ui/package.json
RUN npm ci --ignore-scripts
COPY apps/web apps/web
COPY packages/ui packages/ui
COPY infrastructure/security infrastructure/security
COPY scripts/build-web.mjs scripts/build-web.mjs
RUN npm run build

FROM docker.io/library/python:3.11-slim AS runtime
WORKDIR /product
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY apps/api apps/api
RUN pip install --no-cache-dir './apps/api[earthdata,cloudflare]'
COPY services/speech services/speech
ARG INSTALL_SPEECH=0
RUN if [ "$INSTALL_SPEECH" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends build-essential cmake libsndfile1 && \
      pip install --no-cache-dir 'torch==2.7.1+cpu' 'torchaudio==2.7.1+cpu' --index-url https://download.pytorch.org/whl/cpu && \
      pip install --no-cache-dir -r services/speech/requirements-tts-kandir.txt \
        'llama-cpp-python==0.3.16' --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu && \
      apt-get purge -y --auto-remove build-essential cmake && rm -rf /var/lib/apt/lists/*; \
    fi
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
CMD ["python", "-m", "agrix.deployment"]
